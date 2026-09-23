# Data ownership và storage boundary

> Trạng thái: Accepted cho kiến trúc tích hợp BE – SAG hiện tại.
>
> Mục đích: tránh nhầm lẫn giữa database nghiệp vụ của Continuum và database
> phục vụ RAG của SAG.

## Kết luận ngắn

- **BE / Continuum** dùng MongoDB làm nơi sở hữu dữ liệu identity và nghiệp vụ
  chính: user, project, team, membership và permission.
- **SAG** dùng PostgreSQL cho metadata và trạng thái nội bộ của RAG: source,
  document, job, OCTX state và các quan hệ cần transaction.
- **Qdrant** lưu embedding/vector và payload phục vụ semantic retrieval.
- SAG không thay thế BE để quản lý user/project toàn cục. Nếu SAG có model user
  hoặc auth riêng, đó là identity/runtime cục bộ của SAG và phải map được về
  identity của BE.

## Phân chia ownership

| Dữ liệu | Source of truth | Storage | Ghi chú |
| --- | --- | --- | --- |
| User, organization | BE | MongoDB | BE quản lý vòng đời và trạng thái user |
| Project | BE | MongoDB | BE quản lý create/update và visibility |
| Team, project membership, team membership | BE | MongoDB | BE quyết định user có quyền truy cập project/team |
| Permission/capability | BE | MongoDB | Không tin tưởng permission do frontend tự gửi |
| SAG source/document/job | SAG | PostgreSQL | Metadata và trạng thái pipeline upload/indexing |
| Chunk metadata/OCTX state | SAG | PostgreSQL | Dùng để truy vết và quản lý lifecycle |
| Embedding/vector và payload chunk | SAG | Qdrant | Dùng cho similarity search và filter retrieval |
| File upload gốc | SAG | `SAG_UPLOAD_DIR`/volume | Không lưu file gốc trong Qdrant |

## Ranh giới tích hợp

Hai database không join trực tiếp với nhau. Luồng chuẩn là:

```text
Client
  ↓ authenticate
BE / MongoDB
  ↓ user_id, project_id, team_id và permission assertion đã xác thực
SAG API
  ↓ lưu metadata
PostgreSQL
  ↓ lưu/tìm embedding và payload filter
Qdrant
```

Khi người dùng hỏi về tài liệu:

1. BE xác thực user và xác định project/team scope.
2. BE hoặc gateway truyền identity và scope đã xác thực cho SAG.
3. SAG chỉ truy xuất các source/document được phép.
4. SAG chuyển filter quyền truy cập thành điều kiện trên metadata/Qdrant
   payload.
5. SAG trả context và citation; BE tiếp tục điều phối response về client.

`project_id`, `team_id` hoặc `user_id` do client tự nhập không được xem là
đủ để cấp quyền truy cập.

## Quy tắc cho AI và developer

- Không tạo bản sao độc lập của User/Project/Membership trong SAG nếu chưa có
  quyết định ownership mới.
- Không tạo foreign key trực tiếp giữa MongoDB và PostgreSQL.
- Dùng các external ID ổn định (`user_id`, `project_id`, `team_id`) để liên kết
  dữ liệu giữa BE và SAG.
- Khi ingest tài liệu, SAG cần lưu scope/ACL metadata cần thiết để lọc
  retrieval, nhưng quyền gốc vẫn thuộc BE.
- Khi retrieval, luôn áp dụng permission filter trước khi đưa chunk vào context.
- Qdrant chỉ là vector store; nó không chịu trách nhiệm xác thực user hay
  quyết định membership.
- `SAG_EMBEDDING_*` là cấu hình model tạo embedding; `SAG_SAG_QDRANT_*` là
  cấu hình nơi lưu/tìm vector. Hai nhóm biến này không thay thế cho nhau.

## Cấu hình tham chiếu

### BE

```env
MONGODB_URI=mongodb://...
MONGODB_DATABASE=continuum
```

### SAG

```env
SAG_DATABASE_URL=postgresql+asyncpg://...
SAG_SAG_RELATIONAL_PROVIDER=postgres
SAG_SAG_VECTOR_PROVIDER=qdrant
SAG_SAG_QDRANT_URL=https://<cluster-endpoint>
SAG_SAG_QDRANT_API_KEY=<secret>
```

Không commit API key hoặc credential thật vào repository.

## Phạm vi hiện tại và việc cần làm

- BE đã có hướng cấu hình MongoDB/Mongoose và IAM contract cho user, project,
  team và membership; endpoint/schema nghiệp vụ cần được triển khai theo
  contract đã duyệt.
- SAG đã chuyển storage mặc định sang PostgreSQL + Qdrant.
- Cần có integration test chứng minh `project/team/user scope` từ BE được
  chuyển thành filter retrieval đúng trong SAG/Qdrant.
- Không đánh dấu luồng phân quyền hoàn tất chỉ vì kết nối Qdrant thành công.
