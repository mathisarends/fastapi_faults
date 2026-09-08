import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, cast

from fastapi import FastAPI, Request
from starlette.routing import compile_path
from starlette.types import ExceptionHandler

from .fault import Fault
from .openapi import _effective_http_contracts
from .registry import AnyFault, FaultRegistry
from .router import _INSTALLED_REGISTRY_STATE_KEY
from .types import FaultConfigurationError


class ResponseLike(Protocol):
    status_code: int

    @property
    def headers(self) -> Mapping[str, str]: ...

    def json(self) -> object: ...


@dataclass(frozen=True, slots=True)
class UndeclaredFault:
    operation_id: str
    path: str
    exception_class: type[Exception]
    fault: AnyFault


def assert_problem(
    response: ResponseLike,
    fault: Fault[Any],
    *,
    type_uri: str,
) -> dict[str, Any]:
    if response.status_code != fault.status:
        raise AssertionError(
            f"expected status {fault.status}, received {response.status_code}"
        )
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/problem+json":
        raise AssertionError(
            "expected content type 'application/problem+json', "
            f"received {content_type!r}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise AssertionError("expected the problem response body to be a JSON object")
    expected = {
        "type": type_uri,
        "title": fault.title,
        "status": fault.status,
        "code": fault.code,
    }
    mismatches = {
        name: (value, payload.get(name))
        for name, value in expected.items()
        if payload.get(name) != value
    }
    if mismatches:
        raise AssertionError(f"problem response constants differ: {mismatches!r}")
    return cast("dict[str, Any]", payload)


def assert_openapi_contract(app: FastAPI) -> None:
    registry = _installed_registry(app)
    document = app.openapi()
    paths = document.get("paths", {})
    for path, methods, faults in _effective_http_contracts(app):
        for method in methods:
            operation = paths.get(path, {}).get(method.lower())
            if operation is None:
                continue
            responses = operation.get("responses", {})
            for fault in faults:
                response = responses.get(str(fault.status), {})
                media = response.get("content", {}).get("application/problem+json", {})
                schema = media.get("schema", {})
                reference = f"#/components/schemas/{fault.effective_schema_name}"
                if not _schema_contains_reference(schema, fault.code, reference):
                    msg = (
                        f"{method} {path} does not document fault {fault.code!r} "
                        "in its application/problem+json response"
                    )
                    raise AssertionError(msg)
                if registry._type_uri_for(fault) is None:
                    raise AssertionError(f"fault {fault.code!r} has no resolved type")


@asynccontextmanager
async def assert_no_undeclared_faults(
    app: FastAPI,
) -> AsyncIterator[list[UndeclaredFault]]:
    if app.middleware_stack is not None:
        msg = "assert_no_undeclared_faults must be entered before the first request"
        raise FaultConfigurationError(msg)
    registry = _installed_registry(app)
    contracts = _compiled_route_matchers(app)
    findings: list[UndeclaredFault] = []
    originals: dict[type[Exception], ExceptionHandler] = {}

    for exception_class in {fault.exception for fault in registry.faults}:
        existing_handler = app.exception_handlers.get(exception_class)
        if existing_handler is None:
            continue
        originals[exception_class] = existing_handler
        app.add_exception_handler(
            exception_class,
            _recording_handler(registry, contracts, findings, existing_handler),
        )

    try:
        yield findings
    finally:
        for exception_class, saved_handler in originals.items():
            app.add_exception_handler(exception_class, saved_handler)
        app.middleware_stack = None

    if findings:
        details = "; ".join(
            f"{finding.operation_id} ({finding.path}) raised "
            f"{finding.exception_class.__qualname__} as {finding.fault.code}"
            for finding in findings
        )
        raise AssertionError(f"undeclared faults observed: {details}")


@dataclass(frozen=True, slots=True)
class _RouteMatcher:
    pattern: Any
    methods: frozenset[str]
    operation_id: str
    path: str
    faults: tuple[AnyFault, ...]


def _recording_handler(
    registry: FaultRegistry,
    contracts: tuple[_RouteMatcher, ...],
    findings: list[UndeclaredFault],
    original: ExceptionHandler,
) -> ExceptionHandler:
    async def handler(connection: Any, exception: Exception) -> Any:
        if isinstance(connection, Request):
            fault = registry.resolve(exception)
            contract = _match_contract(
                contracts, connection.url.path, connection.method
            )
            if (
                fault is not None
                and contract is not None
                and not any(declared is fault for declared in contract.faults)
            ):
                findings.append(
                    UndeclaredFault(
                        operation_id=contract.operation_id,
                        path=contract.path,
                        exception_class=type(exception),
                        fault=fault,
                    )
                )
        result = cast("Callable[[Any, Exception], Any]", original)(
            connection, exception
        )
        if inspect.isawaitable(result):
            return await result
        return result

    return cast("ExceptionHandler", handler)


def _compiled_route_matchers(app: FastAPI) -> tuple[_RouteMatcher, ...]:
    document = app.openapi()
    result: list[_RouteMatcher] = []
    for path, methods, faults in _effective_http_contracts(app):
        pattern, _, _ = compile_path(path)
        for method in methods:
            operation = document.get("paths", {}).get(path, {}).get(method.lower())
            if operation is None:
                continue
            operation_id = operation.get("operationId", f"{method} {path}")
            result.append(
                _RouteMatcher(
                    pattern=pattern,
                    methods=frozenset({method}),
                    operation_id=operation_id,
                    path=path,
                    faults=faults,
                )
            )
    return tuple(result)


def _match_contract(
    contracts: tuple[_RouteMatcher, ...], path: str, method: str
) -> _RouteMatcher | None:
    return next(
        (
            contract
            for contract in contracts
            if method in contract.methods and contract.pattern.fullmatch(path)
        ),
        None,
    )


def _installed_registry(app: FastAPI) -> FaultRegistry:
    registry = getattr(app.state, _INSTALLED_REGISTRY_STATE_KEY, None)
    if not isinstance(registry, FaultRegistry):
        msg = "install a FaultRegistry before using fastapi_faults.testing helpers"
        raise FaultConfigurationError(msg)
    return registry


def _schema_contains_reference(
    schema: Mapping[str, Any], code: str, reference: str
) -> bool:
    if schema.get("$ref") == reference:
        return True
    discriminator = schema.get("discriminator", {})
    if not isinstance(discriminator, Mapping):
        return False
    mapping = discriminator.get("mapping", {})
    return isinstance(mapping, Mapping) and mapping.get(code) == reference
