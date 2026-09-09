from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from fastapi_faults.types import FaultConfigurationError

if TYPE_CHECKING:
    from fastapi_faults.registry import AnyFault, FaultRegistry

FAULTS_EXTENSION = "x-fastapi-faults"
INSTALLED_REGISTRY_STATE_KEY = "_fastapi_faults_registry"


@runtime_checkable
class _IncludedRouterRoute(Protocol):
    original_router: APIRouter


def iter_http_contracts(
    router: APIRouter, registry: FaultRegistry
) -> Iterator[tuple[APIRoute, tuple[AnyFault, ...]]]:
    """Yield standard FastAPI routes and faults declared through responses=."""
    yield from _walk_http_contracts(router.routes, registry)


def _walk_http_contracts(
    routes: Sequence[BaseRoute], registry: FaultRegistry
) -> Iterator[tuple[APIRoute, tuple[AnyFault, ...]]]:
    for route in routes:
        if isinstance(route, _IncludedRouterRoute):
            yield from _walk_http_contracts(route.original_router.routes, registry)
            continue
        if isinstance(route, APIRoute):
            yield route, _faults_from_responses(route, registry)


def _faults_from_responses(
    route: APIRoute, registry: FaultRegistry
) -> tuple[AnyFault, ...]:
    by_identity = {str(id(fault)): fault for fault in registry.faults}
    result: list[AnyFault] = []
    seen: set[int] = set()

    for configured_response in route.responses.values():
        response: object = configured_response
        if not isinstance(response, Mapping):
            continue
        identities = response.get(FAULTS_EXTENSION, ())
        if not isinstance(identities, list) or not all(
            isinstance(identity, str) for identity in identities
        ):
            msg = f"route {route.path!r} contains invalid fastapi-faults metadata"
            raise FaultConfigurationError(msg)
        for identity in identities:
            fault = by_identity.get(identity)
            if fault is None:
                msg = (
                    f"route {route.path!r} declares a fault whose exact definition "
                    "is missing from the installed registry"
                )
                raise FaultConfigurationError(msg)
            if id(fault) not in seen:
                seen.add(id(fault))
                result.append(fault)

    return tuple(result)
