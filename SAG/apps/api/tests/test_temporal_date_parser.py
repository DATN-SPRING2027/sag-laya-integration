"""TDD unit & ReDoS tests for VI/EN deterministic as_of date parser and plan_query propagation."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from sag_api.services.query_strategy_planner import plan_query
from sag_api.services.retrieval_plan import (
    build_retrieval_plan,
    rebuild_retrieval_plan_with_top_k,
)
from sag_api.services.temporal_date_parser import extract_as_of_datetime


@pytest.mark.parametrize(
    "query",
    [
        "Chính sách ngày 2025-06-15 là gì?",
        "Policy effective 2025-06-15.",
        "Chính sách ngày 15/06/2025 là gì?",
        "Chính sách áp dụng ngày 15/06/2025.",
        "Quy định áp dụng 15-06-2025 có gì mới?",
        "Áp dụng từ 15.06.2025.",
        "Quy định ngày 15 tháng 6 năm 2025",
        "ngay 15 thang 06 nam 2025",
        "What was the rule on June 15, 2025?",
        "Policy on 15 Jun 2025",
        "Policy on 15th of June 2025",
        "Policy on Jun. 15th, 2025?",
    ],
)
def test_tier1_exact_full_dates_return_utc_end_of_day(query: str) -> None:
    expected = datetime(2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC)
    assert extract_as_of_datetime(query) == expected


def test_tier1_vn_single_digit_day_and_month() -> None:
    expected = datetime(2025, 6, 5, 23, 59, 59, 999999, tzinfo=UTC)
    assert extract_as_of_datetime("ngày 5 tháng 6 năm 2025") == expected
    assert extract_as_of_datetime("ngay 5/6/2025") == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Báo cáo quý 2/2025", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Chính sách quý 2 năm 2025", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("quy 2-2025", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy in Q1 2025", datetime(2025, 3, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Roadmap Q2/2025", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Budget for 2024-Q4", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
    ],
)
def test_tier2_quarter_year(query: str, expected: datetime) -> None:
    assert extract_as_of_datetime(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Chính sách tháng 6/2025", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("tháng 2 năm 2024", datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=UTC)),
        ("tháng 2 năm 2025", datetime(2025, 2, 28, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Báo cáo 06/2025", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Metrics for 2025-06", datetime(2025, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Rules in Jan 2025", datetime(2025, 1, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("What changed in June 2024?", datetime(2024, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)),
    ],
)
def test_tier3_month_year_including_leap_year(query: str, expected: datetime) -> None:
    assert extract_as_of_datetime(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Chính sách năm 2024 là gì?", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("What was the policy in 2024?", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Rules of 2023", datetime(2023, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy as of 2024", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Quy định tính đến 2024", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        (
            "Quy định năm 2024 người lao động được nghỉ mấy ngày?",
            datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        ),
    ],
)
def test_tier4_cued_years(query: str, expected: datetime) -> None:
    assert extract_as_of_datetime(query) == expected


def test_tier5_relative_expressions_anchored_to_reference_time() -> None:
    # Wednesday 2025-06-18T14:00:00Z
    ref = datetime(2025, 6, 18, 14, 0, 0, tzinfo=UTC)
    assert extract_as_of_datetime("Tình trạng hôm qua thế nào?", reference_time=ref) == datetime(
        2025, 6, 17, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("What happened yesterday?", reference_time=ref) == datetime(
        2025, 6, 17, 23, 59, 59, 999999, tzinfo=UTC
    )
    # Previous ISO week's Sunday for Wed 2025-06-18 is Sun 2025-06-15
    assert extract_as_of_datetime("Báo cáo tuần trước", reference_time=ref) == datetime(
        2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("Report from last week", reference_time=ref) == datetime(
        2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC
    )
    # Previous month & quarter crossing Jan -> Dec / Q1 -> Q4 boundary
    jan_ref = datetime(2025, 1, 10, 8, 30, 0)  # naive ref must be coerced to UTC
    assert extract_as_of_datetime("Chính sách tháng trước", reference_time=jan_ref) == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("Policy last month", reference_time=jan_ref) == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("Kết quả quý trước", reference_time=jan_ref) == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("Results from last quarter", reference_time=jan_ref) == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("Quy định năm ngoái", reference_time=ref) == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert extract_as_of_datetime("Rules from last year", reference_time=ref) == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Chính sách trước ngày 15/06/2025", datetime(2025, 6, 14, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy before 2025-06-15", datetime(2025, 6, 14, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy prior to June 15, 2025", datetime(2025, 6, 14, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Quy định trước tháng 6/2025", datetime(2025, 5, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy before June 2025", datetime(2025, 5, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Chính sách trước quý 2/2025", datetime(2025, 3, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy before Q2 2025", datetime(2025, 3, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Chính sách trước năm 2025", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy before 2025", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Policy prior to 2025", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Chính sách trước 2025", datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
    ],
)
def test_exclusive_before_boundary_semantics(query: str, expected: datetime) -> None:
    assert extract_as_of_datetime(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "1000",
        "2025",
        "2025 VNĐ",
        "Phí dịch vụ in 2025 VNĐ",
        "5000 USD",
        "Budget of 2025 USD",
        "Quota of 2024 MB",
        "Limit of 2024 users",
        "Nghị định 05/2024/NĐ-CP",
        "Thông tư 06/2025/TT-BTC",
        "Quyết định số 05/2024",
        "Service listening on port 2024",
        "số điện thoại 0901202512",
        "Hotline 0901-2025-123",
        "See PR-2024 for details",
        "31/02/2025",
        "30/02/2024",
        "ngày 31 tháng 2 năm 2025",
        "February 30, 2025",
        "15/06-2025",
        "Version 1.15.06.2025",
        "Version 15.06.2025.1",
    ],
)
def test_false_positive_rejections_return_none(query: str) -> None:
    assert extract_as_of_datetime(query) is None, f"Unexpected date parsed from {query!r}"


def test_redos_latency_guard_under_10ms_on_4000_char_adversarial_inputs() -> None:
    adversarial_inputs = [
        "quý 1" + (" " * 3990) + "x",
        "tháng 6" + (" " * 3990) + "x",
        ("trước " * 670)[:4000],
        ("Jan 1, " * 575)[:4000],
        ("05/2024/" * 500)[:4000],
        ("15/06/2025/NĐ-CP " * 250)[:4000],
    ]
    for payload in adversarial_inputs:
        t0 = time.perf_counter()
        result = extract_as_of_datetime(payload)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert result is None
        assert elapsed_ms < 10.0, f"Parser exceeded 10ms ({elapsed_ms:.2f}ms) on adversarial input"


def test_plan_query_as_of_gating_and_route_decision_query_fallback() -> None:
    # 1. past_point extracts as_of directly from query
    p_past = plan_query(
        "Chính sách nghỉ phép tháng 6/2024 là gì?",
        temporal_mode="past_point",
    )
    assert p_past.retrieval_plan.temporal_mode == "past_point"
    assert p_past.retrieval_plan.as_of == datetime(2024, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)
    assert "PARSED_PAST_POINT_AS_OF" in p_past.reason_codes
    trace = p_past.retrieval_plan.as_trace()
    assert trace["temporal_mode"] == "past_point"
    assert trace["as_of"] == "2024-06-30T23:59:59.999999+00:00"

    # 2. Agent tool reformulation fallback: tool query omits date, route_decision["query"] retains it
    p_fallback_q = plan_query(
        "chính sách nghỉ phép",
        route_decision={
            "temporal_mode": "past_point",
            "query": "Chính sách nghỉ phép tháng 6/2024 là gì?",
        },
    )
    assert p_fallback_q.retrieval_plan.as_of == datetime(
        2024, 6, 30, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert "PARSED_PAST_POINT_AS_OF" in p_fallback_q.reason_codes

    # 3. Unparseable past_point keeps as_of=None and records fallback reason
    p_unparsed = plan_query("Chính sách hồi trước là gì?", temporal_mode="past_point")
    assert p_unparsed.retrieval_plan.as_of is None
    assert "PAST_POINT_UNPARSED_FALLBACK" in p_unparsed.reason_codes

    # 4. Non-past_point modes NEVER parse as_of implicitly
    for mode in ("current_effective", "atemporal", "change_lineage"):
        p_other = plan_query("Chính sách ngày 15/06/2025 có gì thay đổi?", temporal_mode=mode)
        assert p_other.retrieval_plan.as_of is None
        assert p_other.retrieval_plan.as_trace()["temporal_mode"] == mode
        assert p_other.retrieval_plan.as_trace()["as_of"] is None

    # 5. Explicit as_of overrides and normalizes naive datetime to UTC
    explicit_dt = datetime(2025, 3, 1, 12, 0, 0)
    p_explicit = plan_query("Chính sách nghỉ phép", temporal_mode="atemporal", as_of=explicit_dt)
    assert p_explicit.retrieval_plan.as_of == datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)
    assert "EXPLICIT_AS_OF" in p_explicit.reason_codes

    # 6. rebuild_retrieval_plan_with_top_k preserves temporal_mode & as_of
    base = build_retrieval_plan(
        "Chính sách ngày 15/06/2025",
        temporal_mode="past_point",
        as_of=datetime(2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC),
    )
    rebuilt = rebuild_retrieval_plan_with_top_k(base, 12)
    assert rebuilt.temporal_mode == "past_point"
    assert rebuilt.as_of == datetime(2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC)
