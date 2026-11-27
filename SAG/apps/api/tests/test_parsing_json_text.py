import json

import pytest

from sag_api.core.config import Settings
from sag_api.parsing.service import prepare_document


@pytest.mark.parametrize("suffix", [".json", ".JSON"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
async def test_json_with_unicode_after_long_ascii_prefix_is_preserved(tmp_path, suffix, encoding):
    content = json.dumps(
        {"padding": "a" * 16000, "message": "Approved – scenario tiếng Việt"},
        ensure_ascii=False,
    )
    original = tmp_path / f"scenarios{suffix}"
    original.write_bytes(content.encode(encoding))
    original_bytes = original.read_bytes()

    prepared = await prepare_document(str(original), Settings(_env_file=None), state={})

    assert original.read_bytes() == original_bytes
    with open(prepared.path, encoding="utf-8-sig") as parsed:
        assert parsed.read().strip() == content
