import sys
import io

# Đảm bảo in tiếng Việt trên console Windows không lỗi mã hóa
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from laya import Router

print("Đang khởi tạo Laya Router...")
# Khởi tạo Router. preload=False để tiết kiệm tài nguyên khi chạy thử nghiệm
router = Router(preload=False)

# 1. Đoạn văn bản đầu vào (ví dụ email/ticket của người dùng)
state = {
    "from": "user@example.com",
    "subject": "Bị trừ tiền 2 lần hóa đơn tháng 3",
    "body": "Tôi bị trừ tiền 2 lần cho hóa đơn tháng 3. Hãy hoàn lại tiền cho tôi ngay hôm nay nếu không tôi sẽ hủy dịch vụ và chuyển sang dùng nền tảng khác."
}

# 2. Định nghĩa các câu hỏi phân loại quyết định (Typed Questions)
questions = {
    "department": {
        "type": "choice",
        "instructions": "Which department should handle this request?",
        "criteria": {
            "billing": "invoices, payments, refunds, double charge",
            "technical": "bugs, outages, system errors, crashes",
            "sales": "pricing, new contracts, demo request",
            "other": "everything else"
        }
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this request?",
        "criteria": ["not urgent", "soon", "critical deadline or blocking issue"]
    },
    "churn_risk": {
        "type": "noul",
        "instructions": "Does the user threaten to cancel or leave?"
    },
    "refund_requested": {
        "type": "noul",
        "instructions": "Does the user explicitly request a refund?"
    }
}

print("\n--- Đang phân tích văn bản qua Laya ---")
result = router.predict(state, questions)

print("\n========== KẾT QUẢ PHÂN TÍCH QUYẾT ĐỊNH ==========")
print(f"• Model được router tự động chọn: {result.get('routing', {}).get('model', 'N/A')}")
print(f"• Lý do điều phối: {result.get('routing', {}).get('reason', 'N/A')}")
print("-" * 50)
answers = result.get("answers", {})
for q_name, ans in answers.items():
    print(f"• Câu hỏi: [{q_name}] -> Kết quả: {ans}")
print("==================================================")
