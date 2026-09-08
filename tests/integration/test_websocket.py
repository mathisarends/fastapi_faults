from collections.abc import Callable
from typing import cast

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.types import Message, Scope
from starlette.websockets import WebSocketDisconnect

from fastapi_faults import (
    Fault,
    FaultConfigurationError,
    FaultRegistry,
    WebSocketFault,
)
from fastapi_faults.router import deny_handshake


class SessionNotFound(Exception):
    pass


class SessionExpired(Exception):
    pass


def http_fault() -> Fault[SessionNotFound]:
    return Fault(
        SessionNotFound,
        status=404,
        code="session_not_found",
        title="Session not found",
    )


def websocket_fault(
    *, reason: str | Callable[[SessionExpired], str | None] = "Session expired"
) -> WebSocketFault[SessionExpired]:
    return WebSocketFault(SessionExpired, close_code=4001, reason=reason)


def test_websocket_fault_closes_an_accepted_connection() -> None:
    expired = websocket_fault()
    registry = FaultRegistry(faults=[], websocket_faults=[expired])
    router = registry.router()

    @router.websocket("/events", closes=[expired])
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        raise SessionExpired

    app = FastAPI()
    app.include_router(router)

    with (
        TestClient(app).websocket_connect("/events") as connection,
        pytest.raises(WebSocketDisconnect) as error,
    ):
        connection.receive_text()

    assert error.value.code == 4001
    assert error.value.reason == "Session expired"


def test_websocket_fault_denies_handshake_with_problem_details() -> None:
    missing = http_fault()
    feature_registry = FaultRegistry(faults=[missing], name="sessions")
    application_registry = FaultRegistry.merge(
        feature_registry,
        name="api",
        type_base="https://api.example.com/problems",
    )
    router = feature_registry.router()

    @router.websocket("/events", handshake_raises=[missing])
    async def events(websocket: WebSocket) -> None:
        raise SessionNotFound

    app = FastAPI()
    app.include_router(router)
    application_registry.install(app)

    with (
        pytest.raises(WebSocketDenialResponse) as error,
        TestClient(app).websocket_connect("/events"),
    ):
        pass

    assert error.value.status_code == 404
    assert error.value.headers["content-type"] == "application/problem+json"
    assert error.value.json() == {
        "type": "https://api.example.com/problems/session_not_found",
        "title": "Session not found",
        "status": 404,
        "code": "session_not_found",
    }


def test_router_level_websocket_faults_are_deduplicated() -> None:
    missing = http_fault()
    expired = websocket_fault()
    registry = FaultRegistry(
        faults=[missing],
        websocket_faults=[expired],
        type_base="https://api.example.com/problems",
    )
    router = registry.router(
        handshake_raises=[missing, missing], closes=[expired, expired]
    )

    @router.websocket("/events", handshake_raises=[missing], closes=[expired])
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        raise SessionExpired

    app = FastAPI()
    app.include_router(router)

    with (
        TestClient(app).websocket_connect("/events") as connection,
        pytest.raises(WebSocketDisconnect) as error,
    ):
        connection.receive_text()

    assert error.value.code == 4001


def test_reason_callback_failure_closes_with_1011_without_reason() -> None:
    def broken_reason(_exception: SessionExpired) -> str:
        raise RuntimeError("secret callback detail")

    expired = websocket_fault(reason=broken_reason)
    registry = FaultRegistry(faults=[], websocket_faults=[expired])
    router = registry.router(closes=[expired])

    @router.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        raise SessionExpired

    app = FastAPI()
    app.include_router(router)

    with (
        TestClient(app).websocket_connect("/events") as connection,
        pytest.raises(WebSocketDisconnect) as error,
    ):
        connection.receive_text()

    assert error.value.code == 1011
    assert error.value.reason == ""


def test_undeclared_exception_is_not_converted_to_close() -> None:
    registry = FaultRegistry(faults=[])
    router = registry.router()

    @router.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        raise SessionExpired

    app = FastAPI()
    app.include_router(router)

    with (
        pytest.raises(SessionExpired),
        TestClient(app).websocket_connect("/events") as connection,
    ):
        connection.receive_text()


def test_installed_handler_still_rejects_undeclared_endpoint_fault() -> None:
    expired = websocket_fault()
    registry = FaultRegistry(faults=[], websocket_faults=[expired])
    router = registry.router()

    @router.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        raise SessionExpired

    app = FastAPI()
    app.include_router(router)
    registry.install(
        app,
        include_validation_error=False,
        include_http_exceptions=False,
        include_unhandled_error=False,
    )

    with (
        pytest.raises(SessionExpired),
        TestClient(app).websocket_connect("/events") as connection,
    ):
        connection.receive_text()


def test_undeclared_pre_accept_fault_is_re_raised() -> None:
    registry = FaultRegistry(faults=[])
    router = registry.router()

    @router.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        del websocket
        raise SessionNotFound

    app = FastAPI()
    app.include_router(router)

    with pytest.raises(SessionNotFound), TestClient(app).websocket_connect("/events"):
        pass


def test_handshake_fault_requires_resolved_problem_type() -> None:
    missing = http_fault()
    registry = FaultRegistry(faults=[missing])
    router = registry.router()

    @router.websocket("/events", handshake_raises=[missing])
    async def events(websocket: WebSocket) -> None:
        del websocket
        raise SessionNotFound

    app = FastAPI()
    app.include_router(router)

    with (
        pytest.raises(FaultConfigurationError, match="resolved problem type"),
        TestClient(app).websocket_connect("/events"),
    ):
        pass


def test_fault_after_explicit_close_is_not_handled_twice() -> None:
    expired = websocket_fault()
    registry = FaultRegistry(faults=[], websocket_faults=[expired])
    router = registry.router(closes=[expired])

    @router.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close(code=1000)
        raise SessionExpired

    app = FastAPI()
    app.include_router(router)

    with (
        pytest.raises(SessionExpired),
        TestClient(app).websocket_connect("/events") as connection,
    ):
        connection.receive_text()


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("handshake_raises", [object()]),
        ("closes", [object()]),
    ],
)
def test_router_rejects_wrong_fault_kind(parameter: str, value: object) -> None:
    factory = cast("Callable[..., object]", FaultRegistry(faults=[]).router)

    with pytest.raises(FaultConfigurationError):
        factory(**{parameter: value})


def test_router_rejects_fault_from_another_registry() -> None:
    missing = http_fault()
    expired = websocket_fault()
    registry = FaultRegistry(faults=[])

    with pytest.raises(FaultConfigurationError):
        registry.router(handshake_raises=[missing])
    with pytest.raises(FaultConfigurationError):
        registry.router(closes=[expired])


@pytest.mark.anyio
async def test_handshake_denial_requires_asgi_extension() -> None:
    missing = http_fault()
    registry = FaultRegistry(
        faults=[missing], type_base="https://api.example.com/problems"
    )

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(_message: Message) -> None:
        return None

    websocket = WebSocket(
        cast(
            "Scope",
            {
                "type": "websocket",
                "path": "/events",
                "headers": [],
                "extensions": {},
            },
        ),
        receive=receive,
        send=send,
    )

    with pytest.raises(RuntimeError, match=r"websocket\.http\.response"):
        await deny_handshake(websocket, missing, SessionNotFound(), registry)


def test_outer_router_websocket_close_default_applies_after_lazy_inclusion() -> None:
    expired = WebSocketFault(SessionExpired, close_code=4001, reason="Expired")
    registry = FaultRegistry(faults=[], websocket_faults=[expired])
    inner = registry.router()

    @inner.websocket("/events")
    async def endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        raise SessionExpired

    outer = registry.router(prefix="/outer", closes=[expired])
    outer.include_router(inner)
    app = FastAPI()
    app.include_router(outer)
    registry.install(
        app,
        include_validation_error=False,
        include_http_exceptions=False,
        include_unhandled_error=False,
    )

    with (
        pytest.raises(WebSocketDisconnect) as error,
        TestClient(app).websocket_connect("/outer/events") as websocket,
    ):
        websocket.receive_text()

    assert error.value.code == 4001
    assert error.value.reason == "Expired"


def test_outer_router_handshake_default_applies_after_lazy_inclusion() -> None:
    missing = Fault(
        SessionNotFound,
        status=404,
        code="session_not_found",
        title="Session not found",
    )
    registry = FaultRegistry(
        faults=[missing], type_base="https://example.test/problems"
    )
    inner = registry.router()

    @inner.websocket("/events")
    async def endpoint(websocket: WebSocket) -> None:
        del websocket
        raise SessionNotFound

    outer = registry.router(prefix="/outer", handshake_raises=[missing])
    outer.include_router(inner)
    app = FastAPI()
    app.include_router(outer)
    registry.install(app)

    with (
        pytest.raises(WebSocketDenialResponse) as error,
        TestClient(app).websocket_connect("/outer/events"),
    ):
        pass

    assert error.value.status_code == 404
    assert error.value.json()["code"] == "session_not_found"
