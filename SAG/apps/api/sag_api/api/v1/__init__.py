from fastapi import APIRouter

from sag_api.api.v1 import (
    activity,
    agents,
    answer_actions,
    attachments,
    auth,
    decision,
    dify,
    documents,
    handover,
    insights,
    jobs,
    knowledge,
    octx,
    openai,
    search,
    sources,
    system,
    universe,
)

api_router = APIRouter(prefix="/api/v1")
for _module in (
    auth,
    dify,
    sources,
    documents,
    insights,
    knowledge,
    jobs,
    search,
    agents,
    openai,
    activity,
    attachments,
    system,
    universe,
    octx,
    decision,
):
    api_router.include_router(_module.router)
api_router.include_router(documents.project_router)
api_router.include_router(handover.router)
api_router.include_router(answer_actions.project_share_router)
api_router.include_router(answer_actions.platform_feedback_router)
api_router.include_router(search.global_router)

__all__ = ["api_router"]
