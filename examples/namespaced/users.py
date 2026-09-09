from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from fastapi_faults import Fault, FaultRegistry

KNOWN_USER_ID = UUID("0198e2ef-799c-765d-9ab4-2130d11d1e70")


class UserNotFound(Exception):
    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id


class UserView(BaseModel):
    id: UUID
    display_name: str


USER_NOT_FOUND = Fault(
    UserNotFound,
    status=404,
    code="user_not_found",
    title="User not found",
    detail=lambda error: f"User {error.user_id} does not exist.",
)

# The feature owns both its HTTP routes and its error vocabulary.
faults = FaultRegistry(name="users", faults=[USER_NOT_FOUND])
router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/{user_id}",
    response_model=UserView,
    responses=faults.responses(USER_NOT_FOUND),
)
async def get_user(user_id: UUID) -> UserView:
    if user_id != KNOWN_USER_ID:
        raise UserNotFound(user_id)
    return UserView(id=user_id, display_name="Ada")
