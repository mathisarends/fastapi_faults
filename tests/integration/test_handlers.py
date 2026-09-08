from collections.abc import Callable
from typing import cast

import pytest
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.testclient import WebSocketDenialResponse

from fastapi_faults import (
    Fault,
    FaultConfigurationError,
    FaultRegistry,
    WebSocketFault,
)


class DomainError(Exception):
    pass


class SessionNotFound(DomainError):
    pass


class SessionExpired(DomainError):
    pass


def make_fault(
    exception: type[Exception] = SessionNotFound,
    *,
    detail: object = None,
    headers: object = None,
) -> Fault[Exception]:
    factory = cast("Callable[..., Fault[Exception]]", Fault)
    return factory(
        exception,
        status=404,
        code="session_not_found",
        title="Session not found",
        detail=detail,
        headers=headers,
    )


def make_registry(*faults: Fault[Exception]) -> FaultRegistry:
    return FaultRegistry(
        faults=list(faults), type_base="https://api.example.com/problems"
    )


def test_installed_domain_handler_returns_problem_details() -> None:
    fault = make_fault(
        detail="The requested session does not exist.",
        headers={"Retry-After": "10"},
    )
    registry = make_registry(fault)
    app = FastAPI()

    @app.get("/session")
    async def session() -> None:
        raise SessionNotFound

    registry.install(app)
    response = TestClient(app).get("/session")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "10"
    assert response.json() == {
        "type": "https://api.example.com/problems/session_not_found",
        "title": "Session not found",
        "status": 404,
        "code": "session_not_found",
        "detail": "The requested session does not exist.",
    }


def test_domain_callback_failure_returns_safe_internal_problem() -> None:
    def broken_detail(_exception: Exception) -> str:
        raise RuntimeError("secret mapping failure")

    registry = make_registry(make_fault(detail=broken_detail))
    app = FastAPI()

    @app.get("/session")
    async def session() -> None:
        raise SessionNotFound

    registry.install(app)
    response = TestClient(app).get("/session")

    assert response.status_code == 500
    assert response.json() == {
        "type": "https://api.example.com/problems/internal_server_error",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal_server_error",
    }
    assert "secret" not in response.text


@pytest.mark.parametrize(
    ("status", "detail", "expected_title", "has_detail"),
    [
        (401, "Authentication required", "Unauthorized", True),
        (418, {"unsafe": "structured"}, "I'm a Teapot", False),
        (499, "Custom status", "HTTP Error", True),
    ],
)
def test_http_exceptions_are_normalized_without_structured_detail(
    status: int, detail: object, expected_title: str, has_detail: bool
) -> None:
    app = FastAPI()

    @app.get("/failure")
    async def failure() -> None:
        raise HTTPException(
            status_code=status,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    make_registry().install(app)
    response = TestClient(app).get("/failure")

    assert response.status_code == status
    assert response.json()["type"] == "about:blank"
    assert response.json()["title"] == expected_title
    assert ("detail" in response.json()) is has_detail
    assert response.headers["www-authenticate"] == "Bearer"


class RequestBody(BaseModel):
    value: int = Field(alias="a/b~c")


def test_request_validation_uses_json_pointer_without_rejected_input() -> None:
    app = FastAPI()

    @app.post("/body")
    async def body(payload: RequestBody) -> None:
        del payload

    make_registry().install(app)
    response = TestClient(app).post("/body", json={"a/b~c": "secret"})

    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["pointer"] == "#/a~1b~0c"
    assert error["code"] == "int_parsing"
    assert "input" not in error
    assert "secret" not in response.text


def test_request_validation_identifies_query_parameter() -> None:
    app = FastAPI()

    @app.get("/query")
    async def query(limit: int = Query()) -> None:
        del limit

    make_registry().install(app)
    response = TestClient(app).get("/query")

    assert response.status_code == 422
    assert response.json()["errors"][0] == {
        "code": "missing",
        "detail": "Field required",
        "parameter": "limit",
        "in": "query",
    }


def test_response_validation_is_a_safe_internal_problem() -> None:
    app = FastAPI()

    @app.get("/invalid", response_model=int)
    async def invalid() -> object:
        return object()

    make_registry().install(app)
    response = TestClient(app, raise_server_exceptions=False).get("/invalid")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_server_error"
    assert "validation" not in response.text.lower()


def test_unhandled_exception_is_a_safe_internal_problem() -> None:
    app = FastAPI()

    @app.get("/failure")
    async def failure() -> None:
        raise RuntimeError("database password")

    make_registry().install(app)
    response = TestClient(app, raise_server_exceptions=False).get("/failure")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_server_error"
    assert "password" not in response.text


def test_websocket_dependency_can_deny_the_handshake() -> None:
    missing = make_fault()
    expired = WebSocketFault(SessionExpired, close_code=4001)
    registry = FaultRegistry(
        faults=[missing],
        websocket_faults=[expired],
        type_base="https://api.example.com/problems",
    )
    router = registry.router()

    async def load_session() -> None:
        raise SessionNotFound

    @router.websocket(
        "/events",
        dependencies=[Depends(load_session)],
        handshake_raises=[missing],
        closes=[expired],
    )
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()

    app = FastAPI()
    app.include_router(router)
    registry.install(app)

    with (
        pytest.raises(WebSocketDenialResponse) as error,
        TestClient(app).websocket_connect("/events"),
    ):
        pass

    assert error.value.status_code == 404
    assert error.value.json()["code"] == "session_not_found"


def test_install_is_idempotent_for_same_registry() -> None:
    registry = make_registry()
    app = FastAPI()

    registry.install(app)
    handlers = dict(app.exception_handlers)
    registry.install(app)

    assert app.exception_handlers == handlers


def test_install_rejects_different_registry() -> None:
    app = FastAPI()
    make_registry().install(app)

    with pytest.raises(FaultConfigurationError, match="different FaultRegistry"):
        make_registry().install(app)


def test_install_rejects_cached_openapi() -> None:
    app = FastAPI()
    app.openapi()

    with pytest.raises(FaultConfigurationError, match="before generating"):
        make_registry().install(app)


def test_install_requires_type_base_for_builtin_normalization() -> None:
    with pytest.raises(FaultConfigurationError, match="type_base"):
        FaultRegistry(faults=[]).install(FastAPI())


def test_install_allows_websocket_only_registry_without_builtin_handlers() -> None:
    registry = FaultRegistry(
        faults=[],
        websocket_faults=[WebSocketFault(SessionExpired, close_code=4001)],
    )
    app = FastAPI()

    registry.install(
        app,
        include_validation_error=False,
        include_http_exceptions=False,
        include_unhandled_error=False,
    )

    assert app.state._fastapi_faults_registry is registry


def test_install_rejects_custom_domain_handler() -> None:
    app = FastAPI()

    @app.exception_handler(SessionNotFound)
    async def custom_handler(_request: object, _exception: Exception) -> JSONResponse:
        return JSONResponse({})

    with pytest.raises(FaultConfigurationError, match="SessionNotFound"):
        make_registry(make_fault()).install(app)


@pytest.mark.parametrize(
    "exception_class",
    [StarletteHTTPException, RequestValidationError, Exception],
)
def test_install_rejects_custom_framework_handler(
    exception_class: type[Exception],
) -> None:
    app = FastAPI()

    async def custom_handler(_request: object, _exception: Exception) -> JSONResponse:
        return JSONResponse({})

    app.add_exception_handler(
        exception_class,
        cast("Callable[[object, Exception], JSONResponse]", custom_handler),
    )

    with pytest.raises(FaultConfigurationError):
        make_registry().install(app)
