"""TDD regression tests for Batch P6-3 Packet 1:
- Vietnamese BM25 negation/obligation polarity preservation & Thai combining marks (ReDoS-safe)
- C99 off-diagonal prose cohesion segmentation (no diagonal-dominance shattering)
- MinerU HTML table colspan/rowspan twin fix
- QdrantVectorStore.query None-last sorting in both asc and desc directions
"""

from __future__ import annotations

import time
import unicodedata

import httpx
import pytest
from zleap.sag.core.adapters.models import VectorQuery

from sag_api.core.bm25_tokenizer import COMMON_STOPWORDS, tokenize_bm25_tokens
from sag_api.parsing.canonical import estimate_unicode_tokens, segment_prose_by_information_cohesion
from sag_api.parsing.mineru import _convert_html_table_to_markdown
from sag_api.sag.qdrant_store import QdrantVectorConfig, QdrantVectorStore
from sag_api.sag.search_unit_store import build_sparse_query_vector
from sag_api.services.search_index_service import compute_sparse_bm25_vector


def test_bm25_preserves_vietnamese_negation_and_obligation_polarity():
    for polarity_word in ("không", "chưa", "phải"):
        assert polarity_word not in COMMON_STOPWORDS

    # Real function stopwords remain filtered
    for stop in ("của", "là", "gì", "trong"):
        assert stop in COMMON_STOPWORDS

    neg_text = "nhân viên không được phép truy cập hệ thống"
    pos_text = "nhân viên được phép truy cập hệ thống"
    r_neg = tokenize_bm25_tokens(neg_text)
    r_pos = tokenize_bm25_tokens(pos_text)
    assert "không" in r_neg.tokens
    assert r_neg.tokens != r_pos.tokens
    assert compute_sparse_bm25_vector(neg_text) != compute_sparse_bm25_vector(pos_text)
    assert build_sparse_query_vector(neg_text) != build_sparse_query_vector(pos_text)

    must_text = "nhân viên chưa phải nộp báo cáo"
    plain_text = "nhân viên nộp báo cáo"
    r_must = tokenize_bm25_tokens(must_text)
    r_plain = tokenize_bm25_tokens(plain_text)
    assert "chưa" in r_must.tokens
    assert "phải" in r_must.tokens
    assert r_must.tokens != r_plain.tokens
    assert compute_sparse_bm25_vector(must_text) != compute_sparse_bm25_vector(plain_text)


def test_bm25_preserves_thai_combining_marks_and_linear_time():
    thai_phrase = "สวัสดีผู้ใช้งาน"
    res = tokenize_bm25_tokens(thai_phrase)
    joined = "".join(res.tokens)
    # All Thai vowel/tone combining marks must be preserved, never shredded out
    for mark in ("\u0e31", "\u0e35", "\u0e39", "\u0e49"):
        assert mark in joined, f"Combining mark {mark!r} lost from tokens={res.tokens}"
    # No token should start with an orphaned combining mark
    for tok in res.tokens:
        assert not unicodedata.category(tok[0]).startswith("M"), f"Orphaned combining mark in {tok!r}"

    # Glued Thai alnum with tone mark splits cleanly without losing combining mark
    glued_thai = tokenize_bm25_tokens("ข้อ15")
    assert "ข้อ" in glued_thai.expanded_tokens
    assert "15" in glued_thai.expanded_tokens

    # NFC/NFD Vietnamese equivalence stays intact
    nfd_vi = unicodedata.normalize("NFD", "tiếng việt điều15")
    nfc_vi = "tiếng việt điều15"
    assert tokenize_bm25_tokens(nfd_vi) == tokenize_bm25_tokens(nfc_vi)

    # Linear-time ReDoS safety on 300+ char mixed combining-mark/alnum input
    stress = ("สวัสดีผู้ใช้งาน123 " + unicodedata.normalize("NFD", "tiếng việt không chưa phải điều15 ")) * 8
    assert len(stress) > 300
    t0 = time.perf_counter()
    stress_res = tokenize_bm25_tokens(stress)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert stress_res.tokens
    assert elapsed_ms < 25.0, f"Tokenizer took {elapsed_ms:.2f}ms (possible ReDoS)"


def test_c99_cohesive_vietnamese_paragraph_and_multi_topic_boundaries():
    cohesive_sentences = [
        (
            "Chính sách nghỉ phép năm của công ty áp dụng cho toàn bộ nhân viên chính thức "
            "đã ký hợp đồng lao động không xác định thời hạn."
        ),
        (
            "Mỗi nhân viên làm đủ mười hai tháng trong năm dương lịch được hưởng tối thiểu "
            "mười hai ngày nghỉ phép có hưởng nguyên lương."
        ),
        (
            "Trường hợp nhân viên có thâm niên công tác từ đủ năm năm trở lên tại doanh nghiệp "
            "sẽ được cộng thêm một ngày phép cho mỗi chu kỳ năm năm."
        ),
        (
            "Nhân viên cần gửi đơn đăng ký nghỉ phép trên hệ thống nhân sự nội bộ trước ít nhất "
            "ba ngày làm việc để quản lý trực tiếp phê duyệt."
        ),
        (
            "Đối với kỳ nghỉ kéo dài từ năm ngày làm việc liên tiếp trở lên, người lao động phải "
            "thông báo trước tối thiểu bảy ngày và bàn giao công việc đầy đủ."
        ),
        (
            "Số ngày phép năm chưa sử dụng hết trong năm hiện tại được phép chuyển tiếp tối đa "
            "năm ngày sang quý một của năm kế tiếp theo quy định nội bộ."
        ),
        (
            "Sau ngày ba mươi mốt tháng ba hàng năm, toàn bộ số ngày phép chuyển tiếp chưa dùng "
            "hết sẽ tự động hết hiệu lực và không được quy đổi thành tiền."
        ),
        (
            "Trong trường hợp nhân viên chấm dứt hợp đồng lao động hợp pháp, bộ phận nhân sự sẽ "
            "tính toán thanh toán tiền lương cho những ngày phép năm chưa nghỉ."
        ),
        (
            "Ngày nghỉ lễ, Tết theo quy định của Bộ luật Lao động và các ngày nghỉ cuối tuần "
            "không bị trừ vào quỹ ngày phép năm của người lao động."
        ),
        (
            "Nhân viên đang trong thời gian thử việc chưa phát sinh quyền sử dụng phép năm nhưng "
            "vẫn được tích lũy một ngày phép cho mỗi tháng làm việc đủ công."
        ),
        (
            "Khi phát sinh nhu cầu nghỉ đột xuất vì lý do gia đình hoặc sức khỏe, nhân viên phải "
            "báo ngay cho trưởng nhóm trước chín giờ sáng cùng ngày."
        ),
        (
            "Quản lý trực tiếp có trách nhiệm sắp xếp nhân sự thay thế hợp lý nhằm đảm bảo tiến "
            "độ dự án không bị gián đoạn trong thời gian nhân viên nghỉ phép."
        ),
        (
            "Bộ phận nhân sự sẽ tổng hợp bảng đối soát số dư phép năm vào ngày cuối cùng mỗi "
            "tháng và gửi thông báo tự động qua email công ty cho từng cá nhân."
        ),
        (
            "Mọi sai sót liên quan đến dữ liệu chấm công hoặc số ngày phép còn lại cần được phản "
            "hồi trong vòng năm ngày làm việc kể từ ngày nhận thông báo."
        ),
        (
            "Chính sách này có hiệu lực thi hành kể từ ngày ký và thay thế hoàn toàn các hướng "
            "dẫn về nghỉ phép năm đã ban hành trong những năm trước đây."
        ),
    ]
    para = " ".join(cohesive_sentences)
    segs = segment_prose_by_information_cohesion(para, max_block_tokens=360, min_block_tokens=48)
    assert 3 <= len(segs) <= 5, f"Expected 3-5 coherent segments, got {len(segs)}"
    assert all(estimate_unicode_tokens(s) <= 360 for s in segs)
    assert " ".join(segs) == para

    # Distinct 3-topic document must still split cleanly at topic shifts
    topic_a = [
        "Chính sách nghỉ phép năm của công ty áp dụng cho toàn bộ nhân viên chính thức đã ký hợp đồng lao động.",
        "Mỗi nhân viên làm đủ mười hai tháng được hưởng tối thiểu mười hai ngày nghỉ phép có hưởng nguyên lương.",
        "Nhân viên cần gửi đơn đăng ký nghỉ phép trên hệ thống nhân sự nội bộ trước ít nhất ba ngày làm việc.",
        "Số ngày phép năm chưa sử dụng hết được chuyển tiếp tối đa năm ngày sang quý một năm kế tiếp.",
    ]
    topic_b = [
        (
            "Cụm Kubernetes sản xuất chạy trên ba vùng sẵn sàng với ingress NGINX và chứng chỉ "
            "TLS tự động từ cert-manager."
        ),
        (
            "Mỗi pod microservice trong Kubernetes phải khai báo livenessProbe, readinessProbe "
            "và giới hạn CPU cùng bộ nhớ RAM."
        ),
        (
            "Pipeline CI/CD sử dụng Helm chart và ArgoCD để triển khai phiên bản Docker image "
            "mới mà không gây gián đoạn dịch vụ."
        ),
        (
            "Prometheus và Grafana thu thập chỉ số độ trễ HTTP, tỷ lệ lỗi 5xx và tải CPU của "
            "toàn bộ node Kubernetes."
        ),
    ]
    topic_c = [
        (
            "Báo cáo tài chính kiểm toán hằng năm phải tuân thủ chuẩn mực kế toán Việt Nam VAS "
            "và quy định thuế thu nhập doanh nghiệp."
        ),
        (
            "Hóa đơn giá trị gia tăng điện tử đầu vào hợp lệ được kê khai khấu trừ thuế GTGT "
            "theo kỳ khai thuế quý."
        ),
        (
            "Chi phí khấu hao tài sản cố định được hạch toán theo phương pháp đường thẳng căn "
            "cứ vào khung thời gian sử dụng."
        ),
        (
            "Doanh nghiệp cần lưu trữ chứng từ kế toán, sổ cái tổng hợp và tờ khai quyết toán "
            "thuế tối thiểu mười năm."
        ),
    ]
    multi_doc = " ".join(topic_a + topic_b + topic_c)
    multi_segs = segment_prose_by_information_cohesion(multi_doc, max_block_tokens=360, min_block_tokens=48)
    assert len(multi_segs) == 3, f"Expected 3 topic segments, got {len(multi_segs)}"
    assert "nghỉ phép" in multi_segs[0] and "Kubernetes" not in multi_segs[0]
    assert "Kubernetes" in multi_segs[1] and "thuế" not in multi_segs[1]
    assert "kiểm toán" in multi_segs[2] and "Kubernetes" not in multi_segs[2]


def test_mineru_html_table_to_markdown_honors_colspan_and_rowspan():
    html_tbl = (
        "<table>"
        "<tr><th>Kỳ</th><th colspan='2'>Năm 2025</th><th>Ghi chú</th></tr>"
        "<tr><td rowspan='2'>Q1</td><td>Doanh thu</td><td>100 tỷ</td><td>OK</td></tr>"
        "<tr><td>Chi phí</td><td>40 tỷ</td><td>OK</td></tr>"
        "</table>"
    )
    md = _convert_html_table_to_markdown(html_tbl)
    lines = md.splitlines()
    assert len(lines) == 4
    cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    assert all(len(row) == 4 for row in cells)
    assert cells[0][0] == "Kỳ"
    assert cells[0][1] == "Năm 2025"
    assert cells[0][3] == "Ghi chú"
    assert cells[2] == ["Q1", "Doanh thu", "100 tỷ", "OK"]
    assert cells[3] == ["Q1", "Chi phí", "40 tỷ", "OK"]

    # Non-table text passthrough & bounded rowspan/colspan non-duplication
    assert _convert_html_table_to_markdown("  plain text  ") == "plain text"
    html_span_num = (
        "<table>"
        "<tr><th>Item</th><th>A</th><th>B</th></tr>"
        "<tr><td rowspan='99'>Total</td><td colspan='2'>500 tỷ</td></tr>"
        "</table>"
    )
    md2 = _convert_html_table_to_markdown(html_span_num)
    lines2 = md2.splitlines()
    assert len(lines2) == 3
    row_total = [c.strip() for c in lines2[2].strip().strip("|").split("|")]
    assert row_total == ["Total", "500 tỷ", ""]


@pytest.mark.asyncio
async def test_qdrant_store_query_sort_places_none_last_in_asc_and_desc():
    points = [
        {"id": "p-none-1", "payload": {"_sag_id": "rec-none-1", "priority": None, "seq": 2}},
        {"id": "p-low", "payload": {"_sag_id": "rec-low", "priority": 10, "seq": 3}},
        {"id": "p-none-2", "payload": {"_sag_id": "rec-none-2", "seq": 1}},
        {"id": "p-high", "payload": {"_sag_id": "rec-high", "priority": 30, "seq": 1}},
        {"id": "p-mid", "payload": {"_sag_id": "rec-mid", "priority": 20, "seq": 2}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/scroll"):
            return httpx.Response(200, json={"result": {"points": points, "next_page_offset": None}})
        return httpx.Response(200, json={"result": {}})

    store = QdrantVectorStore(QdrantVectorConfig(url="http://qdrant.local"))
    store._client = httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    )
    try:
        desc_hits = await store.query(
            "test_col",
            VectorQuery(sort=(("priority", "desc"), ("seq", "asc")), limit=10),
        )
        assert [h.id for h in desc_hits] == [
            "rec-high",
            "rec-mid",
            "rec-low",
            "rec-none-2",
            "rec-none-1",
        ]

        asc_hits = await store.query(
            "test_col",
            VectorQuery(sort=(("priority", "asc"), ("seq", "asc")), limit=10),
        )
        assert [h.id for h in asc_hits] == [
            "rec-low",
            "rec-mid",
            "rec-high",
            "rec-none-2",
            "rec-none-1",
        ]
    finally:
        await store._client.aclose()
