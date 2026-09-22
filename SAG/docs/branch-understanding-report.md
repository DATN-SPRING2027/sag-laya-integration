# Báo cáo đọc hiểu branch

> Ngày: 2026-09-22

## Mục tiêu

Branch này chuyển SAG từ mô hình mặc định `SQLite + LanceDB` sang
`PostgreSQL + Qdrant`. Đồng thời branch loại bỏ local embedding server,
storage bootstrap và upgrade flow dành cho LanceDB cũ.

## Thay đổi chính

### Storage

- `compose.yaml` thêm PostgreSQL và Qdrant thành service chính.
- `compose.postgres.yaml` đổi `pgvector/pgvector:pg16` thành `postgres:16` và
  thêm Qdrant.
- Thêm volume `pgdata` và `qdrantdata`; API tiếp tục dùng `sagdata`.
- Mặc định API dùng `sag_vector_provider=qdrant` và
  `sag_relational_provider=postgres`.
- Cấu hình thêm URL/API key cho Qdrant và thông tin kết nối PostgreSQL.

### Qdrant

File mới `apps/api/sag_api/sag/qdrant_store.py` hiện thực vector store qua
HTTP Qdrant: collection schema, upsert, get, scroll, delete, query/filter và
chuẩn hóa lỗi storage. Provider được đăng ký vào engine registry.

`config_builder.py` thay vector config bằng `QdrantVectorConfig`.
`engine_manager.py` đánh dấu Qdrant không hỗ trợ lexical search nên chiến
lược lexical fallback về vector search.

### Loại bỏ thành phần cũ

- Xóa logic đọc vector trực tiếp từ LanceDB trong `octx_vector_protocol.py`.
- Xóa `tools/local_embedding_server.py`.
- Xóa API/schema/UI gate/desktop policy của storage bootstrap.
- Xóa toàn bộ `sag_api.upgrades` và test/fixture migration cũ.
- `main.py` khởi động `KnowledgeRuntime` trực tiếp.
- Login, readiness, Web và Desktop không còn phụ thuộc bootstrap storage.

### OCTX và ORM

- Khôi phục các model ORM trong `apps/api/sag_api/db/models/`.
- Giữ state transition OCTX trong `db/models/octx.py`.
- GC/transfer service không còn recovery path cho migration LanceDB cũ.

## Luồng runtime mới

```text
PostgreSQL -> metadata, user, document, job và OCTX state
Qdrant     -> vector collections và vector search
KnowledgeRuntime -> khởi tạo engine trực tiếp
FastAPI    -> expose API
Web/Desktop -> gọi API, không còn chọn migrate/fresh storage
```

## Cách chạy theo branch

```powershell
cd D:\DoAnTotnghiep\sag-laya-integration\SAG
Copy-Item .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Production override cần `POSTGRES_PASSWORD` và `SAG_SECRET_KEY` trong `.env`:

```powershell
docker compose -f compose.yaml -f compose.postgres.yaml up -d --build
```

## Kiểm chứng

- Ruff cho OCTX model/service: **pass**.
- OCTX focused tests: **12 passed, 1 deselected**.
- Import `sag_api.main` với SQLite test environment: **pass**.
- Qdrant adapter có test HTTP mock tại
  `apps/api/tests/test_qdrant_vector_store.py`.
- Pyrefly được cấu hình trỏ về `apps/api/.venv`.

## Điểm còn tồn tại

1. Qdrant là mặc định, nhưng `pgvector` vẫn còn trong một số type/nhánh tương
   thích; branch chưa xóa tuyệt đối mọi hỗ trợ pgvector.
2. Test Qdrant hiện mock HTTP, chưa phải smoke test với container Qdrant thật.
3. Các file mới của branch cần được review cùng nhau: `pyrefly.toml`,
   `db/models/`, `qdrant_store.py`, test Qdrant và report này.
4. `litellm==1.92.0` trên Windows còn cần Rust/MSVC linker để build; đây là
   vấn đề môi trường dependency, không phải thay đổi storage.
