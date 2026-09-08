import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import cast

from fastapi import FastAPI, Request, WebSocket
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import ExceptionHandler

from .openapi import install_openapi
from .registry import FaultRegistry
from .rendering import render_problem
from .router import (
    _ENDPOINT_ENTERED_SCOPE_KEY,
    _INSTALLED_REGISTRY_STATE_KEY,
    _deny_handshake,
    _effective_websocket_metadata,
    _get_websocket_metadata,
    _iter_http_contracts,
    _iter_websocket_contracts,
    _resolve_declared,
)
from .types import FaultConfigurationError, JsonValue

logger = logging.getLogger("fastapi_faults")


def install_handlers(
    registry: FaultRegistry,
    app: FastAPI,
    *,
    include_validation_error: bool,
    include_http_exceptions: bool,
    include_unhandled_error: bool,
) -> None:
    """Install handlers once while preserving user-owned handler conflicts."""
    installed = getattr(app.state, _INSTALLED_REGISTRY_STATE_KEY, None)
    if installed is registry:
        return
    if installed is not None:
        msg = "a different FaultRegistry is already installed on this application"
        raise FaultConfigurationError(msg)
    if app.openapi_schema is not None:
        msg = "install FaultRegistry before generating or caching OpenAPI"
        raise FaultConfigurationError(msg)

    registry._require_resolved()
    _validate_route_contracts(registry, app)
    if (
        include_validation_error or include_unhandled_error
    ) and registry.type_base is None:
        msg = (
            "type_base is required when validation or unhandled-error normalization "
            "is enabled"
        )
        raise FaultConfigurationError(msg)

    domain_classes = {
        *(fault.exception for fault in registry.faults),
        *(fault.exception for fault in registry.websocket_faults),
    }
    for exception_class in domain_classes:
        _ensure_handler_available(app, exception_class)

    domain_handler = _domain_handler(registry)
    for exception_class in domain_classes:
        app.add_exception_handler(exception_class, domain_handler)

    if include_http_exceptions:
        _ensure_default_or_available(app, HTTPException, http_exception_handler)
        app.add_exception_handler(HTTPException, _http_exception_handler)
    if include_validation_error:
        _ensure_default_or_available(
            app, RequestValidationError, request_validation_exception_handler
        )
        app.add_exception_handler(
            RequestValidationError, _request_validation_handler(registry)
        )
        _ensure_handler_available(app, ResponseValidationError)
        app.add_exception_handler(
            ResponseValidationError, _response_validation_handler(registry)
        )
    if include_unhandled_error:
        _ensure_handler_available(app, Exception)
        app.add_exception_handler(Exception, _unhandled_handler(registry))

    install_openapi(
        registry,
        app,
        include_validation_error=include_validation_error,
    )
    setattr(app.state, _INSTALLED_REGISTRY_STATE_KEY, registry)


def _validate_route_contracts(registry: FaultRegistry, app: FastAPI) -> None:
    for http_route, http_faults in _iter_http_contracts(app.router):
        for http_fault in http_faults:
            if not registry._contains(http_fault):
                msg = (
                    f"route {http_route.path!r} declares fault "
                    f"{http_fault.code!r}, but the "
                    "installed registry does not contain that exact definition"
                )
                raise FaultConfigurationError(msg)

    for websocket_route, handshake_faults, close_faults in _iter_websocket_contracts(
        app.router
    ):
        for handshake_fault in handshake_faults:
            if not registry._contains(handshake_fault):
                msg = (
                    f"WebSocket route {websocket_route.path!r} declares handshake "
                    f"fault {handshake_fault.code!r}, but the installed registry "
                    "does not contain that exact definition"
                )
                raise FaultConfigurationError(msg)
        for close_fault in close_faults:
            if not registry._contains_websocket(close_fault):
                msg = (
                    f"WebSocket route {websocket_route.path!r} declares close code "
                    f"{close_fault.close_code}, but the installed registry does "
                    "not contain that exact definition"
                )
                raise FaultConfigurationError(msg)


def _domain_handler(registry: FaultRegistry) -> ExceptionHandler:
    async def handler(
        connection: Request | WebSocket, exception: Exception
    ) -> Response | None:
        if isinstance(connection, WebSocket):
            await _handle_dependency_fault(connection, exception, registry)
            return None

        fault = registry.resolve(exception)
        if fault is None:
            raise exception
        type_uri = registry._type_uri_for(fault)
        if type_uri is None:
            msg = f"fault {fault.code!r} has no resolved problem type URI"
            raise FaultConfigurationError(msg)
        try:
            problem, headers = render_problem(fault, exception, type_uri=type_uri)
        except Exception:
            logger.exception(
                "Fault rendering callback failed",
                extra={
                    "code": fault.code,
                    "exception_class": type(exception).__qualname__,
                },
            )
            return _internal_error_response(registry)
        return _problem_response(problem.as_dict(), fault.status, headers=headers)

    return cast("ExceptionHandler", handler)


async def _handle_dependency_fault(
    websocket: WebSocket, exception: Exception, registry: FaultRegistry
) -> None:
    if websocket.scope.get(_ENDPOINT_ENTERED_SCOPE_KEY):
        raise exception
    route = websocket.scope.get("route")
    metadata = _get_websocket_metadata(getattr(route, "endpoint", None))
    if metadata is None:
        raise exception
    metadata = _effective_websocket_metadata(websocket, metadata)
    fault = _resolve_declared(exception, metadata.handshake_raises)
    if fault is None:
        raise exception
    await _deny_handshake(websocket, fault, exception, registry)


async def _http_exception_handler(request: Request, exception: Exception) -> Response:
    del request
    if not isinstance(exception, HTTPException):
        raise exception
    status = exception.status_code
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "HTTP Error"
    detail = exception.detail if isinstance(exception.detail, str) else None
    payload: dict[str, JsonValue] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": f"http_{status}",
    }
    if detail is not None:
        payload["detail"] = detail
    return _problem_response(payload, status, headers=exception.headers)


def _request_validation_handler(registry: FaultRegistry) -> ExceptionHandler:
    async def handler(request: Request, exception: Exception) -> Response:
        del request
        if not isinstance(exception, RequestValidationError):
            raise exception
        errors: list[JsonValue] = []
        for error in exception.errors():
            errors.append(_validation_error(error))
        payload: dict[str, JsonValue] = {
            "type": _builtin_type(registry, "request_validation_error"),
            "title": "Request validation failed",
            "status": 422,
            "code": "request_validation_error",
            "errors": errors,
        }
        return _problem_response(payload, 422)

    return cast("ExceptionHandler", handler)


def _response_validation_handler(registry: FaultRegistry) -> ExceptionHandler:
    async def handler(request: Request, exception: Exception) -> Response:
        del request
        if not isinstance(exception, ResponseValidationError):
            raise exception
        logger.error(
            "FastAPI response validation failed",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        return _internal_error_response(registry)

    return cast("ExceptionHandler", handler)


def _unhandled_handler(registry: FaultRegistry) -> ExceptionHandler:
    async def handler(
        connection: Request | WebSocket, exception: Exception
    ) -> Response:
        if isinstance(connection, WebSocket):
            raise exception
        logger.error(
            "Unhandled application exception",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        return _internal_error_response(registry)

    return cast("ExceptionHandler", handler)


def _validation_error(error: dict[str, object]) -> JsonValue:
    location = error.get("loc")
    parts = tuple(location) if isinstance(location, tuple | list) else ()
    source = parts[0] if parts else None
    result: dict[str, JsonValue] = {
        "code": str(error.get("type", "validation_error")),
        "detail": str(error.get("msg", "Invalid input")),
    }
    if source == "body":
        pointer = "#/" + "/".join(_escape_pointer(part) for part in parts[1:])
        result["pointer"] = pointer.rstrip("/") or "#"
    elif source in {"path", "query", "header", "cookie"} and len(parts) > 1:
        result["parameter"] = str(parts[-1])
        result["in"] = cast("str", source)
    return result


def _escape_pointer(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _internal_error_response(registry: FaultRegistry) -> Response:
    return _problem_response(
        {
            "type": _builtin_type(registry, "internal_server_error"),
            "title": "Internal Server Error",
            "status": 500,
            "code": "internal_server_error",
        },
        500,
    )


def _builtin_type(registry: FaultRegistry, code: str) -> str:
    if registry.type_base is None:
        msg = f"type_base is required to render built-in problem {code!r}"
        raise FaultConfigurationError(msg)
    return f"{registry.type_base}/{code}"


def _problem_response(
    payload: dict[str, JsonValue],
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return JSONResponse(
        payload,
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def _ensure_handler_available(app: FastAPI, exception_class: type[Exception]) -> None:
    if exception_class in app.exception_handlers:
        msg = (
            f"application already defines an exception handler for "
            f"{exception_class.__qualname__}"
        )
        raise FaultConfigurationError(msg)


def _ensure_default_or_available(
    app: FastAPI,
    exception_class: type[Exception],
    known_default: object,
) -> None:
    existing = app.exception_handlers.get(exception_class)
    if existing is not None and existing is not known_default:
        msg = (
            f"application already defines an exception handler for "
            f"{exception_class.__qualname__}"
        )
        raise FaultConfigurationError(msg)
