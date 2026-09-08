import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import wraps
from inspect import Parameter, iscoroutinefunction, signature
from typing import Any, cast

from fastapi import APIRouter, WebSocket, params
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.types import DecoratedCallable
from starlette.routing import compile_path
from starlette.websockets import WebSocketState

from ._types import FaultConfigurationError
from .fault import Fault
from .registry import AnyFault, AnyWebSocketFault, FaultRegistry
from .rendering import render_problem
from .websocket import WebSocketFault

_METADATA_ATTRIBUTE = "__fastapi_faults_websocket__"
_HTTP_METADATA_ATTRIBUTE = "__fastapi_faults_http__"
_ENDPOINT_ENTERED_SCOPE_KEY = "fastapi_faults.websocket_endpoint_entered"
_INSTALLED_REGISTRY_STATE_KEY = "_fastapi_faults_registry"
_logger = logging.getLogger("fastapi_faults")


@dataclass(frozen=True, slots=True)
class _WebSocketMetadata:
    registry: FaultRegistry
    handshake_raises: tuple[AnyFault, ...]
    closes: tuple[AnyWebSocketFault, ...]


@dataclass(frozen=True, slots=True)
class _HttpMetadata:
    raises: tuple[AnyFault, ...]


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

    def api_route(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        operation_faults = _validate_http_faults(
            self.registry, raises, parameter="raises"
        )
        register = super().api_route(path, **kwargs)

        def decorator(func: DecoratedCallable) -> DecoratedCallable:
            endpoint = _with_http_metadata(func, operation_faults)
            register(endpoint)
            return func

        return decorator

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        metadata = _get_http_metadata(endpoint)
        operation_faults = metadata.raises if metadata is not None else ()
        effective_faults = _ordered_identity_union(self.raises, operation_faults)
        marked_endpoint = _with_http_metadata(endpoint, effective_faults, replace=True)
        super().add_api_route(path, marked_endpoint, **kwargs)

    def add_api_websocket_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        name: str | None = None,
        *,
        dependencies: Sequence[params.Depends] | None = None,
    ) -> None:
        metadata = _get_websocket_metadata(endpoint)
        if metadata is not None:
            metadata = _WebSocketMetadata(
                registry=metadata.registry,
                handshake_raises=_ordered_identity_union(
                    self.handshake_raises, metadata.handshake_raises
                ),
                closes=_ordered_identity_union(self.closes, metadata.closes),
            )
            endpoint = _with_websocket_metadata(endpoint, metadata)
        super().add_api_websocket_route(
            path,
            endpoint,
            name=name,
            dependencies=dependencies,
        )

    def get(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["GET"], raises=raises, **kwargs)

    def post(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["POST"], raises=raises, **kwargs)

    def put(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["PUT"], raises=raises, **kwargs)

    def patch(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["PATCH"], raises=raises, **kwargs)

    def delete(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["DELETE"], raises=raises, **kwargs)

    def options(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["OPTIONS"], raises=raises, **kwargs)

    def head(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["HEAD"], raises=raises, **kwargs)

    def trace(
        self,
        path: str,
        *,
        raises: Sequence[AnyFault] = (),
        **kwargs: Any,
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=["TRACE"], raises=raises, **kwargs)

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


def _install_http_signatures() -> None:
    for name in (
        "api_route",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "options",
        "head",
        "trace",
    ):
        base = signature(getattr(APIRouter, name))
        parameters = list(base.parameters.values())
        insert_at = next(
            (
                index
                for index, parameter in enumerate(parameters)
                if parameter.name == "responses"
            ),
            len(parameters),
        )
        parameters.insert(
            insert_at + 1,
            Parameter(
                "raises",
                kind=Parameter.KEYWORD_ONLY,
                default=(),
                annotation=Sequence[AnyFault],
            ),
        )
        cast("Any", getattr(FaultRouter, name)).__signature__ = base.replace(
            parameters=parameters
        )


_install_http_signatures()


def _get_websocket_metadata(endpoint: object) -> _WebSocketMetadata | None:
    metadata = getattr(endpoint, _METADATA_ATTRIBUTE, None)
    return metadata if isinstance(metadata, _WebSocketMetadata) else None


def _with_websocket_metadata(
    endpoint: Callable[..., Any], metadata: _WebSocketMetadata
) -> Callable[..., Any]:
    @wraps(endpoint)
    async def marked_endpoint(*args: Any, **kwargs: Any) -> object:
        result = endpoint(*args, **kwargs)
        return await cast("Awaitable[object]", result)

    setattr(marked_endpoint, _METADATA_ATTRIBUTE, metadata)
    return marked_endpoint


def _get_http_metadata(endpoint: object) -> _HttpMetadata | None:
    metadata = getattr(endpoint, _HTTP_METADATA_ATTRIBUTE, None)
    return metadata if isinstance(metadata, _HttpMetadata) else None


def _iter_http_contracts(
    router: APIRouter,
) -> Iterator[tuple[APIRoute, tuple[AnyFault, ...]]]:
    inherited = router.raises if isinstance(router, FaultRouter) else ()
    yield from _walk_http_contracts(router.routes, inherited)


def _walk_http_contracts(
    routes: Sequence[object], inherited: tuple[AnyFault, ...]
) -> Iterator[tuple[APIRoute, tuple[AnyFault, ...]]]:
    for route in routes:
        included = getattr(route, "original_router", None)
        if isinstance(included, APIRouter):
            defaults = included.raises if isinstance(included, FaultRouter) else ()
            yield from _walk_http_contracts(
                included.routes,
                _ordered_identity_union(inherited, defaults),
            )
            continue
        if not isinstance(route, APIRoute):
            continue
        metadata = _get_http_metadata(route.endpoint)
        declared = metadata.raises if metadata is not None else ()
        yield route, _ordered_identity_union(inherited, declared)


def _iter_websocket_contracts(
    router: APIRouter,
) -> Iterator[
    tuple[
        APIWebSocketRoute,
        tuple[AnyFault, ...],
        tuple[AnyWebSocketFault, ...],
    ]
]:
    handshake = router.handshake_raises if isinstance(router, FaultRouter) else ()
    closes = router.closes if isinstance(router, FaultRouter) else ()
    yield from _walk_websocket_contracts(router.routes, handshake, closes)


def _walk_websocket_contracts(
    routes: Sequence[object],
    inherited_handshake: tuple[AnyFault, ...],
    inherited_closes: tuple[AnyWebSocketFault, ...],
) -> Iterator[
    tuple[
        APIWebSocketRoute,
        tuple[AnyFault, ...],
        tuple[AnyWebSocketFault, ...],
    ]
]:
    for route in routes:
        included = getattr(route, "original_router", None)
        if isinstance(included, APIRouter):
            handshake = (
                included.handshake_raises if isinstance(included, FaultRouter) else ()
            )
            closes = included.closes if isinstance(included, FaultRouter) else ()
            yield from _walk_websocket_contracts(
                included.routes,
                _ordered_identity_union(inherited_handshake, handshake),
                _ordered_identity_union(inherited_closes, closes),
            )
            continue
        if not isinstance(route, APIWebSocketRoute):
            continue
        metadata = _get_websocket_metadata(route.endpoint)
        handshake = metadata.handshake_raises if metadata is not None else ()
        closes = metadata.closes if metadata is not None else ()
        yield (
            route,
            _ordered_identity_union(inherited_handshake, handshake),
            _ordered_identity_union(inherited_closes, closes),
        )


def _with_http_metadata(
    endpoint: Callable[..., Any],
    faults: Sequence[AnyFault],
    *,
    replace: bool = False,
) -> Callable[..., Any]:
    existing = _get_http_metadata(endpoint)
    inherited = () if replace or existing is None else existing.raises
    metadata = _HttpMetadata(raises=_ordered_identity_union(inherited, faults))

    if iscoroutinefunction(endpoint):

        @wraps(endpoint)
        async def async_endpoint(*args: Any, **kwargs: Any) -> Any:
            return await endpoint(*args, **kwargs)

        marked = async_endpoint
    else:

        @wraps(endpoint)
        def sync_endpoint(*args: Any, **kwargs: Any) -> Any:
            return endpoint(*args, **kwargs)

        marked = sync_endpoint

    setattr(marked, _HTTP_METADATA_ATTRIBUTE, metadata)
    return marked


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
    metadata = _effective_websocket_metadata(websocket, metadata)
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


def _effective_websocket_metadata(
    websocket: WebSocket, metadata: _WebSocketMetadata
) -> _WebSocketMetadata:
    app = websocket.scope.get("app")
    router = getattr(app, "router", None)
    routes = getattr(app, "routes", None)
    if not isinstance(router, APIRouter) or not isinstance(routes, Sequence):
        return metadata
    contracts = list(_iter_websocket_contracts(router))
    contexts = _effective_websocket_routes(routes)
    if len(contracts) != len(contexts):
        return metadata
    request_path = str(websocket.scope.get("path", ""))
    for (route, handshake, closes), context in zip(contracts, contexts, strict=True):
        original = getattr(context, "original_route", context)
        if original is not route:
            return metadata
        effective_route = getattr(context, "starlette_route", None)
        path = str(
            getattr(effective_route, "path", None)
            or getattr(context, "path", None)
            or route.path
        )
        pattern, _, _ = compile_path(path)
        if pattern.fullmatch(request_path):
            return _WebSocketMetadata(
                registry=metadata.registry,
                handshake_raises=handshake,
                closes=closes,
            )
    return metadata


def _effective_websocket_routes(routes: Sequence[Any]) -> list[object]:
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return [route for route in routes if isinstance(route, APIWebSocketRoute)]
    return [
        context
        for context in iter_route_contexts(routes)
        if isinstance(getattr(context, "original_route", None), APIWebSocketRoute)
    ]


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
