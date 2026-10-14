"""Stable ownership for conversations shared through the Continuum gateway."""

import hashlib
import json

from fastapi import Depends

from sag_api.core.deps import get_current_user
from sag_api.core.principal_assertion import VerifiedPrincipal, require_principal_assertion
from sag_api.db.models import User


def conversation_owner_key(user_id: str, principal: VerifiedPrincipal) -> str:
    identity = [user_id, principal.issuer, principal.organization_id, principal.tenant_id, principal.subject]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


async def get_conversation_owner(
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
) -> str:
    return conversation_owner_key(user.id, principal)
