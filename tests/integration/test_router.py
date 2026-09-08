from collections.abc import Callable
from inspect import signature
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from fastapi.types import DecoratedCallable

from fastapi_faults import Fault, FaultConfigurationError, FaultRegistry
from fastapi_faults.router import _get_http_metadata, _iter_http_contracts


class FeatureUnavailable(Exception):
    pass


class ResourceMissing(Exception):
    pass


class Conflict(Exception):
    pass


def make_fault(
    exception: type[Exception], code: str, status: int = 400
) -> Fault[Exception]:
    return Fault(exception, status=status, code=code, title=code.replace("_", " "))


def test_http_decorators_accept_raises_and_preserve_fastapi_options() -> None:
    shared = make_fault(FeatureUnavailable, "feature_unavailable", 503)
    missing = make_fault(ResourceMissing, "resource_missing", 404)
    registry = FaultRegistry(faults=[shared, missing], type_base="https://example.test")
    router = registry.router(prefix="/items", raises=[shared])

    @router.get(
        "/{item_id}",
        raises=[missing],
        response_class=PlainTextResponse,
        tags=["items"],
    )
    def get_item(item_id: int) -> str:
        return str(item_id)

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/items/42")
    route, faults = next(_iter_http_contracts(app.router))
    metadata = _get_http_metadata(route.endpoint)

    assert metadata is not None
    assert metadata.raises == (shared, missing)
    assert faults == (shared, missing)
    assert response.status_code == 200
    assert response.text == "42"
    assert route.tags == ["items"]


@pytest.mark.parametrize(
    ("decorator_name", "method"),
    [
        ("get", "GET"),
        ("post", "POST"),
        ("put", "PUT"),
        ("patch", "PATCH"),
        ("delete", "DELETE"),
        ("options", "OPTIONS"),
        ("head", "HEAD"),
        ("trace", "TRACE"),
    ],
)
def test_every_http_decorator_accepts_raises(decorator_name: str, method: str) -> None:
    fault = make_fault(ResourceMissing, "resource_missing", 404)
    registry = FaultRegistry(faults=[fault], type_base="https://example.test")
    router = registry.router()
    decorator = cast(
        "Callable[..., Callable[[DecoratedCallable], DecoratedCallable]]",
        getattr(router, decorator_name),
    )

    @decorator("/resource", raises=[fault])
    async def endpoint() -> None:
        return None

    route = cast("APIRoute", router.routes[0])
    metadata = _get_http_metadata(route.endpoint)
    assert metadata is not None
    assert metadata.raises == (fault,)
    assert route.methods == {method}


def test_nested_and_repeated_inclusion_preserves_order_without_mutation() -> None:
    outer_fault = make_fault(FeatureUnavailable, "feature_unavailable", 503)
    inner_fault = make_fault(ResourceMissing, "resource_missing", 404)
    operation_fault = make_fault(Conflict, "resource_conflict", 409)
    registry = FaultRegistry(
        faults=[outer_fault, inner_fault, operation_fault],
        type_base="https://example.test",
    )
    inner = registry.router(prefix="/inner", raises=[inner_fault])

    @inner.get("/resource", raises=[operation_fault, inner_fault])
    async def endpoint() -> None:
        return None

    outer = registry.router(prefix="/outer", raises=[outer_fault])
    outer.include_router(inner)
    app = FastAPI()
    app.include_router(outer, prefix="/one")
    app.include_router(outer, prefix="/two")

    inner_route = cast("APIRoute", inner.routes[0])
    inner_metadata = _get_http_metadata(inner_route.endpoint)
    outer_contracts = list(_iter_http_contracts(outer))
    copied = list(_iter_http_contracts(app.router))

    assert inner_metadata is not None
    assert inner_metadata.raises == (inner_fault, operation_fault)
    assert outer_contracts[0][1] == (outer_fault, inner_fault, operation_fault)
    assert len(copied) == 2
    assert all(
        faults == (outer_fault, inner_fault, operation_fault) for _, faults in copied
    )


def test_unknown_route_fault_fails_at_registration() -> None:
    known = make_fault(ResourceMissing, "resource_missing", 404)
    unknown = make_fault(Conflict, "resource_conflict", 409)
    router = FaultRegistry(faults=[known], type_base="https://example.test").router()

    with pytest.raises(FaultConfigurationError, match="not in the router registry"):

        @router.get("/resource", raises=[unknown])
        async def endpoint() -> None:
            return None


def test_stock_router_can_include_fault_router_without_losing_metadata() -> None:
    fault = make_fault(ResourceMissing, "resource_missing", 404)
    registry = FaultRegistry(faults=[fault], type_base="https://example.test")
    feature = registry.router()

    @feature.get("/resource", raises=[fault])
    async def endpoint() -> None:
        return None

    stock = APIRouter(prefix="/stock")
    stock.include_router(feature)
    route, faults = next(_iter_http_contracts(stock))
    metadata = _get_http_metadata(route.endpoint)
    assert metadata is not None
    assert metadata.raises == (fault,)
    assert faults == (fault,)


def test_same_callable_can_have_independent_fault_contracts() -> None:
    missing = make_fault(ResourceMissing, "resource_missing", 404)
    conflict = make_fault(Conflict, "resource_conflict", 409)
    registry = FaultRegistry(
        faults=[missing, conflict], type_base="https://example.test"
    )
    router = registry.router()

    async def endpoint() -> None:
        return None

    router.get("/missing", raises=[missing])(endpoint)
    router.get("/conflict", raises=[conflict])(endpoint)

    contracts: dict[str, tuple[Fault[Any], ...]] = {}
    for base_route in router.routes:
        route = cast("APIRoute", base_route)
        metadata = _get_http_metadata(route.endpoint)
        assert metadata is not None
        contracts[route.path] = metadata.raises

    assert contracts == {"/missing": (missing,), "/conflict": (conflict,)}


@pytest.mark.parametrize(
    "decorator_name",
    ["api_route", "get", "post", "put", "patch", "delete", "options", "head", "trace"],
)
def test_http_decorator_signature_tracks_fastapi(decorator_name: str) -> None:
    base = signature(getattr(APIRouter, decorator_name))
    fault_aware = signature(
        getattr(type(FaultRegistry(faults=[]).router()), decorator_name)
    )
    fault_parameters = dict(fault_aware.parameters)

    assert fault_parameters.pop("raises").default == ()
    assert list(fault_parameters) == list(base.parameters)
    for name, parameter in base.parameters.items():
        candidate = fault_parameters[name]
        assert candidate.kind == parameter.kind
        assert candidate.default == parameter.default
