# SAG + Laya Local Integration

Workspace phục vụ phát triển và kiểm thử luồng:

```text
Upload tài liệu
  → SAG tạo Document/Job
  → worker parse/chunk/index
  → Document READY
  → Laya Local route câu hỏi
  → SAG retrieval context
  → LLM sinh câu trả lời
```

## Cấu trúc

```text
.
├── SAG/       # Ứng dụng RAG chính: API, worker và web UI
├── laya/      # Mã nguồn/demo và môi trường local của Laya
├── scripts/   # Script hỗ trợ workspace
└── tasks/     # Kế hoạch ở cấp workspace
```

`SAG` là application chính. `laya` chỉ cung cấp router local để phân loại câu hỏi; nó không thay thế parser, vector search hoặc LLM sinh câu trả lời.

## Thiết lập Laya Local

Không commit checkpoint hoặc secret vào repository. Đặt model ở ngoài Git, ví dụ:

```powershell
$env:SAG_ENABLE_LAYA = "true"
$env:SAG_LAYA_MODEL_PATH = "C:\laya-local"
$env:SAG_LAYA_DEVICE = "cpu"
```

Hoặc cấu hình các biến tương ứng trong file `.env` local. Các file `.env` thật đã được ignore; chỉ commit file `.env.example`.

## Chạy SAG

Xem hướng dẫn đầy đủ trong [SAG/README.md](SAG/README.md). Các phần chính nằm ở:

- API: `SAG/apps/api`
- Web: `SAG/apps/web`
- Laya router: `SAG/apps/api/sag_api/services/laya_router.py`
- Laya endpoint: `SAG/apps/api/sag_api/api/v1/laya.py`
- Kế hoạch triển khai: `SAG/tasks/plan.md`
- Checklist: `SAG/tasks/todo.md`

## Quy tắc repository

- Không commit API key, `.env`, database local, `node_modules`, `.venv` hoặc model checkpoint.
- Không sửa trực tiếp model artifact; tải model theo hướng dẫn local.
- Mỗi thay đổi nên có test phù hợp ở API hoặc web trước khi commit.
