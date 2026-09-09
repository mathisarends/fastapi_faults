# Namespaced showcase report

This sample turns a small FastAPI server into an `/api/v1` namespace containing
independent `users` and `sessions` modules. It is deliberately small, but uses
the same composition boundaries as a larger backend.

## Result

```text
app.py (composition root)
  `-- api.py             /api/v1 + merged API registry
        |-- users.py     /users + users registry
        `-- sessions.py  /sessions + sessions registry
```

Each feature is kept in its own module and exports the same small interface:

- `router`: a stock FastAPI `APIRouter`, with a feature-local prefix;
- `faults`: the feature's immutable `FaultRegistry`.

`api.py` includes both routers and merges both registries. `app.py` includes the
whole API namespace and performs a final merge that adds the deployment-wide
problem type base. Only this final registry is installed on the server.

```python
# api.py
router = APIRouter(prefix="/api/v1")
router.include_router(users.router)
router.include_router(sessions.router)
faults = FaultRegistry.merge(users.faults, sessions.faults, name="api-v1")

# app.py
app.include_router(api.router)
faults = FaultRegistry.merge(
    api.faults,
    name="server",
    type_base="https://api.example.com/problems",
)
faults.install(app)
```

Feature registries may remain unresolved while routes are declared. The final
merge resolves every fault to, for example,
`https://api.example.com/problems/user_not_found`. OpenAPI still connects each
operation to the right schema because route metadata keeps the `Fault` identity.

## Try it

Run the server from the repository root:

```console
uv run fastapi dev examples/namespaced/app.py
```

Then open `http://127.0.0.1:8000/docs` or call:

```console
curl http://127.0.0.1:8000/api/v1/users/00000000-0000-0000-0000-000000000000
curl http://127.0.0.1:8000/api/v1/sessions/0198e2ef-799c-765d-9ab4-2130d11d1e72
```

The responses are RFC 9457 `application/problem+json` documents. The first is
`user_not_found` (`404`), the second `session_expired` (`410`).

The generated document is checked in at [`specs/openapi.json`](../../specs/openapi.json)
for manual inspection. Regenerate it from the application with:

```console
uv run python -c "import json; from examples.namespaced.app import app; print(json.dumps(app.openapi(), indent=2))"
```

## Developer-experience findings

What already feels good:

- feature ownership is explicit and ordinary FastAPI routing keeps working;
- merge order is deterministic and collisions fail during startup/configuration;
- the type URI policy lives at the server boundary instead of in every module;
- one `Fault` drives runtime output and the corresponding OpenAPI component.

Where the API still creates friction:

1. `responses=faults.responses(...)` is long and repeats the local registry on
   every operation. A small supported route helper could make the happy path
   clearer without replacing `APIRouter`.
2. The `router` + `faults` export convention is only a convention. A typed
   `Feature`/`Namespace` value could make composition discoverable and prevent a
   router from being included while its registry is forgotten.
3. `FaultRegistry.merge(..., name=...)` does not visually communicate that the
   original feature ownership is retained for collision messages. Documentation
   or an inspection API would make the resulting registry easier to understand.
4. Missing type URIs are intentionally resolved late, but the failure also
   happens late (on installation). An explicit `resolve(type_base=...)` name may
   communicate that transition better than a second one-item merge.
5. Contract errors that depend on generated OpenAPI may not surface until
   `app.openapi()` is first called. A startup validation/compile option would
   shorten the feedback loop.
The accompanying integration test locks down routing, runtime Problem Details,
the merged registry, and generated OpenAPI.
