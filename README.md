<div align="center">

# fastapi-faults

**Typed error contracts for FastAPI — from Python exceptions to RFC 9457 and OpenAPI.**

[![CI](https://github.com/mathisarends/fastapi_faults/actions/workflows/ci.yml/badge.svg)](https://github.com/mathisarends/fastapi_faults/actions/workflows/ci.yml)
[![Python 3.12–3.14](https://img.shields.io/badge/python-3.12%E2%80%933.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![RFC 9457](https://img.shields.io/badge/Problem_Details-RFC_9457-5A45FF)](https://www.rfc-editor.org/rfc/rfc9457)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

`fastapi-faults` gives every application error one immutable definition and
uses it everywhere: exception handling, `application/problem+json` responses,
and generated OpenAPI documentation.

```text
domain exception  ──▶  Fault  ──▶  runtime response
                              └──▶  OpenAPI schema
```

No duplicated `responses={...}` dictionaries, no process-global registry, and
no drift between what an endpoint documents and what it actually returns.

> [!IMPORTANT]
> The project is currently **pre-release**. The public contract is taking shape,
> but breaking changes are still possible before 1.0.

## Why fastapi-faults?

- **One source of truth** — status, stable code, title, detail, headers, examples,
  and schemas live in one `Fault`.
- **Real Problem Details** — errors use the RFC 9457 media type and structure.
- **OpenAPI that stays honest** — `raises=[...]` produces the matching response
  documentation automatically.
- **Typed extension members** — Pydantic models validate custom problem fields
  and generate their schemas.
- **Feature-local design** — define faults beside a feature, then compose
  registries at the application boundary.
- **Safe defaults** — request validation, FastAPI HTTP errors, and unexpected
  failures can be normalized without exposing private inputs or internals.
- **Contract testing included** — assert response shape, OpenAPI coverage, and
  undeclared runtime faults.

## Quickstart

Install the current development version from GitHub:

```console
uv add "fastapi-faults @ git+https://github.com/mathisarends/fastapi_faults.git"
```

Or with `pip`:

```console
python -m pip install "fastapi-faults @ git+https://github.com/mathisarends/fastapi_faults.git"
```

Define a domain exception, map it once, and declare it on the route that can
raise it:

```python
from fastapi import FastAPI
from fastapi_faults import Fault, FaultRegistry


class SessionNotFound(Exception):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
    detail=lambda error: f"Session {error.session_id} does not exist.",
)

session_faults = FaultRegistry(
    name="sessions",
    faults=[SESSION_NOT_FOUND],
)
router = session_faults.router(prefix="/sessions", tags=["sessions"])


@router.get("/{session_id}", raises=[SESSION_NOT_FOUND])
async def get_session(session_id: str) -> dict[str, str]:
    raise SessionNotFound(session_id)


app = FastAPI()
app.include_router(router)

api_faults = FaultRegistry.merge(
    session_faults,
    name="api",
    type_base="https://api.example.com/problems",
)
api_faults.install(app)
```

A request to `GET /sessions/abc` now returns:

```http
HTTP/1.1 404 Not Found
content-type: application/problem+json
```

```json
{
  "type": "https://api.example.com/problems/session_not_found",
  "title": "Session not found",
  "status": 404,
  "code": "session_not_found",
  "detail": "Session abc does not exist."
}
```

The same route is documented in OpenAPI with a `404` response using
`application/problem+json` and a reusable `SessionNotFoundProblem` schema.

## How it fits together

### Keep errors close to their feature

Each feature owns a small registry. The application composes them explicitly:

```python
api_faults = FaultRegistry.merge(
    browser_faults,
    session_faults,
    account_faults,
    name="api",
    type_base="https://api.example.com/problems",
)
```

Definitions are immutable, merge order is deterministic, and conflicting
exception classes, codes, type URIs, or schema names fail during configuration.

`type_base` derives a stable problem `type` URI from each fault's `code`. A
fault can instead provide an explicit `type`. Installation fails when a domain
fault has neither, keeping incomplete contracts out of a running application.

### Add typed problem fields

RFC 9457 allows problem-specific extension members at the top level. Use a
Pydantic model to validate those values and describe them in OpenAPI:

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

This produces a top-level `current_version` member at runtime and an `integer`
property in the generated problem schema.

### Declare shared router faults once

Faults that apply to every operation in a feature can be router defaults:

```python
browser_router = browser_faults.router(
    prefix="/browsers/{browser_id}",
    raises=[BROWSER_NOT_FOUND],
)


@browser_router.get("/state", raises=[BROWSER_STATE_UNAVAILABLE])
async def get_state(browser_id: str) -> BrowserState:
    ...
```

Router defaults and operation faults are combined in outer-to-inner order and
deduplicated by identity. Every declared fault must belong to the registry
installed on the application.

Already using FastAPI's standard `APIRouter`? Generate just the response
metadata as an interoperability escape hatch:

```python
@router.get(
    "/{session_id}",
    responses=session_faults.responses(SESSION_NOT_FOUND),
)
async def get_session(session_id: str) -> SessionView:
    ...
```

## Framework errors and safe fallbacks

Installing a registry normalizes FastAPI and Starlette failures by default:

| Failure | Default behavior |
| --- | --- |
| Request validation | `422` Problem Details with stable, location-aware errors |
| `HTTPException` / routing errors | Matching Problem Details response |
| Response validation | Safe internal-error response |
| Unexpected exception | Safe internal-error response |

Built-in handlers can be selected at installation time:

```python
api_faults.install(
    app,
    include_validation_error=True,
    include_http_exceptions=True,
    include_unhandled_error=True,
)
```

## Testing contracts

Optional helpers make runtime and documentation drift visible in tests:

```python
from fastapi_faults.testing import (
    assert_no_undeclared_faults,
    assert_openapi_contract,
    assert_problem,
)


assert_openapi_contract(app)

response = client.get("/sessions/abc")
problem = assert_problem(
    response,
    SESSION_NOT_FOUND,
    type_uri="https://api.example.com/problems/session_not_found",
)
assert problem["detail"] == "Session abc does not exist."

async with assert_no_undeclared_faults(app):
    # Exercise routes here. The context fails afterward if a registered fault
    # occurred without being declared in that operation's raises list.
    ...
```

The undeclared-fault monitor must be entered before the application's first
request.

## WebSockets

WebSocket endpoints distinguish failures during the HTTP handshake from close
frames after the connection has been accepted:

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

See [WebSocket support and caveats](WEBSOCKETS.md) for denial responses, close
codes, reason-length limits, and the current documentation boundary.

## Requirements

- CPython 3.12, 3.13, or 3.14
- FastAPI 0.115 or newer (below 1.0)
- Pydantic 2.9 or newer (below 3.0)

## Development

Clone the repository, then install all dependency groups:

```console
uv sync --all-groups
```

Run the same quality gates used by the project:

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

The runnable sample lives in [`examples/minimal`](examples/minimal). The full
public and behavioral contract is documented in [SPEC.md](SPEC.md), release
progress in [MISSING.md](MISSING.md), and notable changes in
[CHANGELOG.md](CHANGELOG.md).

## License

Released under the [MIT License](LICENSE).
