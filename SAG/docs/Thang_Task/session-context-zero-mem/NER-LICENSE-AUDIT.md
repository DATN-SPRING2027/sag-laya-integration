# Audit license NER — 26/10/2026

**Kết luận: chưa xác minh được quyền dùng/redistribute fine-tune; không đạt gate rollout.** Đây là kết quả kiểm tra nguồn công khai, không phải kết luận rằng model bị cấm trong mọi trường hợp. Không tự đổi status thành MIT từ license model nền.

Đối tượng: `Davlan/xlm-roberta-base-wikiann-ner`, revision `642b9a14e71df124c43c67e47703a91341576dfb` đang dùng trong T3b/T4.

| Nguồn chính chủ đã kiểm | Bằng chứng |
|---|---|
| [Model API đúng revision](https://huggingface.co/api/models/Davlan/xlm-roberta-base-wikiann-ner/revision/642b9a14e71df124c43c67e47703a91341576dfb) | `cardData.license` không có/null; không có file tên LICENSE/COPYING trong siblings |
| [Cây file đúng revision](https://huggingface.co/Davlan/xlm-roberta-base-wikiann-ner/tree/642b9a14e71df124c43c67e47703a91341576dfb) | README, config, tokenizer và weights; không tìm thấy grant/license riêng |
| [Model card](https://huggingface.co/Davlan/xlm-roberta-base-wikiann-ner/blob/642b9a14e71df124c43c67e47703a91341576dfb/README.md) | Có mô tả training/languages/limitations, không khai license fine-tune |
| [Community API](https://huggingface.co/api/models/Davlan/xlm-roberta-base-wikiann-ner/discussions) | Hai discussion/PR đã merged: thêm TF weights và safetensors; không có xác nhận license |
| [Base model API](https://huggingface.co/api/models/FacebookAI/xlm-roberta-base) | Model nền khai MIT. Đây không phải grant riêng cho weights fine-tune của Davlan |

API response hashes ghi trong `evidence/ner-license-audit.json` để truy nguyên lần kiểm tra. API có fields động nên hash là snapshot lần đọc, không phải cam kết response tương lai bất biến. Không track model weights, token đăng nhập hoặc dữ liệu user.

Gate trước rollout: cần grant/terms rõ ràng từ chủ fine-tune áp dụng đúng artifact/version và rà các nghĩa vụ tương ứng; hoặc chọn model NER có provenance/license rõ rồi chạy lại calibration với revision đó. Không lấy license từ dự án bên thứ ba sử dụng model, hoặc từ model cùng tác giả nhưng khác tên, để cấp quyền cho model này.

T4 vẫn đo prototype và ghi số liệu riêng. Kết quả chất lượng tốt không tự đóng license gate. Việc gửi yêu cầu xác nhận cho chủ model và phê duyệt sử dụng/redistribution là bước riêng; audit này không giả đã có văn bản chấp thuận.

Runtime follow-up: chọn model khác có publisher Apache-2.0 declaration, xem [MODEL-LICENSES](MODEL-LICENSES.md). Kết luận Davlan ở trên không thay đổi; các số T4 vẫn thuộc model cũ, runtime candidate mới phải calibration/holdout lại.
