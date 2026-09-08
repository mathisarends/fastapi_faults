import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import Any, cast

from fastapi import APIRouter, WebSocket, params
from fastapi.responses import JSONResponse
from fastapi.types import DecoratedCallable
from starlette.websockets import WebSocketState

from ._types import FaultConfigurationError
from .fault import Fault
from .registry import AnyFault, AnyWebSocketFault, FaultRegistry
from .rendering import render_problem
from .websocket import WebSocketFault

_METADATA_ATTRIBUTE = "__fastapi_faults_websocket__"
_ENDPOINT_ENTERED_SCOPE_KEY = "fastapi_faults.websocket_endpoint_entered"
_INSTALLED_REGISTRY_STATE_KEY = "_fastapi_faults_registry"
_logger = logging.getLogger("fastapi_faults")


@dataclass(frozen=True, slots=True)
class _WebSocketMetadata:
    registry: FaultRegistry
    handshake_raises: tuple[AnyFault, ...]
    closes: tuple[AnyWebSocketFault, ...]


class FaultRouter(APIRouter):
    """A registry-bound FastAPI router with explicit fault contracts."""

    def __init__(
        self,
        *,
        registry: FaultRegistry,
        raises: Sequence[AnyFault] = (),
        handshake_raises: Sequence[AnyFault] = (),
        closes: Sequence[AnyWebSocketFault] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.registry = registry
        self.raises = _validate_http_faults(registry, raises, parameter="raises")
        self.handshake_raises = _validate_http_faults(
            registry, handshake_raises, parameter="handshake_raises"
        )
        self.closes = _validate_websocket_faults(registry, closes)

    def websocket(
        self,
        path: str,
        name: str | None = None,
        *,
        dependencies: Sequence[params.Depends] | None = None,
        handshake_raises: Sequence[AnyFault] = (),
        closes: Sequence[AnyWebSocketFault] = (),
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        route_handshake_faults = _ordered_identity_union(
            self.handshake_raises,
            _validate_http_faults(
                self.registry, handshake_raises, parameter="handshake_raises"
            ),
        )
        route_close_faults = _ordered_identity_union(
            self.closes,
            _validate_websocket_faults(self.registry, closes),
        )
        metadata = _WebSocketMetadata(
            registry=self.registry,
            handshake_raises=route_handshake_faults,
            closes=route_close_faults,
        )

        def decorator(func: DecoratedCallable) -> DecoratedCallable:
            @wraps(func)
            async def endpoint(*args: Any, **kwargs: Any) -> object:
                websocket = _find_websocket(args, kwargs)
                websocket.scope[_ENDPOINT_ENTERED_SCOPE_KEY] = True
                try:
                    result = func(*args, **kwargs)
                    return await cast("Awaitable[object]", result)
                except Exception as exception:
                    if await _handle_endpoint_exception(websocket, exception, metadata):
                        return None
                    raise

            setattr(endpoint, _METADATA_ATTRIBUTE, metadata)
            super(FaultRouter, self).websocket(
                path, name=name, dependencies=dependencies
            )(endpoint)
            return cast("DecoratedCallable", endpoint)

        return decorator


Router = FaultRouter


def _get_websocket_metadata(endpoint: object) -> _WebSocketMetadata | None:
    metadata = getattr(endpoint, _METADATA_ATTRIBUTE, None)
    return metadata if isinstance(metadata, _WebSocketMetadata) else None


def _validate_http_faults(
    registry: FaultRegistry,
    faults: Sequence[AnyFault],
    *,
    parameter: str,
) -> tuple[AnyFault, ...]:
    validated: list[AnyFault] = []
    for index, candidate in enumerate(cast("Sequence[object]", faults)):
        if not isinstance(candidate, Fault):
            msg = f"{parameter}[{index}] must be a Fault instance"
            raise FaultConfigurationError(msg)
        if not registry._contains(candidate):
            msg = (
                f"fault {candidate.code!r} in {parameter} is not in the router registry"
            )
            raise FaultConfigurationError(msg)
        validated.append(candidate)
    return _ordered_identity_union(tuple(validated))


def _validate_websocket_faults(
    registry: FaultRegistry, faults: Sequence[AnyWebSocketFault]
) -> tuple[AnyWebSocketFault, ...]:
    validated: list[AnyWebSocketFault] = []
    for index, candidate in enumerate(cast("Sequence[object]", faults)):
        if not isinstance(candidate, WebSocketFault):
            msg = f"closes[{index}] must be a WebSocketFault instance"
            raise FaultConfigurationError(msg)
        if not registry._contains_websocket(candidate):
            msg = (
                f"WebSocket fault with close code {candidate.close_code} in closes "
                "is not in the router registry"
            )
            raise FaultConfigurationError(msg)
        validated.append(candidate)
    return _ordered_identity_union(tuple(validated))


def _ordered_identity_union[ItemT](*groups: Sequence[ItemT]) -> tuple[ItemT, ...]:
    result: list[ItemT] = []
    seen: set[int] = set()
    for group in groups:
        for item in group:
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(item)
    return tuple(result)


def _find_websocket(args: tuple[Any, ...], kwargs: dict[str, Any]) -> WebSocket:
    for candidate in (*args, *kwargs.values()):
        if isinstance(candidate, WebSocket):
            return candidate
    msg = "FastAPI did not provide a WebSocket argument to the WebSocket endpoint"
    raise RuntimeError(msg)


async def _handle_endpoint_exception(
    websocket: WebSocket,
    exception: Exception,
    metadata: _WebSocketMetadata,
) -> bool:
    if websocket.application_state == WebSocketState.CONNECTING:
        handshake_fault = _resolve_declared(exception, metadata.handshake_raises)
        if handshake_fault is None:
            return False
        await _deny_handshake(websocket, handshake_fault, exception, metadata.registry)
        return True

    if websocket.application_state == WebSocketState.CONNECTED:
        close_fault = _resolve_declared(exception, metadata.closes)
        if close_fault is None:
            return False
        try:
            reason = close_fault._render_reason(exception)
        except Exception:
            _logger.exception(
                "WebSocket fault reason callback failed",
                extra={
                    "close_code": close_fault.close_code,
                    "exception_class": type(exception).__qualname__,
                },
            )
            await websocket.close(code=1011)
            return True
        await websocket.close(code=close_fault.close_code, reason=reason)
        return True

    return False


async def _deny_handshake(
    websocket: WebSocket,
    fault: AnyFault,
    exception: Exception,
    registry: FaultRegistry,
) -> None:
    if "websocket.http.response" not in websocket.scope.get("extensions", {}):
        msg = (
            "WebSocket handshake fault requires the ASGI "
            "websocket.http.response extension"
        )
        raise RuntimeError(msg) from exception
    registry = _effective_registry(websocket, registry, fault)
    type_uri = registry._type_uri_for(fault)
    if type_uri is None:
        msg = f"fault {fault.code!r} has no resolved problem type URI"
        raise FaultConfigurationError(msg)
    problem, headers = render_problem(fault, exception, type_uri=type_uri)
    response = JSONResponse(
        problem.as_dict(),
        status_code=fault.status,
        headers=headers,
        media_type="application/problem+json",
    )
    await websocket.send_denial_response(response)


def _effective_registry(
    websocket: WebSocket, feature_registry: FaultRegistry, fault: AnyFault
) -> FaultRegistry:
    app = websocket.scope.get("app")
    state = getattr(app, "state", None)
    installed = getattr(state, _INSTALLED_REGISTRY_STATE_KEY, None)
    if isinstance(installed, FaultRegistry) and installed._contains(fault):
        return installed
    return feature_registry


def _resolve_declared[FaultT: AnyFault | AnyWebSocketFault](
    exception: Exception, faults: Sequence[FaultT]
) -> FaultT | None:
    by_exception = {fault.exception: fault for fault in faults}
    for exception_class in type(exception).__mro__:
        fault = by_exception.get(exception_class)
        if fault is not None:
            return fault
    return None
