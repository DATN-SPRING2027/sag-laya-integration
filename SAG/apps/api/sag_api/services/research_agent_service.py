"""Phase 10 & Checkpoint D Bounded Research Agent execution service.

Implements Discovery/Fetch with SSRF/allowlist enforcement, duplicate guards,
budget caps, no-new-information early termination, and relay into normal ingestion.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.connectors.web import extract_web_markdown, extract_web_title
from sag_api.core.logging import get_logger
from sag_api.db.models import (
    IngestionRun,
    ResearchTask,
)
from sag_api.jobs import JobQueue
from sag_api.services.document_service import handle_document_upload
from sag_api.services.incremental_tree_service import (
    IncrementalTreeUpdateResult,
    update_tree_incrementally,
)
from sag_api.services.knowledge_gap_question_service import (
    PhraseAdapter,
    generate_question_for_gap,
)
from sag_api.services.research_planner_service import (
    DEFAULT_MIN_PRIORITY,
    ResearchBudget,
    ResearchPolicy,
    ResearchTaskStatus,
    create_or_get_research_task,
    detect_eligible_gaps,
    is_url_allowed,
    normalize_url,
    plan_research_task,
)
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    RoutingSnapshot,
    TreeBuildConfig,
)
from sag_api.tools.builtin import _validated_public_web_url

log = get_logger("research.agent")

_USER_AGENT = "sag-research-bot/0.1 (+https://github.com/Zleap-AI/SAG)"


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    url: str
    success: bool
    status_code: int | None = None
    content_bytes: bytes = b""
    payload_hash: str = ""
    title: str = ""
    markdown: str = ""
    error: str | None = None
    retry_count: int = 0
    is_duplicate: bool = False
    is_no_info: bool = False


FetchAdapter = Callable[
    [str, ResearchPolicy, ResearchBudget],
    Awaitable[FetchOutcome],
]


async def fetch_url_bounded(
    url: str,
    policy: ResearchPolicy,
    budget: ResearchBudget,
    *,
    client: httpx.AsyncClient | None = None,
) -> FetchOutcome:
    """Fetch URL with SSRF guards, allowlist enforcement, stream bounds and retries."""
    if not is_url_allowed(url, policy):
        return FetchOutcome(
            url=url,
            success=False,
            error=f"URL is not in allowed domains: {url}",
        )

    try:
        validated_url = await _validated_public_web_url(url)
    except RuntimeError as err:
        return FetchOutcome(
            url=url,
            success=False,
            error=f"SSRF or invalid URL guard rejected address: {err}",
        )

    retries = 0
    last_error: str | None = None
    close_client = False
    active_client = client
    if active_client is None:
        active_client = httpx.AsyncClient(
            timeout=budget.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        close_client = True

    try:
        for attempt in range(budget.max_retries + 1):
            if attempt > 0:
                retries += 1
                await asyncio.sleep(0.1)

            try:
                response = await active_client.get(validated_url)
                if response.status_code >= 400:
                    last_error = f"HTTP status error: {response.status_code}"
                    if response.status_code in {401, 403, 404, 410}:
                        # Non-retryable HTTP client errors
                        break
                    continue

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    remaining = budget.max_bytes_per_page - size
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    size += min(len(chunk), remaining)

                content_bytes = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                html_text = content_bytes.decode(encoding, errors="replace")

                markdown_body = extract_web_markdown(html_text)
                if not markdown_body or not markdown_body.strip():
                    return FetchOutcome(
                        url=url,
                        success=True,
                        status_code=response.status_code,
                        content_bytes=content_bytes,
                        is_no_info=True,
                        retry_count=retries,
                    )

                title = extract_web_title(html_text) or urlsplit(url).netloc
                full_markdown = f"# {title}\n\n> Source: {url}\n\n{markdown_body}\n"
                payload_hash = hashlib.sha256(full_markdown.encode("utf-8")).hexdigest()

                return FetchOutcome(
                    url=url,
                    success=True,
                    status_code=response.status_code,
                    content_bytes=content_bytes,
                    payload_hash=payload_hash,
                    title=title,
                    markdown=full_markdown,
                    retry_count=retries,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as req_err:
                last_error = f"Network or timeout error: {req_err}"
            except Exception as unk_err:  # noqa: BLE001
                last_error = f"Fetch failed: {unk_err}"
                break
    finally:
        if close_client:
            await active_client.aclose()

    return FetchOutcome(
        url=url,
        success=False,
        error=last_error or "Fetch failed after retries",
        retry_count=retries,
    )


async def execute_research_task(
    session: AsyncSession,
    task_id: str,
    *,
    job_queue: JobQueue | None = None,
    fetch_adapter: FetchAdapter | None = None,
) -> tuple[ResearchTask, list[FetchOutcome]]:
    """Execute a persisted research task: fetch targets, enforce guards, and relay to normal upload."""
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise ValueError(f"Research task {task_id} not found")

    task.status = ResearchTaskStatus.FETCHING.value
    await session.flush()

    policy = ResearchPolicy(**task.policy_json)
    budget = ResearchBudget(**task.budget_json)
    fetcher = fetch_adapter or fetch_url_bounded

    target_urls: list[str] = list(task.target_urls_json)
    outcomes: list[FetchOutcome] = []
    seen_payload_hashes: set[str] = set()
    ingested_run_ids: list[str] = list(task.ingestion_run_ids_json)
    ingested_version_ids: list[str] = list(task.document_version_ids_json)
    has_new_information = False

    for url in target_urls:
        if task.fetch_count >= budget.max_fetches:
            log.info("Task %s reached max_fetches budget cap (%s)", task.id, budget.max_fetches)
            break

        outcome = await fetcher(url, policy, budget)
        task.fetch_count += 1
        task.retry_count += outcome.retry_count

        if not outcome.success or outcome.is_no_info or not outcome.payload_hash:
            outcomes.append(outcome)
            continue

        # Check duplicate within this task execution
        if outcome.payload_hash in seen_payload_hashes:
            outcome = FetchOutcome(
                url=outcome.url,
                success=True,
                status_code=outcome.status_code,
                content_bytes=outcome.content_bytes,
                payload_hash=outcome.payload_hash,
                title=outcome.title,
                markdown=outcome.markdown,
                retry_count=outcome.retry_count,
                is_duplicate=True,
            )
            outcomes.append(outcome)
            continue

        # Check duplicate against existing IngestionRuns in this tenant/project
        existing_run_stmt = select(IngestionRun.id).where(
            IngestionRun.tenant_id == task.tenant_id,
            IngestionRun.project_id == task.project_id,
            IngestionRun.payload_hash == outcome.payload_hash,
        )
        existing_run_id = (await session.execute(existing_run_stmt)).scalar_one_or_none()
        if existing_run_id is not None:
            outcome = FetchOutcome(
                url=outcome.url,
                success=True,
                status_code=outcome.status_code,
                content_bytes=outcome.content_bytes,
                payload_hash=outcome.payload_hash,
                title=outcome.title,
                markdown=outcome.markdown,
                retry_count=outcome.retry_count,
                is_duplicate=True,
            )
            outcomes.append(outcome)
            continue

        # We have verified new, non-duplicate information
        seen_payload_hashes.add(outcome.payload_hash)
        has_new_information = True
        outcomes.append(outcome)

        # Relay to Normal Ingestion Pipeline via handle_document_upload
        client_token = f"research-task-{task.id}-{outcome.payload_hash[:16]}"
        file_bytes = outcome.markdown.encode("utf-8")
        safe_netloc = urlsplit(url).netloc.replace(":", "-").replace("/", "-") or "web"
        original_filename = f"research_{safe_netloc}_{task.node_id}_{outcome.payload_hash[:8]}.md"

        upload_res = await handle_document_upload(
            session,
            tenant_id=task.tenant_id,
            project_id=task.project_id,
            owner_id=f"research-agent:{task.id}",
            security_partition_id=task.security_partition_id,
            client_token=client_token,
            file_bytes=file_bytes,
            original_filename=original_filename,
            content_type="text/markdown",
            logical_source_id=f"web:{normalize_url(url)}",
            job_queue=job_queue,
        )

        if upload_res.is_duplicate:
            outcome = FetchOutcome(
                url=outcome.url,
                success=True,
                status_code=outcome.status_code,
                content_bytes=outcome.content_bytes,
                payload_hash=outcome.payload_hash,
                title=outcome.title,
                markdown=outcome.markdown,
                retry_count=outcome.retry_count,
                is_duplicate=True,
            )
            continue

        if upload_res.run_id and upload_res.run_id not in ingested_run_ids:
            ingested_run_ids.append(upload_res.run_id)
        if upload_res.version_id and upload_res.version_id not in ingested_version_ids:
            ingested_version_ids.append(upload_res.version_id)

    task.ingestion_run_ids_json = ingested_run_ids
    task.document_version_ids_json = ingested_version_ids

    # Stop conditions & lifecycle state finalization
    if not has_new_information:
        # All URLs yielded duplicates, empty content, or errors -> terminate cleanly without crawl loop
        task.status = ResearchTaskStatus.STOPPED_NO_NEW_INFO.value
    else:
        task.status = ResearchTaskStatus.COMPLETED.value

    await session.flush()
    return task, outcomes


async def run_bounded_research_flow(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str,
    security_partition_id: str,
    tree_version: str,
    candidate_urls: Sequence[str],
    policy: ResearchPolicy | None = None,
    budget: ResearchBudget | None = None,
    min_priority: float = DEFAULT_MIN_PRIORITY,
    phrase_adapter: PhraseAdapter | None = None,
    fetch_adapter: FetchAdapter | None = None,
    job_queue: JobQueue | None = None,
) -> tuple[ResearchTask | None, list[FetchOutcome]]:
    """Run full bounded research workflow: Gap -> Question -> Plan -> Fetch -> Ingestion."""
    eligible_gaps = await detect_eligible_gaps(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        security_partition_id=security_partition_id,
        tree_version=tree_version,
        min_priority=min_priority,
        limit=1,
    )
    if not eligible_gaps:
        return None, []

    gap, _ = eligible_gaps[0]

    # Generate or reuse grounded question from Phase 9
    question = await generate_question_for_gap(
        session,
        gap.id,
        phrase=phrase_adapter,
    )

    plan = plan_research_task(
        gap,
        question,
        tenant_id=tenant_id,
        project_id=project_id,
        security_partition_id=security_partition_id,
        tree_version=tree_version,
        policy=policy,
        budget=budget,
        candidate_urls=candidate_urls,
    )

    task, _ = await create_or_get_research_task(session, plan)
    task, outcomes = await execute_research_task(
        session,
        task.id,
        job_queue=job_queue,
        fetch_adapter=fetch_adapter,
    )
    return task, outcomes


def apply_incremental_tree_update_for_research(
    base_snapshot: RoutingSnapshot,
    delta_units: Sequence[KnowledgeUnitInput],
    delta_edges: Sequence[KnowledgeEdgeInput] = (),
    config: TreeBuildConfig | None = None,
) -> IncrementalTreeUpdateResult:
    """Connect new research units into Knowledge Tree via standard delta update."""
    return update_tree_incrementally(
        base_snapshot,
        list(delta_units),
        list(delta_edges),
        config=config,
    )
