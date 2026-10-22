# Knowledge Universe sau khi đổi kiến trúc SAG

## Đối chiếu với SAG gốc

Đã đối chiếu mã gốc Zleap-AI/SAG tại commit
`d3c882e676a01f59b64b639880b2bfceaa504da6` (v1.9.1).

| Thành phần | Bản gốc | Kiến trúc hiện tại và cách trình bày |
| --- | --- | --- |
| Không gian | Cảnh 3D, tinh vân nguồn, sao, camera và mức chi tiết | Dùng lại scene engine, camera, thao tác chuột và bàn phím |
| Dữ liệu | Source → Event → Entity | Cây routing đã publish → cụm → KnowledgeUnit |
| Cụm nhỏ | Dữ liệu sự kiện phân bố trong không gian | Một leaf mở thẳng các KnowledgeUnit, tránh chỉ hiện một nút cho cả tài liệu |
| Liên kết | Quan hệ sự kiện/thực thể | Chỉ các cạnh tương đồng từ manifest đã publish; cạnh phân cấp được đánh dấu là liên kết trình bày |
| Chi tiết | Sự kiện hoặc thực thể | Nội dung canonical, tài liệu, phiên bản, trang và bằng chứng trích xuất |
| Kết quả tìm kiếm | Các node event/entity | SearchUnit assignment hoặc canonical block reference; phân biệt kết quả thực với đường routing |
| Làm mới | Có luồng rebuild Universe | Chỉ tải lại dữ liệu đọc; không publish cây và không tăng search epoch |

## Vì sao giao diện cũ chỉ còn một chấm

Frontend cũ chuyển sang `UniverseTreeView` dạng SVG khi manifest có cây.
Nhánh này bỏ qua cảnh 3D gốc. Backend mới cung cấp cây routing thay vì các
partition event/entity mà bộ điều phối 3D cũ chờ nhận.

Một leaf có thể chứa toàn bộ corpus nhỏ. Hiển thị mỗi leaf như một điểm làm
người dùng không thấy các KnowledgeUnit bên trong. Nhãn dựa vào entity có
thể chọn một số như `0.75`, nên điểm này còn không cho biết nội dung gì.

Khi inventory routing không khớp dữ liệu hiện hành, manifest trả `stale` và
không trả cây. Đây là ràng buộc nhất quán của retrieval; Universe giữ nguyên
ràng buộc đó và bổ sung khung nhìn theo nguồn riêng.

## Hai chế độ dữ liệu

1. **Routing:** đọc manifest ACTIVE cùng version và search epoch hiện hành.
   Membership lấy từ routing profile và SearchUnit assignment đã publish.
   Không đọc cây ứng viên đang xây dựng để giả thành cây hiện hành.
2. **Source:** nếu không có cây để trình bày, nhóm KnowledgeUnit hiện hành
   theo nguồn được cấp quyền. `projection="source"`, snapshot có tiền tố
   `source-`, `search_epoch=null`; không tồn tại cây routing mới trong DB.
   Giao diện ghi rõ đang chờ cây đồng bộ và chưa hiển thị liên kết tương đồng.

Chế độ theo nguồn vẫn yêu cầu phiên bản canonical đang có hiệu lực và đáp
ứng điều kiện search readiness của backend. Có dữ liệu thô chưa được phép
truy xuất không đồng nghĩa với việc được phép đưa vào Universe.

## Phạm vi thay đổi

- Frontend: bộ điều phối Universe mới, adapter KnowledgeUnit sang cảnh 3D,
  nhãn dễ đọc, breadcrumb, phân trang, chi tiết, danh sách dự phòng khi WebGL
  lỗi, cài đặt hiển thị và ánh xạ bằng chứng từ tìm kiếm/câu trả lời.
- API: thêm các thao tác đọc trong `/universe/knowledge/`:
  `overview`, `cluster`, `units/{id}` và `resolve`.
  `resolve` dùng POST để mang danh sách tham chiếu, không ghi dữ liệu.
- Dùng lại bảng, cấu hình và cơ chế xác thực hiện có. Không migration,
  không thay ingestion, retrieval, clustering, Qdrant hoặc tree publisher.

## Tính nhất quán và quyền truy cập

- Kiểm tra tenant, project, partition và SourceProjectMapping CONFIRMED.
- Dùng cùng bộ kiểm tra phiên bản hiện hành/index readiness với retrieval.
- Snapshot routing được kiểm tra checksum, version và active epoch.
- Cursor ký được gắn với phạm vi truy cập, snapshot, node và chế độ related.
- Mỗi trang tối đa 50 đơn vị; resolve tối đa 100 tham chiếu, tối đa 500
  đơn vị khớp trong một project.
- Nội dung phải khớp canonical block, content hash và provenance. Chi tiết
  kiểm tra thêm checksum của đơn vị và vị trí trích dẫn của bằng chứng.
- Hai đầu của cạnh đều phải nằm trong phạm vi và khung nhìn được trả về.
- Khi quyền hoặc snapshot thay đổi, frontend bỏ nội dung đang giữ và yêu cầu
  tải lại; không tiếp tục trình bày chi tiết từ snapshot cũ.

## Kiểm tra

Các bài kiểm tra tập trung nằm ở `apps/api/tests/test_universe_knowledge.py`
và `apps/web/lib/universe-knowledge-projection.test.ts`.

Ảnh và kịch bản trình duyệt nằm trong thư mục bị git ignore
`output/playwright/`. Ảnh dùng dữ liệu mẫu để kiểm tra bố cục; chúng không
phải ảnh chụp nội dung riêng tư của project thật. Kiểm tra trình duyệt chặn
mọi thao tác ghi và kiểm tra không có lời gọi `/universe/rebuild`.

Chưa đo tải dữ liệu lớn, chưa kiểm tra LLM hoặc toàn bộ luồng đăng nhập thật.
API phải được chạy bằng đúng cấu hình môi trường hiện có của project để
kiểm tra tích hợp với dữ liệu thật; không thay cấu hình DB để phục vụ UI.
