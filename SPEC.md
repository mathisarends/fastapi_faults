# fastapi-faults: API and behavior specification

Status: **Draft for v1**

Distribution name: `fastapi-faults`

Import name: `fastapi_faults`

## 1. Intent

`fastapi-faults` is a small integration layer between application exceptions,
HTTP Problem Details, and FastAPI's OpenAPI document.

An error is defined once. The same definition MUST drive both:

1. the response returned by the exception handler at runtime; and
2. the error responses documented for each path operation.

The wire format is JSON Problem Details as defined by RFC 9457. The media type
is `application/problem+json`.

The primary design target is excellent application-developer and API-consumer
ergonomics, not a general-purpose exception framework.

The key flow is:

```text
application exception
        |
        v
   FaultRegistry
     /       \
    v         v
runtime       OpenAPI 3.1
handler       response schemas
    |         |
    v         v
RFC 9457      typed client errors
```

The words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are normative in this
document.

## 2. Design principles

### 2.1 One source of truth

HTTP status, stable error code, problem type URI, title, detail rendering,
extension schema, and response headers belong to one `Fault` definition.

Route declarations refer to named `Fault` definitions. They MUST NOT repeat
response schemas, status codes, or descriptions. This makes a declaration such
as `raises=[SESSION_NOT_FOUND]` read as part of the operation's contract.

### 2.2 Domain isolation

Domain and application exceptions MUST NOT inherit from `HTTPException` and do
not need to import FastAPI, Starlette, Pydantic, or `fastapi_faults`.

### 2.3 Explicit operation contracts

Python does not have checked exceptions and FastAPI cannot reliably infer all
exceptions raised by a handler or its dependency graph. Known domain faults
therefore MUST be declared at the operation or router level.

No stack inspection, bytecode inspection, or automatic call-graph analysis is
part of the design.

### 2.4 Native FastAPI behavior

The integration MUST use a normal `FastAPI` application and a subclass of
`APIRouter`. It MUST NOT require a custom `FastAPI` subclass, middleware, a base
class for application exceptions, or a service-locator global.

Normal FastAPI arguments such as `responses`, `response_model`, dependencies,
callbacks, and custom `APIRoute` classes MUST continue to work.

### 2.5 Safe by default

`str(exception)`, exception representations, tracebacks, request bodies, and
Pydantic's rejected input MUST NOT be exposed unless the application explicitly
maps a value into the public problem payload.

## 3. Target user experience

### 3.1 Minimal complete example

```python
# domain.py -- no HTTP or framework imports
from uuid import UUID


class SessionNotFound(Exception):
    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id


class SessionConflict(Exception):
    pass
```

```python
# api/errors.py -- the HTTP boundary
from fastapi_faults import Fault, FaultRegistry

from app.domain import SessionConflict, SessionNotFound


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
    detail=lambda exc: f"Session {exc.session_id} does not exist.",
)

SESSION_CONFLICT = Fault(
    SessionConflict,
    status=409,
    code="session_conflict",
    title="Session conflict",
)

faults = FaultRegistry(
    faults=[
        SESSION_NOT_FOUND,
        SESSION_CONFLICT,
    ],
    type_base="https://api.example.com/problems",
)
```

```python
# api/routes.py
from uuid import UUID

from app.api.errors import SESSION_CONFLICT, SESSION_NOT_FOUND, faults


router = faults.router(prefix="/sessions", tags=["sessions"])


@router.get(
    "/{session_id}",
    raises=[SESSION_NOT_FOUND, SESSION_CONFLICT],
)
async def get_session(session_id: UUID) -> SessionView:
    return await sessions.get(session_id)
```

```python
# api/app.py
from fastapi import FastAPI

from app.api.errors import faults
from app.api.routes import router


app = FastAPI()
app.include_router(router)
faults.install(app)
```

The application writes no `responses={...}`, no exception handler, and no
Problem Details model for this common case.

### 3.2 Runtime result

```http
HTTP/1.1 404 Not Found
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/session_not_found",
  "title": "Session not found",
  "status": 404,
  "detail": "Session 0198e2ef-799c-765d-9ab4-2130d11d1e70 does not exist.",
  "code": "session_not_found"
}
```

`type` is the RFC 9457 primary identifier. `code` is a required extension in
the `fastapi-faults` profile because it is convenient and stable for generated
clients. Clients MUST NOT parse `title` or `detail` to branch on error type.

## 4. Public API

The root package SHOULD export only the commonly used API:

```python
from fastapi_faults import (
    Fault,
    FaultConfigurationError,
    FaultRegistry,
    FaultRouter,
    Problem,
    Router,
)
```

Framework-specific implementation details MAY live under
`fastapi_faults.fastapi`, but common usage MUST NOT require deep imports.

### 4.1 `Fault`

The conceptual constructor is:

```python
Fault(
    exception: type[Exception],
    *,
    status: int,
    code: str,
    title: str,
    type: str | None = None,
    detail: str | Callable[[Exception], str | None] | None = None,
    extensions_model: type[pydantic.BaseModel] | None = None,
    extensions: (
        Mapping[str, JsonValue]
        | Callable[[Exception], Mapping[str, JsonValue] | BaseModel]
        | None
    ) = None,
    headers: (
        Mapping[str, str]
        | Callable[[Exception], Mapping[str, str]]
        | None
    ) = None,
    openapi_headers: Mapping[str, OpenAPIHeader] | None = None,
    description: str | None = None,
    example: Mapping[str, JsonValue] | None = None,
    schema_name: str | None = None,
)
```

The final typing MAY use protocols and generics to make callbacks see the
concrete exception type. The runtime behavior MUST match this contract.

Validation rules:

- `exception` MUST be an `Exception` subclass.
- `status` MUST be between 400 and 599 for v1.
- `code` MUST match `^[a-z][a-z0-9_]{2,}$`.
- `title` MUST be a non-empty, single-line string.
- `type`, when provided, MUST be a valid absolute URI.
- `detail` MUST NOT default to `str(exception)`.
- `extensions` MUST NOT contain `type`, `title`, `status`, `detail`,
  `instance`, or `code`.
- `extensions_model` and `extensions` MUST either both be present or both be
  absent. A static mapping is exempt from the model requirement only when all
  values have an unambiguous JSON Schema type.
- `headers` containing dynamic values SHOULD have matching `openapi_headers`.
- A static `example` MUST validate against the compiled problem schema.

`Fault` instances MUST be immutable and safe to reuse across registries.

### 4.2 Typed RFC 9457 extension members

Problem-specific data is flattened into the problem object, as required by the
RFC extension-member model; it is not nested below an `extensions` or `data`
property.

```python
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SessionNotFoundFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID


SESSION_NOT_FOUND = Fault(
    SessionNotFound,
    status=404,
    code="session_not_found",
    title="Session not found",
    detail=lambda exc: f"Session {exc.session_id} does not exist.",
    extensions_model=SessionNotFoundFields,
    extensions=lambda exc: SessionNotFoundFields(session_id=exc.session_id),
)
```

The resulting payload has `session_id` at its top level. The model is used to
validate serialization and to generate the corresponding OpenAPI properties.

Extension serialization MUST use Pydantic's JSON-mode serialization so types
such as UUID, date, and Enum produce the same representation at runtime and in
the schema.

### 4.3 `FaultRegistry`

```python
FaultRegistry(
    *,
    faults: Sequence[Fault],
    name: str | None = None,
    type_base: str | None = None,
)
```

Responsibilities:

- resolve exception classes to exactly one `Fault`;
- validate the complete configuration when it is merged or installed;
- install runtime exception handlers;
- augment the existing FastAPI OpenAPI generator;
- create bound `FaultRouter` instances through `registry.router(...)`.

`name` is optional diagnostic metadata. Feature registries SHOULD use a short,
stable name such as `"browser"` or `"sessions"` so merge errors can identify
the owners of conflicting definitions.

For faults without an explicit `type`, the URI is
`{type_base.rstrip('/')}/{code}`. A feature registry MAY leave `type_base`
unset so the application composition root can supply it later. Such a registry
can own routers and be tested in isolation, but cannot be installed until every
fault has a resolved type URI. A domain-specific fault MUST NOT silently fall
back to `about:blank`.

One exception class maps to one fault within a fully composed registry.
Duplicate exception classes, duplicate codes, duplicate type URIs, and
duplicate schema names MUST fail eagerly with a `FaultConfigurationError` that
names both definitions and their source registries.

Mappings for an exception base class and its subclasses are allowed. Runtime
resolution follows normal Python MRO and uses the most specific registered
class.

Registries are immutable values. There is no `register()` or `add()` mutation
API in v1. Composition creates a new registry, preventing import order and
application startup timing from changing the error contract.

#### 4.3.1 Feature registry composition

Larger applications SHOULD define one registry per feature or application
stage, next to that feature's fault definitions and router. Registries are
merged explicitly at the application composition root, similar to composing
multiple dependency providers into one container:

```python
from fastapi_faults import FaultRegistry

from app.accounts.errors import account_faults
from app.browser.errors import browser_faults
from app.sessions.errors import session_faults


api_faults = FaultRegistry.merge(
    browser_faults,
    session_faults,
    account_faults,
    name="api",
    type_base="https://api.example.com/problems",
)
```

Each feature remains self-contained:

```python
# app/browser/errors.py
BROWSER_NOT_FOUND = Fault(
    BrowserNotFound,
    status=404,
    code="browser_not_found",
    title="Browser not found",
)

browser_faults = FaultRegistry(
    name="browser",
    faults=[
        BROWSER_NOT_FOUND,
        BROWSER_STATE_UNAVAILABLE,
    ],
)

browser_router = browser_faults.router(
    prefix="/browser/{browser_id}",
    raises=[BROWSER_NOT_FOUND],
)
```

The application installs only the composed registry:

```python
app.include_router(browser_router)
app.include_router(session_router)
app.include_router(account_router)
api_faults.install(app)
```

`FaultRegistry.merge(...)` accepts registries as ordinary positional arguments
plus the keyword-only options `name` and `type_base`. Callers never need to
unpack fault tuples or lists. It MUST:

- return a new registry and never mutate its inputs;
- preserve registry argument order and definition order;
- flatten already-composed registries;
- deduplicate the same `Fault` object when it arrives through multiple shared
  registries (diamond composition);
- reject distinct definitions that collide by exception class, code, final
  type URI, or schema name;
- retain an explicit `Fault.type` unchanged;
- retain a type URI already resolved by a feature registry's `type_base`; and
- use the merged registry's `type_base` only as the fallback for unresolved
  feature faults.

The `faults=[...]` constructor parameter is intentional. A named feature
collection is always a `FaultRegistry`, never an informal tuple that has to be
expanded with `*` at the composition root.

Before OpenAPI generation, `install()` MUST verify that every fault referenced
by every included feature router is present in the installed application
registry. This catches an accidentally omitted feature registry at startup.

A feature registry with its own `type_base` MAY be installed directly in a
feature test application. The same registry MUST resolve to the same type URIs
when later merged into the production application registry.

Decorator-based global registration and process-global registry state are
deliberately not part of v1.

### 4.4 `Router` / `FaultRouter`

`Router` is the concise public name for `FaultRouter`. It is API-compatible
with FastAPI's `APIRouter` and adds the same keyword both to the router itself
and to HTTP path-operation decorators:

```python
raises: Sequence[Fault] = ()
```

The preferred constructor is registry-bound:

```python
router = faults.router(
    prefix="/sessions",
    tags=["sessions"],
    raises=[AUTHENTICATION_FAILED],
)
```

The explicit form is also supported:

```python
router = Router(
    registry=faults,
    prefix="/sessions",
    raises=[AUTHENTICATION_FAILED],
)
```

Router-level `raises` is unioned with the operation's `raises`. It is intended
for feature-wide resource lookup, authentication, authorization, or tenant
errors:

```python
browser_router = faults.router(
    prefix="/browser/{browser_id}",
    raises=[BROWSER_NOT_FOUND],
)


@browser_router.get("/state", raises=[BROWSER_STATE_UNAVAILABLE])
async def get_browser_state(browser_id: UUID) -> BrowserState:
    ...
```

Duplicate fault definitions are removed while preserving declaration order.
Application-/feature-level defaults are a first-class use case, not an
afterthought.

Every `Fault` in `raises` MUST exist in the router's feature registry. An
unknown definition is a configuration error at route registration, not a
missing schema discovered in production. The exact registered object is used;
the router MUST NOT construct another definition from the exception class.

Accepting exception classes as a registry-resolved shorthand MAY be considered
later, but is not part of the v1 contract. Named fault constants are more
explicit in reviews and make status-specific variants impossible to confuse.

`include_router()` MUST preserve fault metadata through FastAPI's route-copying
behavior, including nested routers and mounting the same router more than once.
Outer router-level faults are applied before inner router-level faults, followed
by operation-level faults. A fault is emitted only once in the effective list.

For unresolved feature registries, the router records the fault contract and
defers final response-schema compilation until the composed application
registry is installed. It MUST NOT emit an incomplete placeholder schema.

WebSocket routes do not accept `raises` in v1.

### 4.5 Stock `APIRouter` escape hatch

Codebases unable to adopt `FaultRouter` MAY use:

```python
@router.get(
    "/{session_id}",
    responses=faults.responses(SESSION_NOT_FOUND, SESSION_CONFLICT),
)
async def get_session(session_id: UUID) -> SessionView:
    ...
```

`FaultRegistry.responses()` accepts one or more fault definitions as ordinary
positional arguments. It MUST return a fresh FastAPI-compatible responses
mapping and MUST use the same OpenAPI compiler as `FaultRouter`. This is an
interoperability escape hatch, not the recommended API.

### 4.6 `install()`

```python
faults.install(
    app,
    include_validation_error=True,
    include_http_exceptions=True,
    include_unhandled_error=True,
)
```

All three normalization options default to the values shown, so normal usage
remains the single line `faults.install(app)`.

Installation MUST:

1. validate the final composed registry and every included route reference;
2. register exception handlers;
3. wrap the application's existing `openapi()` method;
4. preserve OpenAPI caching through `app.openapi_schema`; and
5. be idempotent for the same application and registry.

Installing a different registry on the same application MUST fail. Installing
after an OpenAPI schema has already been cached MUST fail with an actionable
message; it MUST NOT silently serve a stale contract.

The OpenAPI wrapper MUST call the previously installed OpenAPI function and
transform its result. It MUST NOT replace unrelated OpenAPI customizations.

If an application already has a custom handler for a class the registry intends
to handle, installation MUST fail by default and identify the class. Known
unmodified FastAPI default handlers are replaced when their normalization is
enabled. A future explicit conflict policy MAY permit replacement; silent
replacement is forbidden.

## 5. Problem Details profile

### 5.1 Base model

`Problem` represents this library's RFC 9457 profile:

```python
class Problem(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None
```

The JSON representation MUST omit optional values that are `None`.

Although RFC 9457 makes all standard members optional, every response generated
for a registered domain fault MUST contain `type`, `title`, `status`, and
`code`. `detail` and `instance` remain occurrence-specific and optional.

- `type` MUST equal the registered type URI.
- `title` MUST equal the registered short summary in v1.
- `status` MUST equal the actual HTTP response status.
- `code` MUST equal the registered code.
- `detail`, if present, SHOULD explain how the client can correct this specific
  occurrence and MUST NOT be the machine-readable identifier.
- `instance`, if present, MUST be a URI reference identifying this occurrence.

Localization of `title` is outside v1 because a localized value would weaken
the generated constant schema. Applications MAY localize `detail` in a custom
renderer, provided the contract remains stable.

### 5.2 Media type and encoding

Every generated problem response MUST use:

```http
Content-Type: application/problem+json
```

The body MUST be valid UTF-8 JSON. The implementation SHOULD use Starlette's
normal JSON response machinery and FastAPI/Pydantic JSON encoders rather than a
private JSON implementation.

### 5.3 Headers

Dynamic headers from the fault renderer are merged with framework-required
headers. Header names are case-insensitive. Attempting to set `Content-Type` or
a header managed by the ASGI server MUST fail configuration validation.

`Retry-After`, `WWW-Authenticate`, and `Allow` MUST be preserved when converting
framework HTTP exceptions.

### 5.4 Unexpected failures

When `include_unhandled_error=True`, unhandled exceptions produce a generic 500
problem. Its `detail` is absent and its payload contains no exception-derived
data. The exception is re-used for normal server-side logging with its original
traceback.

The fallback fault has a stable code such as `internal_server_error` and a type
URI derived from the registry. It is documented as a `default` response only
when the application opts into documenting unexpected failures; it MUST NOT be
listed as a known 500 outcome on every operation by default.

FastAPI/Starlette debug exception pages MAY take precedence when `app.debug` is
true. This difference MUST be documented and tested.

## 6. Built-in FastAPI normalization

### 6.1 Request validation

With `include_validation_error=True`, `RequestValidationError` is rendered as a
422 problem and FastAPI's default `HTTPValidationError` response schema is
replaced on affected operations.

The problem code is `request_validation_error`. It contains an `errors` array.
Each item has stable machine-readable fields:

```json
{
  "code": "int_parsing",
  "detail": "Input should be a valid integer",
  "pointer": "#/age"
}
```

For JSON request bodies, `pointer` is an RFC 6901 JSON Pointer. For path, query,
header, and cookie input, an item instead uses `parameter` and `in`:

```json
{
  "code": "missing",
  "detail": "Field required",
  "parameter": "x-request-id",
  "in": "header"
}
```

The rejected `input` and Pydantic context values are omitted by default because
they may contain credentials or personal data. The mapping from Pydantic error
locations to pointers MUST be deterministic, correctly escape `~` and `/` per
RFC 6901, and have dedicated tests.

### 6.2 HTTP exceptions

With `include_http_exceptions=True`, FastAPI and Starlette `HTTPException`
instances are normalized to Problem Details while preserving their status and
headers.

An HTTP exception with no registered application-specific mapping uses
`about:blank`; its title is the standard reason phrase for the status, in line
with RFC 9457. A string `detail` MAY be used as occurrence detail. Structured
FastAPI `detail` values MUST NOT be copied into the RFC `detail` string; they
require an explicit custom fault.

Framework-generated 404 and 405 responses are normalized at runtime, but are
not added to every operation's OpenAPI contract.

### 6.3 Response validation

Response-model validation failures are server defects. They MUST be mapped to
the generic 500 problem and logged. Their validation details MUST NOT be exposed
to the client.

## 7. OpenAPI contract

### 7.1 Version

V1 targets the OpenAPI 3.1 document generated by supported FastAPI versions.
OpenAPI 3.0 compatibility is not required unless it is added and tested as a
separate compatibility mode.

### 7.2 Components

The compiler MUST add a reusable base `Problem` schema and one deterministic
component per fault, for example `SessionNotFoundProblem`.

Each fault component MUST describe:

- required `type`, `title`, `status`, and `code` properties;
- constant values for `type`, `title`, `status`, and `code`;
- optional `detail` and `instance` properties;
- all typed extension members;
- a validated example when configured; and
- `instance` and `type` as URI references.

Generated names use PascalCase `code` plus `Problem`; `schema_name` overrides
the generated name. All collisions are configuration errors.

The schema MUST allow unknown future extension members so clients follow RFC
9457's forward-compatibility rule. The producer still validates the extensions
it emits.

### 7.3 Operation responses

For one fault at a status, the operation response directly references its
component. For multiple faults sharing a status, it uses `oneOf` and a
`discriminator` on `code` with an explicit mapping.

Example:

```yaml
responses:
  "409":
    description: The request conflicts with current session state.
    content:
      application/problem+json:
        schema:
          oneOf:
            - $ref: "#/components/schemas/SessionConflictProblem"
            - $ref: "#/components/schemas/SessionAlreadyClosedProblem"
          discriminator:
            propertyName: code
            mapping:
              session_conflict: "#/components/schemas/SessionConflictProblem"
              session_already_closed: "#/components/schemas/SessionAlreadyClosedProblem"
```

The response description is `Fault.description` when exactly one fault is
present. Otherwise the compiler generates a concise description that names all
possible problem titles.

The generated media type MUST be `application/problem+json`, never the default
`application/json` FastAPI assigns to ordinary JSON response models.

### 7.4 Composition with manual `responses`

Manual responses and generated fault responses are merged at status and media
type level:

- unrelated status codes are preserved;
- non-problem media types at the same status are preserved;
- user-defined headers and links are preserved unless they conflict with a
  fault-owned definition; and
- a manual `application/problem+json` schema at a generated fault status is a
  configuration error.

The library MUST NOT silently overwrite a manual contract.

### 7.5 Runtime/OpenAPI parity

For every registered and declared fault, tests MUST demonstrate that:

- the runtime HTTP status equals the documented response key;
- the runtime media type is documented;
- the runtime payload validates against the exact documented schema branch;
- the `type`, `status`, and `code` constants agree; and
- runtime response headers are documented when `openapi_headers` is supplied.

## 8. Error handling semantics

### 8.1 Handler resolution

Runtime handling is application-wide. `raises` controls documentation, not
whether a registered exception can be rendered. If a registered exception
escapes an operation that did not declare it, it is still returned safely as a
problem, but the operation contract is incomplete.

The package SHOULD provide an opt-in test helper that records such undeclared
faults and fails the test with the operation ID and exception class. It MUST NOT
turn this into a production-only runtime failure.

### 8.2 Callback failures

If a `detail`, `extensions`, or `headers` callback fails, the library MUST NOT
return a partial problem. It logs the mapping failure with exception chaining
and returns the generic 500 problem.

### 8.3 Cancellation and control-flow exceptions

Only `Exception` subclasses are accepted. `BaseException`, cancellation,
keyboard interruption, and system-exit signals MUST NOT be captured as domain
faults.

Python `ExceptionGroup` mapping is outside v1.

## 9. Observability and privacy

The library MUST use standard Python logging and MUST NOT configure application
logging.

Every handled fault SHOULD expose structured log context through a documented
hook, including `code`, `type`, `status`, exception class, and route operation
ID. The default logging policy SHOULD be:

- expected 4xx domain faults: no log or debug;
- expected 5xx domain faults: error;
- unexpected 500 faults: exception with traceback.

Secrets and the rendered `detail` or extensions MUST NOT be logged
automatically. Applications own correlation IDs. An optional `instance`
provider MAY turn an existing correlation ID into a URI reference, but the
library MUST NOT invent globally meaningful identifiers.

## 10. Compatibility and lifecycle

V1 supports CPython 3.12, 3.13, and 3.14. Package metadata MUST declare
`requires-python = ">=3.12,<3.15"` until support for a later Python release is
explicitly tested and added. V1 requires Pydantic v2 and a FastAPI version that
emits OpenAPI 3.1.

### 10.1 CI matrix

Every pull request and protected-branch push MUST run the complete test suite
on Linux for Python 3.12, 3.13, and 3.14. CI MUST additionally exercise:

- the lowest supported FastAPI, Starlette, and Pydantic versions on Python
  3.12;
- the newest permitted dependency versions on Python 3.14; and
- the normal dependency set on Windows and macOS with Python 3.14.

No supported Python version may be an allowed failure. Dependency prereleases
MAY run as a scheduled, non-blocking early-warning job, but do not count as
support.

The required CI gates are formatting, linting, strict type checking, unit and
integration tests, OpenAPI contract validation, package build, and installation
of the built wheel into a clean environment. Tests MUST run against the
installed package or an editable installation configured by the project; they
MUST NOT succeed by accidentally importing from the repository root.

### 10.2 Test architecture

Test code follows the production structure so ownership stays obvious:

```text
src/fastapi_faults/fault.py       -> tests/unit/test_fault.py
src/fastapi_faults/registry.py    -> tests/unit/test_registry.py
src/fastapi_faults/rendering.py   -> tests/unit/test_rendering.py
src/fastapi_faults/handlers.py    -> tests/integration/test_handlers.py
src/fastapi_faults/router.py      -> tests/integration/test_router.py
src/fastapi_faults/openapi.py     -> tests/contract/test_openapi.py
```

When a production module is split, its corresponding test module SHOULD be
split in the same change. Shared fixtures live at the narrowest useful scope.
Tests MUST assert observable behavior and invariants rather than copy the
implementation algorithm into test helpers.

Unit tests cover immutable definitions, validation, merge behavior, resolution,
and rendering without constructing a full application. Integration tests use a
real `FastAPI` application and its test client. Contract tests generate the
actual OpenAPI document, validate it independently, and validate real runtime
responses against the documented schema branch.

Bug fixes MUST add a failing regression test in the layer where the defect was
observable. Coverage is a guardrail rather than the target; CI SHOULD enforce
line and branch coverage while reviews prioritize meaningful boundary and
failure-path assertions.

### 10.3 Framework compatibility

Public behavior MUST be tested against the lowest and newest supported FastAPI
versions. In particular, tests MUST cover:

- nested and repeatedly included routers;
- isolated feature registries, nested merges, diamond merges, and merge
  conflicts;
- detection of a feature router omitted from the installed application
  registry;
- FastAPI OpenAPI caching;
- a pre-existing custom OpenAPI wrapper;
- sync and async path operations;
- exception inheritance;
- custom response classes;
- manual response merging;
- request validation for body, path, query, header, and cookie input;
- framework 404/405 and HTTP exception headers;
- debug and production exception behavior; and
- schema validation with an independent OpenAPI 3.1 validator.

The `FaultRouter` signatures necessarily track FastAPI's decorator signatures.
Signature parity MUST have an automated regression test so upgrades cannot
silently drop new FastAPI parameters.

## 11. Non-goals for v1

- changing domain exceptions to HTTP-aware classes;
- inferring exceptions from source code or type annotations;
- response success envelopes;
- GraphQL, WebSocket, XML Problem Details, or non-ASGI integrations;
- automatic localization;
- automatically hosting HTML pages for problem type URIs;
- retry orchestration or client SDK generation;
- serializing arbitrary exception attributes;
- replacing application logging, tracing, or error reporting.

## 12. Required repository structure

```text
.
├── .github/
│   └── workflows/
│       └── ci.yml
├── examples/
│   └── minimal/
│       ├── app.py
│       └── domain.py
├── src/
│   └── fastapi_faults/
│       ├── __init__.py          # small public surface
│       ├── _types.py            # protocols and JSON value types
│       ├── fault.py             # immutable Fault definition
│       ├── problem.py           # Problem and built-in problem models
│       ├── registry.py          # validation, resolution, composition
│       ├── rendering.py         # exception -> validated problem payload
│       ├── handlers.py          # FastAPI/Starlette handlers
│       ├── router.py            # FaultRouter and metadata preservation
│       ├── openapi.py           # schema and operation response compiler
│       └── testing.py           # parity and undeclared-fault helpers
├── tests/
│   ├── contract/
│   │   └── test_openapi.py
│   ├── integration/
│   │   ├── test_handlers.py
│   │   ├── test_http_exceptions.py
│   │   ├── test_request_validation.py
│   │   └── test_router.py
│   └── unit/
│       ├── test_fault.py
│       ├── test_problem.py
│       ├── test_registry.py
│       └── test_rendering.py
├── CHANGELOG.md
├── LICENSE
├── README.md
├── SPEC.md
├── pyproject.toml
└── uv.lock
```

The project MUST use a `src` layout and a PEP 517 build backend. Dependency and
environment management use `uv`; linting and formatting use Ruff; tests use
pytest; static typing uses mypy in strict mode; coverage includes branch
coverage. GitHub Actions is the reference CI implementation.

The runtime dependency set SHOULD remain limited to FastAPI and Pydantic plus
their transitive Starlette dependency. A new runtime dependency requires a
documented reason and must materially reduce correctness or maintenance risk.

The public package layout is:

```text
src/fastapi_faults/
├── __init__.py          # small public surface
├── _types.py            # protocols and JSON value types
├── fault.py             # immutable Fault definition
├── problem.py           # Problem and built-in problem models
├── registry.py          # validation, resolution, composition
├── rendering.py         # exception -> validated problem payload
├── handlers.py          # FastAPI/Starlette handlers
├── router.py            # FaultRouter and metadata preservation
├── openapi.py           # schema and operation response compiler
└── testing.py           # parity and undeclared-fault helpers
```

Modules prefixed with `_` are private. Public names and import paths require an
explicit compatibility decision before release.

## 13. Acceptance criteria for the first release

V1 is complete only when all of the following are true:

1. The minimal example in section 3 runs without omitted setup.
2. A domain exception is defined without framework imports.
3. One fault definition produces both its runtime response and OpenAPI schema.
4. A route declares named fault constants with only `raises=[...]`.
5. Multiple faults at one status generate a discriminated `oneOf`.
6. Runtime examples validate against generated OpenAPI schemas.
7. FastAPI request-validation and HTTP exceptions are normalized consistently.
8. Manual response and OpenAPI customizations are preserved or fail loudly on
   real conflicts.
9. No exception string, traceback, rejected input, or response-validation
   detail leaks by default.
10. The public API is fully typed, documented, and passes strict static type
    checking in the example project.
11. Independent feature registries merge without mutation and remain usable in
    isolated feature test applications.
12. Merge collisions and a feature router missing from the installed
    application registry fail before the first request is served.
13. Required CI passes on Python 3.12, 3.13, and 3.14.
14. The built wheel installs into a clean environment and runs the minimal
    example from section 3.

## 14. Deliberate decisions and rejected alternatives

### Throwing problem classes from the domain

Rejected because it couples business code to HTTP and makes the same use case
harder to expose through a worker, CLI, or another transport.

### Repeating `responses={...}` on every route

Supported as an escape hatch but rejected as the primary API because it is
verbose and permits runtime behavior and documentation to drift.

### Inferring route errors automatically

Rejected because dependencies and arbitrary Python control flow make the result
incomplete and surprising. Explicit `raises` is small, local, and reviewable.

### Using only an application-specific `code`

Rejected because RFC 9457 defines `type` as the primary identifier. The profile
keeps both: standards-compliant `type` and client-friendly `code`.

### Using `about:blank` for all domain errors

Rejected because `about:blank` asserts that the problem has no semantics beyond
the HTTP status. Domain error codes do have additional semantics.

### Catch-all middleware

Rejected because FastAPI/Starlette exception handlers compose more naturally
with framework behavior, preserve HTTP exception headers, and avoid disturbing
the ASGI middleware stack.

## 15. Normative references

- [RFC 9457: Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457.html)
- [RFC 6901: JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901.html)
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
- [OpenAPI Specification 3.1](https://spec.openapis.org/oas/v3.1.1.html)
- [FastAPI: Additional Responses in OpenAPI](https://fastapi.tiangolo.com/advanced/additional-responses/)
- [FastAPI: Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/)
- [FastAPI: Extending OpenAPI](https://fastapi.tiangolo.com/how-to/extending-openapi/)

## 16. Implementation plan for coding agents

This section is the execution order for an implementation produced from this
specification. A coding agent SHOULD complete each phase and its tests before
moving to the next phase.

### Phase 1: Project foundation

1. Create the repository structure from section 12.
2. Configure packaging, supported Python versions, Ruff, strict mypy, pytest,
   branch coverage, and the CI matrix from section 10.
3. Add the minimal public exports as importable placeholders only where needed
   by the next phase; do not design additional public API.
4. Verify source-layout imports from the built wheel.

### Phase 2: Core definitions and composition

1. Implement JSON value types and callback protocols.
2. Implement immutable `Fault` validation from section 4.1.
3. Implement `Problem` and typed extension serialization.
4. Implement immutable `FaultRegistry`, deterministic `merge()`, identity
   deduplication, collision diagnostics, type-URI resolution, and MRO lookup.
5. Complete the corresponding unit tests before FastAPI integration.

### Phase 3: Runtime integration

1. Implement problem rendering and callback-failure fallback behavior.
2. Install domain exception handlers without middleware.
3. Normalize FastAPI/Starlette HTTP exceptions, request validation errors, and
   response validation errors as specified in sections 5 and 6.
4. Preserve headers and traceback-aware server logging.
5. Verify all behavior through real FastAPI integration tests.

### Phase 4: Router ergonomics

1. Implement `Router` / `FaultRouter` with router- and operation-level
   `raises`.
2. Preserve all FastAPI decorator signatures and route metadata.
3. Implement deterministic inheritance through nested and repeated router
   inclusion.
4. Detect unknown fault definitions and application registries missing a
   feature registry.
5. Add the stock `APIRouter` escape hatch.

### Phase 5: OpenAPI compiler

1. Generate base and per-fault OpenAPI 3.1 schemas.
2. Generate status responses with `application/problem+json`.
3. Generate discriminated `oneOf` schemas for same-status faults.
4. Replace FastAPI's default request-validation schema where applicable.
5. Compose with existing OpenAPI wrappers and manual responses using the
   conflict rules in section 7.
6. Validate the document independently and validate runtime payloads against
   their documented schema branches.

### Phase 6: Hardening and release readiness

1. Implement `fastapi_faults.testing` helpers.
2. Complete security, inheritance, debug-mode, cache, header, and callback
   failure tests.
3. Add README examples by copying the verified examples from this spec; do not
   create a second, divergent API description.
4. Run every required CI job, build wheel and source distribution, install the
   wheel cleanly, and run the minimal example.
5. Record the initial public contract and compatibility range in the changelog.

### Agent constraints

- The specification is authoritative for public API and observable behavior.
- An implementation detail MAY change without editing the spec; a public API,
  default, wire-format, schema, or conflict-policy change MUST update the spec
  and its contract tests in the same change.
- The agent MUST not weaken or delete an acceptance criterion merely to make a
  test pass.
- The agent MUST prefer the smallest implementation satisfying the contract
  and MUST not add speculative abstractions or integrations from the v1
  non-goals.
- Every phase ends with formatting, linting, strict typing, and its relevant
  tests passing.
