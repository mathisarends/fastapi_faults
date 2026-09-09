from fastapi import APIRouter

from fastapi_faults import FaultRegistry

from . import sessions, users

# This is the public API namespace. Features do not need to know its URL prefix.
router = APIRouter(prefix="/api/v1")
router.include_router(users.router)
router.include_router(sessions.router)

# Merging retains declaration order and the original feature names for diagnostics.
faults = FaultRegistry.merge(users.faults, sessions.faults, name="api-v1")
