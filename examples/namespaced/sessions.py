from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from fastapi_faults import Fault, FaultRegistry

ACTIVE_SESSION_ID = UUID("0198e2ef-799c-765d-9ab4-2130d11d1e71")
EXPIRED_SESSION_ID = UUID("0198e2ef-799c-765d-9ab4-2130d11d1e72")


class SessionNotFound(Exception):
    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id


class SessionExpired(Exception):
    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id


class SessionView(BaseModel):
    id: UUID
    user_id: UUID
    created_at: datetime


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
    detail=lambda error: f"Session {error.session_id} does not exist.",
)
SESSION_EXPIRED = Fault(
    SessionExpired,
    status=410,
    code="session_expired",
    title="Session expired",
    detail=lambda error: f"Session {error.session_id} has expired.",
)

faults = FaultRegistry(
    name="sessions",
    faults=[SESSION_NOT_FOUND, SESSION_EXPIRED],
)
router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get(
    "/{session_id}",
    response_model=SessionView,
    responses=faults.responses(SESSION_NOT_FOUND, SESSION_EXPIRED),
)
async def get_session(session_id: UUID) -> SessionView:
    if session_id == EXPIRED_SESSION_ID:
        raise SessionExpired(session_id)
    if session_id != ACTIVE_SESSION_ID:
        raise SessionNotFound(session_id)
    return SessionView(
        id=session_id,
        user_id=UUID("0198e2ef-799c-765d-9ab4-2130d11d1e70"),
        created_at=datetime(2026, 9, 9, 8, 0, tzinfo=UTC),
    )
