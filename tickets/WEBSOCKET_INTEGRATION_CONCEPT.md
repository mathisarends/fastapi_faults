# Spec: WebSockets in the Shared Error Contract

Status: implementation proposal. The APIs below describe the target state and
are not implemented yet. “LPI” means the public library API. This spec covers
the API, internal structure, export contract, and acceptance tests.

## 1. Target API and Code Structure

### 1.1 The Same Domain Error Definition for HTTP and WebSocket

```python
from fastapi import APIRouter, FastAPI, WebSocket
from pydantic import BaseModel

from fastapi_faults import Fault, FaultRegistry, WebSocketPolicy


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    def __init__(self, version: int) -> None:
        self.version = version


class ConflictFields(BaseModel):
    current_version: int


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
    websocket=WebSocketPolicy(close_code=1008),
)
SESSION_CONFLICT = Fault(
    SessionConflict,
    status=409,
    code="session_conflict",
    title="Session conflict",
    extensions_model=ConflictFields,
    extensions=lambda error: ConflictFields(current_version=error.version),
    websocket=WebSocketPolicy(recoverable=True),
)

session_faults = FaultRegistry(
    name="sessions", faults=[SESSION_NOT_FOUND, SESSION_CONFLICT]
)
router = APIRouter(prefix="/sessions")


@router.get(
    "/{session_id}",
    responses=session_faults.responses(SESSION_NOT_FOUND, SESSION_CONFLICT),
)
async def get_session(session_id: str) -> dict[str, str]:
    raise SessionNotFound()


@router.websocket("/{session_id}/events", name="session_events")
@session_faults.websocket_errors(
    SESSION_NOT_FOUND,
    SESSION_CONFLICT,
    operation_id="session_events",
)
async def session_events(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    raise SessionNotFound()


app = FastAPI()
app.include_router(router)
api_faults = FaultRegistry.merge(
    session_faults, name="api", type_base="https://api.example.com/problems"
)
api_faults.install(app, include_websockets=True)

# Explicit export for the build pipeline and client generators; no new HTTP endpoint.
websocket_contract = api_faults.websocket_schema(app)
```

`code`, `type`, title, detail, and typed extensions belong to the domain error.
HTTP status/headers and WebSocket behavior are transport mappings. A client
branches on `code`; a close code expresses only the broad reason for closing the
connection. Multiple faults may share the same HTTP status or close code.

The extension is additive: `Fault.status` remains required at this stage, even
for faults used exclusively over WebSocket. This keeps existing constructors,
`Problem`, HTTP responses, and schemas compatible. A future split into a
transport-neutral `Fault` plus `HttpPolicy` is outside this ticket's scope.

### 1.2 Recoverable Errors per Message

A global handler cannot continue an endpoint that an exception has exited.
Therefore, there is an additional local asynchronous context manager:

```python
from fastapi import WebSocketDisconnect


@router.websocket("/{session_id}/commands", name="session_commands")
@session_faults.websocket_errors(
    SESSION_CONFLICT, operation_id="session_commands"
)
async def session_commands(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    errors = api_faults.websocket(websocket)
    try:
        while True:
            # The application protocol supplies an already validated command here.
            command = await receive_command(websocket)
            async with errors.message(request_id=command.request_id):
                result = await update_session(session_id, command)
                await websocket.send_json({
                    "event": "session.updated",
                    "request_id": command.request_id,
                    "result": result,
                })
    except WebSocketDisconnect:
        return
```

`receive_command` and `update_session` stand for application code. Parsing and
validation of its own message protocol remain its responsibility; expected
parser errors are translated there to registered domain exceptions. A bare
Pydantic `ValidationError` is not automatically considered a user-input error.

The context manager suppresses only successfully sent, registered errors with
`recoverable=True`. It skips the rest of the block; the loop can then process
the next command. It performs no rollback. Terminal and unknown exceptions
propagate to close the connection; an internal scope marker takes the
`request_id` for that error and is then cleared. Successful blocks send nothing
additional.

The context manager is allowed only after `accept()` and with the registry
installed on the app. Nested or concurrent `message()` blocks on the same
connection are rejected with `FaultConfigurationError`. Independent tasks
outside the awaited endpoint invocation do not belong to this catch area.

### 1.3 Public Signatures and Fixed Defaults

```python
@dataclass(frozen=True, slots=True)
class WebSocketPolicy:
    close_code: int = 1008
    recoverable: bool = False

# New optional keyword-only field:
# Fault.websocket: WebSocketPolicy | None = None

# New/extended FaultRegistry methods:
def websocket_errors(
    self, *faults: AnyFault, operation_id: str
) -> Callable[[EndpointT], EndpointT]: ...

def websocket(self, connection: WebSocket) -> WebSocketFaultSession: ...

def websocket_schema(self, app: FastAPI) -> dict[str, JsonValue]: ...

def install(
    self,
    app: FastAPI,
    *,
    include_validation_error: bool = True,
    include_http_exceptions: bool = True,
    include_unhandled_error: bool = True,
    include_websockets: bool = False,
) -> None: ...
```

The signatures are schematic; `EndpointT` must preserve the original callable
signature for FastAPI and mypy. The decorator attaches only immutable metadata
to the function and returns the same function. It is placed immediately below
`@router.websocket`, as shown above.

- `websocket=None`: no declared WS contract for this fault. HTTP remains usable.
- `WebSocketPolicy()` means an error message followed by close `1008`.
- With `recoverable=True`, `close_code` is used only if the error occurs outside
  a `message()` block and thus ends the endpoint.
- Configurable close codes allowed in v1: `1008`, `1011`, `3000..4999`; no
  boolean values. All other values raise `FaultConfigurationError`.
- `request_id` is optional, has type `str | None`, and is omitted from JSON
  when `None`. It is not automatically derived from unvalidated messages or
  query parameters.
- `operation_id` is explicit, non-empty, and unique within the WS export.
  Decorating the same endpoint multiple times is rejected, as is including it
  multiple times with the same operation ID; use separate endpoint functions.
- New public exports: `WebSocketPolicy`, `WebSocketProblem`,
  `WebSocketErrorEvent`, `WebSocketFaultSession`.

### 1.4 Wire Format and Lifecycle

HTTP continues to return `application/problem+json`, for example status `409`
with `{type, title, status, code, current_version}` at the top level.
WebSocket returns a JSON text message in the following binding format:

```json
{
  "event": "fault",
  "request_id": "cmd-17",
  "fatal": false,
  "close_code": null,
  "problem": {
    "type": "https://api.example.com/problems/session_conflict",
    "title": "Session conflict",
    "code": "session_conflict",
    "current_version": 7
  }
}
```

`WebSocketProblem` contains required `type`, `title`, and `code`, optional
`detail`, `instance`, and the same validated extensions as HTTP. It does not
contain an HTTP `status`. This is a dedicated message format inspired by
Problem Details, not an RFC 9457 HTTP response. `status` remains reserved as an
extension. HTTP header callbacks are not run when rendering WS messages.

`WebSocketErrorEvent` forbids unknown envelope fields. `fatal` and
`close_code` are always present: `false`/`null` when continuing locally,
`true`/an allowed code when closing the connection. Schema and runtime must
enforce these combinations. For a terminal error, send one event first, then
exactly one close frame; the close reason is the stable `problem.code`, trimmed
to at most 123 UTF-8 bytes. Details and JSON do not belong in the close reason.

Built-ins use `type_base + "/" + code`: `websocket_error` has the title
`WebSocket error`, `websocket_validation_error` has the title
`WebSocket validation failed`, and `internal_server_error` has the existing
title `Internal Server Error`. Only the validation fault adds `errors` as a
required field: a list of objects with `code` (validation type), `detail`
(constant `Invalid input`), and optional `in`/`parameter` for
path/query/header/cookie, analogous to the HTTP renderer. No built-in adopts
the original exception text. Schema names are `WebSocketErrorProblem`,
`WebSocketValidationProblem`, and `WebSocketInternalErrorProblem`.

| Situation | Binding behavior with WS support enabled |
| --- | --- |
| Domain error before `accept()` | HTTP denial as the existing problem JSON with `Fault.status` and HTTP headers if ASGI denial is supported; otherwise `websocket.close` before accept, which rejects the handshake with HTTP 403. Never accept automatically. |
| Domain error after `accept()`, outside `message()` | Event with `fatal=true`, followed by the policy close; this also applies with `recoverable=True`. |
| Recoverable domain error inside `message()` | One event with `fatal=false`; the connection remains open. |
| Terminal error inside `message()` | Propagates to the outer WS handler, which sends the event plus close; do not continue. |
| `WebSocketRequestValidationError` from FastAPI dependencies | With `include_validation_error=True`: `websocket_validation_error`, safely sanitized error list; denial 422/403 before accept, event and close 1008 after accept. |
| `WebSocketException` | Safe `websocket_error` built-in, no unvalidated `reason`; denial 400/403 before accept, then event and close. Preserve the exception close code if it is allowed by the v1 list above, otherwise use 1008. |
| `HTTPException` in WS scope | With `include_http_exceptions=True`: normalized HTTP denial before accept; after accept, built-in `websocket_error` and close 1008, not an HTTP response. |
| Unknown exception or registered fault without WS policy | With `include_unhandled_error=True`: safe `internal_server_error`, denial 500/403 or event plus close 1011; log the original server-side only. Otherwise propagate. |
| Error while rendering detail/extensions/headers | Regardless of the unhandled switch, return a safe internal error, log once, and do not recurse into broken callbacks. |
| Peer disconnect / already closed | No additional message and no second close. Treat `WebSocketDisconnect` as a normal termination. |
| Cancellation / `BaseException` | Propagate unchanged; do not normalize. |

Denial is best-effort error information: without the ASGI extension, a
structured handshake body cannot be guaranteed. Browser applications should
handle handshake errors generally; the API does not guarantee access to status
or body in that case. Structured application errors are available after a
successful accept. The denial mechanism is described in the
[Starlette WebSocket documentation](https://www.starlette.io/websockets/); frame
types, disconnects, and send errors follow the
[ASGI specification](https://asgi.readthedocs.io/en/latest/specs/www.html#websocket).

Send failures caused by a closed connection do not result in a second error
event. After a failed transport send, the connection is marked locally as ended;
arbitrary application `OSError` instances, however, are not swallowed as
disconnects. Emitting the event once is no delivery guarantee to the peer.

### 1.5 Registry, Transport Selection, and Installation

The existing registry remains the sole source of fault identities. `resolve()`
continues to use Python MRO; collisions for exception class, code, type URI,
and schema name remain globally forbidden. Transport-dependent duplicates of
the same domain definition are not introduced. `merge()` retains existing rules
for object identity and type-URI resolution.

`responses(...)` declares HTTP operations; `websocket_errors(...)` declares WS
operations. At decoration time, faults must be members of the feature registry
and have a WS policy; resolved URIs are required only at installation.
The installed aggregate registry must contain exactly the same fault objects.
Declarations serve documentation and contract validation, not error resolution:
a registered fault that is not declared on the route is rendered safely and is
reported by the test monitor as a contract violation.

Decide solely from `scope["type"]`: `http` or `websocket`. HTML is an HTTP
output format, not a separate transport. This ticket adds neither an HTML
renderer nor content negotiation; existing HTTP errors remain JSON.

`include_websockets=True` enables WS handling for all WS routes of this app.
Undecorated routes have an empty domain contract but receive built-in handling.
Decorator metadata with WS support disabled is an installation error. Existing
domain handlers must recognize WS scopes even with WS support disabled and
propagate exceptions there instead of sending HTTP JSON. Independently mounted
ASGI apps install their own registries; their routes are neither collected nor
normalized here.

Installation takes place after `include_router()` and before the first
request/OpenAPI generation. Perform all checks before any mutation: URIs, route
membership, WS policies, operation IDs, schema collisions, custom handlers, and
middleware start. An identical registry plus identical options is idempotent;
a differing registry or options fail. Store installation options in app state
as well. When WS is enabled, `type_base` is required for built-ins. Codes
`websocket_error`, `websocket_validation_error`, `internal_server_error`, and
the built-in schema names in use must not collide with domain faults.

### 1.6 Runtime Architecture

A pure ASGI middleware keeps per-connection state in a private scope entry and
wraps the downstream app call. It processes only WS scopes of its own app,
observes `receive`/`send`, and tracks accept, a started denial response, close,
and peer disconnect. It stores no connection-specific data on the middleware
instance and does not use HTTP-only `BaseHTTPMiddleware`.

The already installed domain exception handlers become transport dispatchers:
HTTP delegates to the existing handler, while WS re-raises the exception for
the outer WS middleware. The same applies, when normalization is enabled, to
`HTTPException`, `WebSocketException`, and `WebSocketRequestValidationError`.
FastAPI standard handlers may be deliberately replaced; custom handlers are
reported as configuration conflicts as before. With switches disabled, the
framework/custom behavior for that error class remains intact.

This lets middleware also capture errors during dependency resolution, before
the endpoint runs, and unknown errors after `accept()`. The existing
`Exception` handler alone is insufficient: Starlette's `ServerErrorMiddleware`
handles only HTTP scopes; see the
[Starlette implementation](https://raw.githubusercontent.com/Kludex/starlette/main/starlette/middleware/errors.py).
Dispatchers must not return an HTTP response in WS scope; see
[exception dispatch](https://raw.githubusercontent.com/Kludex/starlette/main/starlette/_exception_handler.py).

Middleware and `message()` use the same internal WS emitter and the same
occurrence hook for contract tests. The emitter works with observed ASGI state
and the send channel, not a newly created `WebSocket` object that would not know
the actual accept state. It produces valid `websocket.send`, `websocket.close`,
or denial ASGI messages. A handled error is not raised again; unknown errors
are explicitly logged and terminated when normalization is enabled. Custom
middleware outside this boundary is not covered.

### 1.7 Schema Export and Generated Clients

HTTP remains in the existing OpenAPI contract. `websocket_schema(app)` also
produces a standalone JSON-serializable document with
`format="fastapi-faults-websocket"` and `version=1`. This is a documented
library format, not a simulated OpenAPI or AsyncAPI document.

```json
{
  "format": "fastapi-faults-websocket",
  "version": 1,
  "operations": {
    "session_commands": {
      "path": "/sessions/{session_id}/commands",
      "transport": "websocket",
      "direction": "server_to_client",
      "encoding": "json-text",
      "faults": [
        {"code": "session_conflict", "recoverable": true, "close_code": 1008}
      ],
      "builtins": ["websocket_validation_error", "websocket_error", "internal_server_error"],
      "message_schema": {"$ref": "#/$defs/SessionCommandsFaultEvent"}
    }
  },
  "schemas": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {}
  }
}
```

The example shows the envelope; `$defs` must be complete in the real export.
All schema refs resolve relative to the `schemas` resource. A generator extracts
this field as a JSON Schema document and uses operation refs as its entry
points. There are no references to a separate OpenAPI document.

Each fault produces `<effective_schema_name>WebSocket` without `status`, with
`const` for `code` and `type`, the same extension properties and required
fields, and correctly rewritten nested `$defs`. HTTP examples are projected for
WS by removing `status` and validating again. Envelope schemas reference these
problem schemas; operation schemas form their `oneOf`. The operation schema
name derives from the operation ID using the same PascalCase convention as
fault names, plus `FaultEvent`; IDs must therefore satisfy
`^[a-z][a-z0-9_]{2,}$`. Name collisions fail during installation.

The union is at `problem`, so `problem.code` determines the disjoint variant.
For every envelope branch, define allowed `fatal`/`close_code` combinations:
terminal is always possible, and recoverable is additionally possible with the
corresponding policy. Built-ins are terminal. `websocket_error` permits the
close-code set defined above; an internal error uses 1011 only. Handshake denials
are defined separately by lifecycle rules and are not events.

The export contains only route-declared domain faults plus the actually enabled
built-ins. `websocket_error` is always included when WS is enabled; validation
follows its switch; the internal error is always possible because of the
rendering fallback. Routes without a decorator are not exported for
documentation. Fault schemas are shared; IDs are never derived from `id()` or
memory addresses. Sort operations and `$defs` lexicographically; fault lists
retain declaration order. Repeated exports are identical and return independently
mutable results.

A generator could, for example, produce a TypeScript union from it:

```typescript
type SessionConflictProblem = {
  type: "https://api.example.com/problems/session_conflict";
  title: "Session conflict";
  code: "session_conflict";
  current_version: number;
  detail?: string;
  instance?: string;
};
// In the full union: switch (event.problem.code).
// event.fatal determines the expected connection closure.
```

A production client generator, AsyncAPI export, automatic reconnects, success
message schemas, and subprotocol negotiation are follow-up work. This ticket
provides the complete error-schema export as their foundation. Ordinary OpenAPI
generators do not thereby automatically gain WS support.

### 1.8 Concrete File Placement

| Path | Responsibility / change |
| --- | --- |
| `fastapi_faults/fault.py` | Optional `websocket` field; validate policy type and retain existing validation. |
| `fastapi_faults/websocket_policy.py` (new) | Immutable `WebSocketPolicy`; validate close codes without importing the registry. |
| `fastapi_faults/problem.py` | `WebSocketProblem`, shared field/extension validation; retain the existing `Problem` contract. |
| `fastapi_faults/rendering.py` | Extract shared detail/extension resolution; HTTP and WS renderers, headers only in the HTTP branch. Retain `render_problem()`. |
| `fastapi_faults/websocket.py` (new) | Public event/session types, `message()`, and internal emitter. |
| `fastapi_faults/websocket_middleware.py` (new) | Pure ASGI boundary, connection state, terminal exception handling, and denial fallback. |
| `fastapi_faults/registry.py` | New public methods; use existing resolution and merge rules. |
| `fastapi_faults/contracts.py` | WS metadata, `iter_websocket_contracts()`, prefix/router resolution, occurrence hook; retain HTTP detection. |
| `fastapi_faults/handlers.py` | Installation preflight and transport dispatchers; keep existing HTTP handlers. |
| `fastapi_faults/schemas.py` (new) | Extract shared problem/extension schema functions from `openapi.py`. |
| `fastapi_faults/openapi.py` | HTTP document compilation; retain `compile_fault_schema()` as a delegating entry point. |
| `fastapi_faults/websocket_schema.py` (new) | WS export v1, complete `$defs`, built-ins, operation unions, and ref validation. |
| `fastapi_faults/__init__.py` | Export new public types. |
| `examples/websocket/{__init__,domain,app}.py` (new) | Runnable example with HTTP, terminal WS error, recoverable loop, and validated commands. |
| `specs/websocket.json` (new) | Deterministic export generated from the example, alongside `specs/openapi.json`. |
| `tests/helpers.py` | WS event/schema assertions and transport-aware undeclared-fault monitor; still not part of the shipped package. |
| `README.md` | Short WS example, opt-in, handshake boundaries, and links to export/example. |

`contracts.py` must continue the existing treatment of included routers and
correctly determine effective paths including multiple prefixes. Do not depend
on private FastAPI attributes without a compatibility test. Endpoint metadata
must not be lost during `include_router()`.

Implementation order: policy/models → shared rendering/schema →
registry/metadata → WS emitter/session/middleware → installation/dispatchers →
export → example/contract tests. Shared modules do not import transport
adapters; refer to registry types in adapters using `TYPE_CHECKING`, and perform
runtime facade imports locally when needed to avoid cycles.

## 2. Test Strategy and Definition of Done

Tests must secure observable behavior. Existing HTTP tests remain the
authoritative regression tests; do not change their expected wire formats.

| Test file | Core cases / acceptance |
| --- | --- |
| `tests/unit/test_websocket_policy.py` (new) | Defaults, immutability, valid codes and boundaries, forbidden codes/bool, invalid policy on a fault. |
| `tests/unit/test_rendering.py` | HTTP and WS share code/URI/extensions; WS has no status and does not run header callbacks; extension aliases, reserved fields, broken callbacks. |
| `tests/unit/test_registry.py` | Merge/identity/MRO remain intact; declaring foreign faults or faults without policy fails; duplicate operation IDs and built-in/schema collisions. |
| `tests/unit/test_websocket.py` (new) | Envelope invariants, omitting request_id, context-manager suppression only for recoverable faults; terminal propagation with correlation; reject block nesting/concurrency. |
| `tests/integration/test_websocket_handlers.py` (new) | Same exception yields HTTP problem or WS event; terminal event before close; a recoverable error outside the guard is still terminal; errors in endpoint and dependency before/after accept. |
| `tests/integration/test_websocket_messages.py` (new) | On one connection: erroneous command → exactly one correlated event → next successful command works; no success frame after a failed block; unknown error ends with 1011. |
| `tests/integration/test_websocket_lifecycle.py` (new) | Denial with/without ASGI extension; never accept on handshake error; disconnect, send-disconnect race, already closed connection, started denial response, cancellation, and no duplicate output. |
| `tests/integration/test_handlers.py` | Opt-in/opt-out, switch combinations, default/custom-handler conflicts, identical/differing installation; preflight errors cause no partial installation. |
| `tests/contract/test_websocket_schema.py` (new) | JSON Schema 2020-12 validation of real events including built-ins and nested extensions; all refs resolve; reject unknown code, HTTP status, and invalid fatal/close combinations. |
| `tests/contract/test_websocket_schema.py` (new) | Feature-registry merge, included routers/prefixes, app isolation, deterministic export without object IDs, agreement with `specs/websocket.json`. |
| `tests/integration/test_testing.py` | The undeclared-fault monitor detects globally rendered and locally recoverable WS faults and names transport, effective path, operation ID, and code; the HTTP monitor continues to work. |
| `tests/integration/test_websocket_example.py` (new) | Example starts; HTTP and both WS flows work without external services. |

For browser-compatible message flows, use `TestClient.websocket_connect()` as a
context manager; timeouts prevent hanging tests. Test denial fallback, ASGI
message ordering, and transport races additionally through a small ASGI harness
with explicit `scope["extensions"]`: TestClient alone does not prove server
support for the denial extension. Test cancellation separately for the absence
of error frames.

Built-in validation tests contain secret input values and exception text: these
must not appear in the payload. Validation details may use only sanitized,
allowed fields, never `input`, `ctx`, query strings, or arbitrary exception
representations. WS schemas explicitly forbid `status`, even among otherwise
allowed problem extensions.

Before completion, run the existing project gates:

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

Also check the existing Python 3.12/3.13/3.14 matrix, as well as one resolvable
combination at the declared FastAPI minimum and one current compatible
combination. Do not raise the minimum version wholesale just because of
`send_denial_response`: the emitter can use the standardized ASGI denial
extension directly. Demonstrate actual incompatibilities with a reproducible
test.

The implementation is complete when the examples run, all lifecycle cases are
covered, every generated error event validates against its matching exported
schema, HTTP remains unchanged, and the export is complete for a generator
without Python object identities or further manual error lists.
