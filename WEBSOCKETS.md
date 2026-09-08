# WebSocket support: review notes and caveats

This document records the design boundary for WebSocket faults. `SPEC.md`
remains normative; this file explains the trade-offs that deserve explicit
review before the first release.

## Why WebSockets need a separate contract

An HTTP route has one response with a status, headers, media type, and body. An
accepted WebSocket has messages followed by an optional close frame. RFC 9457
defines HTTP Problem Details and therefore applies directly only while the
WebSocket handshake can still be denied as an HTTP response.

The public API deliberately uses two declarations:

```python
@router.websocket(
    "/sessions/{session_id}/events",
    handshake_raises=[SESSION_NOT_FOUND],
    closes=[SESSION_EXPIRED_WS],
)
```

- `handshake_raises` contains normal HTTP `Fault` definitions.
- `closes` contains `WebSocketFault` definitions.
- HTTP route `raises` is not accepted by the WebSocket decorator.

Using one ambiguous list would hide whether a fault becomes an HTTP response or
a close frame, and would make behavior depend on the line at which `accept()`
happened.

## Handshake denial

Before `websocket.accept()`, the library can render a declared HTTP fault as an
`application/problem+json` denial response. This also covers a declared domain
exception raised by a FastAPI dependency, because dependencies execute before
the endpoint is entered.

Custom denial responses require the ASGI `websocket.http.response` extension.
The capability is supplied by the ASGI server in the connection scope and
cannot reliably be checked at application startup. If it is unavailable, the
library raises an actionable runtime error. It does not silently replace a
declared 401, 404, or 409 response with the generic close-before-accept 403.

The HTTP denial response is not added to OpenAPI because OpenAPI does not model
WebSocket operations.

## After the connection is accepted

After `accept()`, handling a declared `WebSocketFault` sends exactly one close
frame. V1 does not send a JSON error message first. Sending a message would
require the library to own the application's WebSocket message envelope,
ordering, encoding, and acknowledgement rules.

Application close codes are restricted to 4000 through 4999. A client should
branch on the numeric close code. The optional reason is advisory text and must
not be parsed as a machine identifier.

The encoded close reason can occupy at most 123 UTF-8 bytes. This is a byte
limit, not a character limit; non-ASCII text may use multiple bytes. Static
reasons are validated during configuration. Callback results are validated at
runtime.

If a reason callback raises, returns the wrong type, or exceeds the byte limit,
the library closes an accepted socket with code 1011 and an empty reason. It
must not leak the callback exception or partially generated text.

## Exception and connection state

- Only definitions declared on the effective route are handled.
- Exception matching follows Python MRO and chooses the most specific declared
  exception class.
- An undeclared exception is re-raised for the server's normal error handling.
- An exception raised after a close frame was already sent is re-raised; the
  library does not attempt a second close.
- Cancellation and other `BaseException` control flow are never intercepted.
- A registered exception raised by a dependency is a handshake failure. A
  registered exception raised inside the endpoint is classified using the
  actual `WebSocket.application_state`.

## Registry composition

HTTP and WebSocket definitions share the feature registry but have separate
collision domains:

```python
session_faults = FaultRegistry(
    name="sessions",
    faults=[SESSION_NOT_FOUND, SESSION_EXPIRED],
    websocket_faults=[SESSION_EXPIRED_WS],
)
```

The same domain exception may have one HTTP mapping and one WebSocket mapping.
Within WebSocket mappings, exception classes and application close codes must
be unique after composition. Identity-based diamond deduplication behaves the
same way as it does for HTTP faults.

## Documentation boundary

V1 exposes runtime behavior and testable route metadata. It does not generate
AsyncAPI and does not alter OpenAPI. A future AsyncAPI integration can consume
the retained metadata without changing runtime semantics, but it needs a
separate specification covering message schemas and channels.

## Review checklist

- Is failing loudly without the ASGI denial extension preferable to a portable
  but lossy 403 fallback?
- Are application close codes 4000–4999 sufficiently restrictive?
- Should dynamic close reasons remain supported, given their byte limit and
  limited diagnostic value?
- Should a future release offer an opt-in JSON message before closing, or leave
  message envelopes entirely application-owned?
- Is AsyncAPI generation desirable as an optional integration rather than a
  runtime dependency?
