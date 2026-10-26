"""Authenticated, resumable per-thread index job. Defaults to read-only dry run.

Supply server-accepted tokens via private files, never command-line token values.
No owner/project override, no legacy assistant provenance promotion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from starlette.requests import Request  # noqa: E402

from sag_api.core.conversation_ownership import conversation_owner_key  # noqa: E402
from sag_api.core.db import SessionLocal  # noqa: E402
from sag_api.core.deps import get_current_user  # noqa: E402
from sag_api.core.principal_assertion import (  # noqa: E402
    principal_assertion_lifespan,
    require_project_read_principal,
)
from sag_api.services.agent_domain import get_agent, get_thread  # noqa: E402
from sag_api.services.session_context.budget import BudgetLimits, BudgetState  # noqa: E402
from sag_api.services.session_context.index import sync_index  # noqa: E402
from sag_api.services.session_context.runtime import configured_encoder  # noqa: E402
from sag_api.services.session_context.scope import eligible_message_ids, resolve_scope  # noqa: E402
from sag_api.services.session_context.storage import read_records, source_dependencies  # noqa: E402
from sag_api.services.session_context.types import SessionKey  # noqa: E402


async def backfill(session, *, agent_id, thread_id, token, assertion=None, apply=False):
    headers = [(b"authorization", ("Bearer " + token).encode())]
    if assertion:
        headers.append((b"x-sag-principal-assertion", assertion.encode()))
    request = Request({"type": "http", "headers": headers})
    user = await get_current_user(request, token=token, session=session)
    principal = await require_project_read_principal(request)
    owner = conversation_owner_key(user.id, principal)
    await get_agent(session, agent_id, owner_key=owner)
    await get_thread(session, agent_id, thread_id, owner_key=owner)
    key = SessionKey(owner, agent_id, thread_id)
    scope = await resolve_scope(
        session, key=key, principal=principal, candidate_source_ids=await source_dependencies(session, key=key)
    )
    deadline = time.monotonic() + min(180, max(0, principal.expires_at - time.time()))
    budget = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 32000000, deadline))
    async with asyncio.timeout_at(deadline):
        if not apply:
            records = await read_records(session, scope=scope, budget=budget)
            return {
                "mode": "dry_run",
                "eligible_messages": len(eligible_message_ids(scope, records)),
                "raw_history_modified": False,
            }
        encoder = await asyncio.to_thread(configured_encoder)
        indexed = await sync_index(session, scope=scope, encoder=encoder, budget=budget)
        return {
            "mode": "apply",
            "state": "ready",
            "eligible_messages": len(indexed.history.messages),
            "raw_history_modified": False,
            "index_work_units": budget.index_units,
        }


async def run(args):
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    assertion = Path(args.assertion_file).read_text(encoding="utf-8").strip() if args.assertion_file else None
    async with principal_assertion_lifespan():
        async with SessionLocal() as session:
            return await backfill(
                session,
                agent_id=args.agent_id,
                thread_id=args.thread_id,
                token=token,
                assertion=assertion,
                apply=args.apply,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--assertion-file")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args))))
    except Exception as error:
        # Error strings may include private paths, token values or SQL parameters.
        print(json.dumps({"state": "not_ready", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
