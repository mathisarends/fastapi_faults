# Concept: Automatic Fault Discovery for Routes and Dependencies

Status: draft proposal. Not yet approved for implementation; public APIs and
names are suggestions only.

## Motivation

A route can raise registered domain exceptions not only directly but also
through services and arbitrarily deeply nested FastAPI dependencies. If such a
fault is not documented using `responses=registry.responses(...)`, runtime
behavior and the OpenAPI contract can diverge.

Existing runtime auditing can detect errors that a test actually triggers. It
cannot, however, find control paths that were not executed. Complementary
static analysis could make potential faults visible early and report missing
response declarations in CI.

## Target State

An audit examines a fully configured FastAPI application and produces a
traceable report for every HTTP operation covering:

- faults explicitly declared through `responses=registry.responses(...)`;
- registered exceptions raised directly in the handler;
- faults from recursively traversed FastAPI dependencies;
- faults from statically resolvable calls in application-owned code;
- missing, superfluous, or indeterminate declarations.

The existing route API remains unchanged. In particular, no new `raises=[...]`
option is introduced for FastAPI decorators.

## Fundamental Limitation

Complete exception analysis is not reliably possible for dynamic Python code.
Examples include dynamic imports, callbacks, monkeypatching, data-dependent
call targets, and exceptions from third-party libraries. Exceptions found
statically may also be caught within a function and thus never reach the route.

The feature must therefore not promise absolute completeness. Every finding
must disclose its origin and confidence level.

## Proposed Analysis

### 1. Routes and Dependency Trees

The starting point is the application's `APIRoute` instances. The analyzer uses
FastAPI's already constructed `dependant` tree and recursively visits:

- the path operation handler;
- direct and indirect `Depends(...)` dependencies;
- callable instances and bound methods;
- functions bound with `functools.partial`;
- decorator chains resolvable through `inspect.unwrap()`.

Cycles and reused dependencies must be detected by callable identity.

### 2. Direct Raises

When Python source code is available, the AST of a callable is examined.
Unambiguous expressions like the following can be detected with high
confidence:

```python
raise SessionNotFound(session_id)
```

The analysis must also account for:

- re-raising via `raise`;
- `raise SomeError from cause`;
- exception aliases from imports;
- conditional control paths;
- `try`/`except` blocks that fully catch an exception or replace it with a
  different exception.

The analysis may consider only exception classes resolvable through the
installed `FaultRegistry`. Arbitrary technical exceptions should be reported
separately, but must not automatically become part of the public
problem-details contract.

### 3. Static Call Graph

Optionally, the analysis can follow statically resolvable calls in configured
application packages:

```python
async def get_session(service: SessionService = Depends(...)) -> Session:
    return await service.load_session()
```

In this example, the analyzer would also inspect `SessionService.load_session()`.
By default, it should respect package boundaries and not analyze entire
third-party libraries without explicit configuration.

For protocols, abstract methods, or multiple possible implementations, the
finding must be considered uncertain. Recursion and cyclic call graphs require
a stable traversal strategy and a configurable depth limit.

### 4. Explicit Hints for Dynamic Locations

Where static resolution is impossible, callables could receive optional
analyzer metadata:

```python
@may_raise(DatabaseUnavailable)
async def load_session(...) -> Session:
    ...
```

This metadata describes only possible Python exceptions. It replaces neither
registry membership nor the explicit HTTP declaration through
`responses=registry.responses(...)`.

Whether this uses a decorator, `Annotated` metadata, or a separate manifest
remains open. This extension should not be mandatory for the first version.

## Confidence Levels

Every finding receives a machine-readable evidence class:

| Level | Meaning |
| --- | --- |
| `certain` | A direct, unambiguously resolved `raise` expression. |
| `declared` | Explicit metadata on an examined callable. |
| `possible` | A finding inferred through the call graph or dynamic type resolution. |
| `observed` | Actually propagated to the application during a test. |

By default, CI should fail only for undocumented `certain` and `observed`
faults. `possible` findings are initially emitted as warnings so that uncertain
analysis does not produce unnecessary false positives.

## Possible API

A Python API could produce a report without modifying the application:

```python
report = registry.audit(
    app,
    packages=["myapp"],
    follow_calls=True,
)

report.raise_for_missing_faults()
```

For CI and local development, a CLI entry point would be particularly useful:

```console
fastapi-faults audit myapp.main:app
```

Example output:

```text
GET /sessions/{session_id}
  declared: SessionNotFound
  certain:  SessionNotFound, DatabaseUnavailable
  missing:  DatabaseUnavailable
    myapp/services/sessions.py:48 via get_session -> load_session
```

The report should also be exportable as JSON so that IDEs and CI systems can
map findings to specific source-code lines.

## Interaction with Runtime Auditing

Static analysis complements the existing `assert_no_undeclared_faults()`
concept:

- static analysis finds possible paths that have not yet been executed;
- runtime auditing confirms paths that actually occurred;
- `responses=registry.responses(...)` remains the binding HTTP contract.

Both sources could later be combined into a shared audit report. A runtime
finding has stronger evidence than a static inference.

## Non-Goals

- No guarantee of finding every theoretically possible Python exception.
- No automatic addition of discovered faults to OpenAPI.
- No change to FastAPI decorator signatures.
- No publication of internal infrastructure errors as domain contracts.
- No mandatory analysis of site-packages or code without available Python
  source.

## Technical Risks

- FastAPI's internal `dependant` structure can change between versions.
- `inspect.getsource()` is not always available for generated, interactive, or
  compiled code.
- Control-flow analysis for `try`/`except` can quickly become complex and slow.
- Overly aggressive call-graph resolution produces false positives; overly
  conservative resolution misses relevant paths.
- Source-code paths and function names in the report must not contain sensitive
  runtime data.

## Recommended Stages

1. Analyze only the FastAPI dependency tree and direct `raise` expressions.
2. Stabilize origin, confidence levels, and JSON reporting.
3. Add a bounded call graph within explicitly selected application packages.
4. Optionally introduce explicit metadata for dynamic call sites.
5. Combine static and observed runtime findings in one report.

## Definition of Done for an Initial Version

- All HTTP routes and arbitrarily deep FastAPI dependencies are traversed.
- Direct raises of registered exception classes are reported with file and
  line number.
- Declared and certainly detected faults are compared by identity.
- Undeclared certain findings can cause CI to fail.
- Uncertain findings remain warnings by default.
- Missing source code results in an explained `unknown`, not a crash.
- Recursive dependencies and callables are handled without infinite loops.
- The analyzer changes neither app, routes, registry, nor OpenAPI document.
- Unit and integration tests cover direct raises, multiple dependency levels,
  caught exceptions, decorators, callable classes, and unknown dynamic calls.

## Open Questions

- Should the first version appear only as a CLI/test tool, or also as a method
  on `FaultRegistry`?
- Which evidence levels should block CI by default?
- How precise must the first control-flow analysis for `try`/`except` be?
- Should explicit callable metadata become part of the public API?
- Which package boundaries and depth limits are sensible, safe defaults?
