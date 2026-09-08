# Missing work for v1

This file tracks the gap between the current implementation and the normative
requirements in `SPEC.md`. A checkbox is closed only when the behavior and its
tests are committed together.

## Release blockers

- [x] Complete HTTP `FaultRouter` ergonomics.
  - Accept `raises` on every FastAPI HTTP path-operation decorator.
  - Union outer-router, inner-router, and operation faults in deterministic,
    identity-preserving order.
  - Preserve FastAPI signatures and route metadata through nested and repeated
    `include_router()` calls.
  - Support synchronous and asynchronous endpoints and custom response classes.
- [x] Validate the installed application graph.
  - Reject routes whose fault definitions are absent from the installed
    application registry.
  - Perform validation during installation, before the first request or
    OpenAPI generation.
- [x] Add the stock `APIRouter` escape hatch.
  - Implement `FaultRegistry.responses(*faults)`.
  - Reuse the same response compiler as `FaultRouter`.
- [x] Implement the OpenAPI 3.1 compiler.
  - Add the reusable `Problem` schema and deterministic per-fault components.
  - Emit `application/problem+json` responses for declared faults.
  - Emit a discriminated `oneOf` for multiple faults sharing a status.
  - Include typed extensions, examples, response headers, and descriptions.
  - Replace FastAPI's default request-validation response when normalization is
    enabled.
  - Preserve unrelated manual responses, media types, links, headers, and
    pre-existing custom OpenAPI wrappers.
  - Fail loudly on component or manual problem-schema conflicts.
  - Preserve FastAPI OpenAPI caching.
- [x] Prove runtime/OpenAPI parity with contract tests.
  - Validate generated documents with an independent OpenAPI 3.1 validator.
  - Validate real runtime payloads against their exact documented schema.
  - Assert status, media type, constants, examples, and documented headers.

## Hardening

- [x] Add `tests/helpers.py` helpers for contract and undeclared-fault
  assertions.
- [x] Add focused integration coverage for framework 404/405 responses,
  request validation in every input location, response validation, debug mode,
  callback failures, custom handlers, and custom response classes.
- [x] Add automated FastAPI decorator-signature parity checks.
- [x] Exercise the declared lowest and newest compatible FastAPI, Starlette,
  and Pydantic versions in CI without rewriting the committed lockfile.
- [ ] Run the complete CI workflow on the hosted repository.

## Documentation and packaging

- [x] Replace the placeholder README with the verified minimal example and API
  guidance from `SPEC.md`.
- [x] Add the runnable `examples/minimal` application.
- [x] Add a changelog and record the initial public contract.
- [x] Complete package metadata with author, classifiers, project URLs, and
  repository links.
- [x] Choose and add the project license and its package classifier.
- [x] Install the built wheel in a clean environment and run the documented
  minimal example there.

## Already implemented

- [x] Python 3.12-3.14 project and CI foundation.
- [x] Immutable `Fault`, `Problem`, and `WebSocketFault` definitions.
- [x] Composable feature registries with collision and MRO handling.
- [x] RFC 9457 runtime rendering and safe callback fallbacks.
- [x] Domain, HTTP exception, request-validation, response-validation, and
  unhandled-error handlers.
- [x] WebSocket handshake and accepted-connection fault handling.
- [x] WebSocket limitations and review questions in `WEBSOCKETS.md`.
