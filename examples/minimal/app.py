from uuid import UUID

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from fastapi_faults import Fault, FaultRegistry

from .domain import SessionNotFound


class SessionView(BaseModel):
    id: UUID
    name: str


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
    detail=lambda error: f"Session {error.session_id} does not exist.",
)

session_faults = FaultRegistry(name="sessions", faults=[SESSION_NOT_FOUND])
router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get(
    "/{session_id}",
    responses=session_faults.responses(SESSION_NOT_FOUND),
)
async def get_session(session_id: UUID) -> SessionView:
    raise SessionNotFound(session_id)


app = FastAPI(title="fastapi-faults minimal example")
app.include_router(router)

faults = FaultRegistry.merge(
    session_faults,
    name="api",
    type_base="https://api.example.com/problems",
)
faults.install(app)
