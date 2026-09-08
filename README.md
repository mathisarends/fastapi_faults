# fastapi-faults

Typed RFC 9457 Problem Details for FastAPI, with one definition for runtime
handling and OpenAPI documentation.

```python
from fastapi import FastAPI
from fastapi_faults import Fault, FaultRegistry


class SessionNotFound(Exception):
    pass


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
)

session_faults = FaultRegistry(name="sessions", faults=[SESSION_NOT_FOUND])
router = session_faults.router(prefix="/sessions")


@router.get("/{session_id}", raises=[SESSION_NOT_FOUND])
async def get_session(session_id: str) -> dict[str, str]:
    raise SessionNotFound


app = FastAPI()
app.include_router(router)

faults = FaultRegistry.merge(
    session_faults,
    name="api",
    type_base="https://api.example.com/problems",
)
faults.install(app)
```

The route produces an RFC 9457 response at runtime and documents the same
contract as `application/problem+json` in OpenAPI:

```json
{
  "type": "https://api.example.com/problems/session_not_found",
  "title": "Session not found",
  "status": 404,
  "code": "session_not_found"
}
```

## Installation

The package requires CPython 3.12, 3.13, or 3.14, FastAPI, and Pydantic v2.

```console
uv add fastapi-faults
```

The project is still pre-release. Install from the repository until a package
release is published.

## Feature registries

Keep fault definitions next to their feature and compose complete registries at
the application boundary:

```python
api_faults = FaultRegistry.merge(
    browser_faults,
    session_faults,
    account_faults,
    name="api",
    type_base="https://api.example.com/problems",
)
```

No iterable unpacking or process-global registration is needed. Definitions
are immutable, merge order is deterministic, and collisions fail during
configuration.

`type_base` derives a stable RFC 9457 `type` for faults that do not define one
explicitly. It should resolve to documentation in a public API, but this
library does not host those pages. It can be omitted from a feature registry
and supplied when registries are merged. Installation fails if a domain fault
still has no resolved type URI. The default validation and internal-error
normalizers also require it.

## Typed extension members

Problem-specific fields remain top-level RFC 9457 extension members. A Pydantic
model validates runtime values and supplies their OpenAPI schema:

```python
from pydantic import BaseModel


class ConflictFields(BaseModel):
    current_version: int


SESSION_CONFLICT = Fault(
    SessionConflict,
    status=409,
    code="session_conflict",
    title="Session conflict",
    extensions_model=ConflictFields,
    extensions=lambda error: ConflictFields(current_version=error.version),
)
```

## Router defaults

Feature-wide errors can be declared once:

```python
browser_router = browser_faults.router(
    prefix="/browser/{browser_id}",
    raises=[BROWSER_NOT_FOUND],
)


@browser_router.get("/state", raises=[BROWSER_STATE_UNAVAILABLE])
async def get_state(browser_id: str) -> BrowserState:
    ...
```

Router defaults and operation faults are unioned in outer-to-inner order with
identity-based deduplication. Every included feature definition must be present
in the registry installed on the application.

For a stock `APIRouter`, use the interoperability escape hatch:

```python
@router.get(
    "/{session_id}",
    responses=session_faults.responses(SESSION_NOT_FOUND),
)
async def get_session(session_id: str) -> SessionView:
    ...
```

## WebSockets

WebSockets distinguish HTTP handshake failures from accepted-connection close
frames:

```python
@router.websocket(
    "/{session_id}/events",
    handshake_raises=[SESSION_NOT_FOUND],
    closes=[SESSION_EXPIRED_WS],
)
async def events(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    ...
```

See [WEBSOCKETS.md](WEBSOCKETS.md) for close-code, denial-extension, reason
length, and documentation caveats.

## Testing

The optional helpers keep tests concise without changing production behavior:

```python
from fastapi_faults.testing import (
    assert_no_undeclared_faults,
    assert_openapi_contract,
    assert_problem,
)

assert_openapi_contract(app)
assert_problem(response, SESSION_NOT_FOUND, type_uri=expected_type)

async with assert_no_undeclared_faults(app):
    ...
```

The undeclared-fault monitor must be entered before the application's first
request. Registered errors are still rendered safely; the context fails the
test afterward if an operation omitted the corresponding `raises` declaration.

## Development

```console
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

The complete public and behavioral contract is in [SPEC.md](SPEC.md). Remaining
release work is tracked in [MISSING.md](MISSING.md).

## License

Released under the [MIT License](LICENSE).
