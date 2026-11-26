"""对话图片附件 —— 上传与取回（本地落盘，鉴权访问）。

仅图片、≤10MB；id = uuid+原始扩展名（正则校验，杜绝路径穿越）。
消息里只存附件 meta（id/media_type/name），发送给视觉模型时由生成层读盘转 base64。
"""

from __future__ import annotations

import io
import os
import re
import struct
import uuid

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from sag_api.core.config import settings
from sag_api.core.conversation_ownership import get_conversation_owner
from sag_api.core.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/attachments", tags=["attachments"])

_ALLOWED = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}
_EXPECTED_PIL_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".gif": "GIF",
}
_MAX_MB = 10
_MAX_IMAGE_PIXELS = 36_000_000
_ID_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpe?g|webp|gif)$")


def _extract_header_dimensions(ext: str, data: bytes) -> tuple[int, int]:
    if ext == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if ext == ".gif" and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")) and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if ext in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"):
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            if marker == 0xFF:
                idx += 1
                continue
            if marker in {0xD8, 0xD9} or (0xD0 <= marker <= 0xD7):
                idx += 2
                continue
            seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                return w, h
            idx += 2 + max(2, seg_len)
    if ext == ".webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return w, h
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return w, h
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return 0, 0


def _verify_image_attachment(ext: str, data: bytes) -> None:
    if ext == ".png":
        magic_ok = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in {".jpg", ".jpeg"}:
        magic_ok = data.startswith(b"\xff\xd8\xff")
    elif ext == ".gif":
        magic_ok = data.startswith((b"GIF87a", b"GIF89a"))
    elif ext == ".webp":
        magic_ok = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    else:
        magic_ok = False
    if not magic_ok:
        raise ValidationError("Tệp đính kèm không khớp chữ ký định dạng ảnh hợp lệ")

    hdr_w, hdr_h = _extract_header_dimensions(ext, data)
    if hdr_w > 0 and hdr_h > 0 and (hdr_w * hdr_h) > _MAX_IMAGE_PIXELS:
        raise ValidationError(
            f"Kích thước ảnh quá lớn ({hdr_w}x{hdr_h} px), vượt giới hạn cho phép"
        )

    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != _EXPECTED_PIL_FORMATS.get(ext):
                raise ValidationError("Định dạng thực tế của ảnh không khớp phần mở rộng tệp")
            width, height = im.size
            if width <= 0 or height <= 0 or (width * height) > _MAX_IMAGE_PIXELS:
                raise ValidationError(
                    f"Kích thước ảnh quá lớn ({width}x{height} px), vượt giới hạn cho phép"
                )
            im.verify()
    except ValidationError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError("Tệp ảnh bị hỏng hoặc không hợp lệ") from exc


def _dir(owner_key: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", owner_key) is None:
        raise NotFoundError("附件不存在")
    path = os.path.join(settings.upload_dir, "attachments", owner_key)
    os.makedirs(path, exist_ok=True)
    return path


def attachment_path(attachment_id: str, *, owner_key: str) -> str | None:
    """id → 磁盘路径（校验失败/不存在返回 None）。供生成层复用。"""
    if not _ID_RE.match(attachment_id or ""):
        return None
    path = os.path.join(_dir(owner_key), attachment_id)
    return path if os.path.isfile(path) else None


@router.post("", status_code=201)
async def upload(
    file: UploadFile,
    owner_key: str = Depends(get_conversation_owner),
) -> dict:
    ext = os.path.splitext(file.filename or "")[1].lower()
    media_type = _ALLOWED.get(ext)
    if media_type is None:
        raise ValidationError("仅支持图片附件（png / jpg / webp / gif）")
    max_upload_bytes = _MAX_MB * 1024 * 1024
    data = await file.read(max_upload_bytes + 1)
    if len(data) > max_upload_bytes:
        raise ValidationError(f"图片过大（上限 {_MAX_MB}MB）")
    _verify_image_attachment(ext, data)
    attachment_id = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_dir(owner_key), attachment_id), "wb") as f:
        f.write(data)
    return {"id": attachment_id, "name": file.filename or attachment_id, "media_type": media_type}


@router.get("/{attachment_id}")
async def get_file(
    attachment_id: str,
    owner_key: str = Depends(get_conversation_owner),
) -> FileResponse:
    path = attachment_path(attachment_id, owner_key=owner_key)
    if path is None:
        raise NotFoundError("附件不存在")
    ext = os.path.splitext(attachment_id)[1]
    return FileResponse(path, media_type=_ALLOWED.get(ext, "application/octet-stream"))
