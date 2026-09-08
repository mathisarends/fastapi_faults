import copy
from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError

from .problem import Problem
from .registry import AnyFault, FaultRegistry
from .router import _iter_http_contracts
from .types import FaultConfigurationError, JsonValue

_PROBLEM_MEDIA_TYPE = "application/problem+json"
_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def install_openapi(
    registry: FaultRegistry,
    app: FastAPI,
    *,
    include_validation_error: bool,
) -> None:
    previous_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = previous_openapi()
        compiled = compile_document(
            registry,
            app,
            document,
            include_validation_error=include_validation_error,
        )
        app.openapi_schema = compiled
        return compiled

    cast("Any", app).openapi = openapi


def compile_document(
    registry: FaultRegistry,
    app: FastAPI,
    document: dict[str, Any],
    *,
    include_validation_error: bool,
) -> dict[str, Any]:
    result = copy.deepcopy(document)
    components = result.setdefault("components", {}).setdefault("schemas", {})
    _add_component(components, "Problem", _base_problem_schema())

    for fault in registry.faults:
        _add_component(
            components,
            fault.effective_schema_name,
            compile_fault_schema(registry, fault),
        )

    if include_validation_error:
        _add_component(
            components,
            "RequestValidationProblem",
            _request_validation_schema(registry),
        )

    for path, methods, faults in _effective_http_contracts(app):
        path_item = cast("dict[str, Any] | None", result.get("paths", {}).get(path))
        if path_item is None:
            continue
        for method in methods:
            operation = cast("dict[str, Any] | None", path_item.get(method.lower()))
            if operation is None or method.lower() not in _HTTP_METHODS:
                continue
            responses = operation.setdefault("responses", {})
            _merge_fault_responses(responses, compile_responses(registry, faults))
            if include_validation_error:
                _replace_default_validation_response(responses)

    return result


def compile_responses(
    registry: FaultRegistry,
    faults: Sequence[AnyFault],
) -> dict[int | str, dict[str, Any]]:
    grouped: dict[int, list[AnyFault]] = {}
    for fault in faults:
        if not registry._contains(fault):
            msg = f"fault {fault.code!r} does not belong to this registry"
            raise FaultConfigurationError(msg)
        if registry._type_uri_for(fault) is None:
            msg = f"fault {fault.code!r} has no resolved problem type URI"
            raise FaultConfigurationError(msg)
        grouped.setdefault(fault.status, []).append(fault)

    return {status: _response_for_faults(group) for status, group in grouped.items()}


def compile_fault_schema(
    registry: FaultRegistry,
    fault: AnyFault,
) -> dict[str, Any]:
    type_uri = registry._type_uri_for(fault)
    if type_uri is None:
        msg = f"fault {fault.code!r} has no resolved problem type URI"
        raise FaultConfigurationError(msg)
    schema = _base_problem_schema()
    schema["title"] = fault.effective_schema_name
    properties = cast("dict[str, Any]", schema["properties"])
    properties.update(
        {
            "type": {"type": "string", "format": "uri-reference", "const": type_uri},
            "title": {"type": "string", "const": fault.title},
            "status": {"type": "integer", "const": fault.status},
            "code": {"type": "string", "const": fault.code},
        }
    )

    required = cast("list[str]", schema["required"])
    if fault.extensions_model is not None:
        extension_schema = fault.extensions_model.model_json_schema(
            mode="serialization", by_alias=True
        )
        extension_schema = _rewrite_local_definitions(
            extension_schema, fault.effective_schema_name
        )
        extension_properties = extension_schema.get("properties", {})
        properties.update(extension_properties)
        required.extend(extension_schema.get("required", []))
        if "$defs" in extension_schema:
            schema["$defs"] = extension_schema["$defs"]
    elif isinstance(fault.extensions, Mapping):
        for name, value in fault.extensions.items():
            properties[name] = _schema_for_static_value(value)
            required.append(name)

    if fault.example is not None:
        example = _thaw(fault.example)
        _validate_example(fault, type_uri, example)
        schema["examples"] = [example]
    return schema


def _base_problem_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "title": "Problem",
        "description": "RFC 9457 Problem Details with a stable application code.",
        "properties": {
            "type": {"type": "string", "format": "uri-reference"},
            "title": {"type": "string"},
            "status": {"type": "integer", "minimum": 100, "maximum": 599},
            "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,}$"},
            "detail": {"type": ["string", "null"]},
            "instance": {"type": ["string", "null"], "format": "uri-reference"},
        },
        "required": ["type", "title", "status", "code"],
        "additionalProperties": True,
    }


def _response_for_faults(faults: Sequence[AnyFault]) -> dict[str, Any]:
    references = [
        f"#/components/schemas/{fault.effective_schema_name}" for fault in faults
    ]
    if len(faults) == 1:
        schema: dict[str, Any] = {"$ref": references[0]}
        description = faults[0].description or faults[0].title
    else:
        schema = {
            "oneOf": [{"$ref": reference} for reference in references],
            "discriminator": {
                "propertyName": "code",
                "mapping": {
                    fault.code: reference
                    for fault, reference in zip(faults, references, strict=True)
                },
            },
        }
        description = "Possible problems: " + ", ".join(fault.title for fault in faults)

    response: dict[str, Any] = {
        "description": description,
        "content": {_PROBLEM_MEDIA_TYPE: {"schema": schema}},
    }
    headers: dict[str, Any] = {}
    for fault in faults:
        for name, definition in (fault.openapi_headers or {}).items():
            existing = next(
                (known for known in headers if known.lower() == name.lower()), None
            )
            candidate = _thaw(definition)
            if existing is not None and headers[existing] != candidate:
                msg = f"conflicting OpenAPI header {name!r} for status {fault.status}"
                raise FaultConfigurationError(msg)
            headers[name] = candidate
    if headers:
        response["headers"] = headers
    return response


def _merge_fault_responses(
    target: dict[str, Any], generated: Mapping[int | str, dict[str, Any]]
) -> None:
    for status, response in generated.items():
        key = str(status)
        existing = target.get(key)
        if existing is None:
            target[key] = response
            continue
        content = existing.setdefault("content", {})
        if _PROBLEM_MEDIA_TYPE in content:
            msg = f"manual response {key} already defines {_PROBLEM_MEDIA_TYPE!r}"
            raise FaultConfigurationError(msg)
        content[_PROBLEM_MEDIA_TYPE] = response["content"][_PROBLEM_MEDIA_TYPE]
        _merge_response_members(existing, response, key)


def _merge_response_members(
    existing: dict[str, Any], generated: dict[str, Any], status: str
) -> None:
    generated_headers = cast("dict[str, Any]", generated.get("headers", {}))
    existing_headers = existing.setdefault("headers", {}) if generated_headers else {}
    for name, definition in generated_headers.items():
        collision = next(
            (known for known in existing_headers if known.lower() == name.lower()), None
        )
        if collision is not None and existing_headers[collision] != definition:
            msg = f"manual response {status} conflicts on header {name!r}"
            raise FaultConfigurationError(msg)
        existing_headers[name] = definition


def _replace_default_validation_response(responses: dict[str, Any]) -> None:
    response = responses.get("422")
    if not isinstance(response, dict):
        return
    content = response.get("content")
    if not isinstance(content, dict):
        return
    application_json = content.get("application/json")
    if not isinstance(application_json, dict):
        return
    schema = application_json.get("schema")
    if schema != {"$ref": "#/components/schemas/HTTPValidationError"}:
        return
    content.pop("application/json")
    content[_PROBLEM_MEDIA_TYPE] = {
        "schema": {"$ref": "#/components/schemas/RequestValidationProblem"}
    }
    response["description"] = "Request validation failed"


def _request_validation_schema(registry: FaultRegistry) -> dict[str, Any]:
    if registry.type_base is None:
        msg = "type_base is required for the request validation schema"
        raise FaultConfigurationError(msg)
    schema = _base_problem_schema()
    properties = cast("dict[str, Any]", schema["properties"])
    properties.update(
        {
            "type": {
                "type": "string",
                "format": "uri-reference",
                "const": f"{registry.type_base}/request_validation_error",
            },
            "title": {"type": "string", "const": "Request validation failed"},
            "status": {"type": "integer", "const": 422},
            "code": {"type": "string", "const": "request_validation_error"},
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "detail": {"type": "string"},
                        "pointer": {"type": "string"},
                        "parameter": {"type": "string"},
                        "in": {
                            "type": "string",
                            "enum": ["path", "query", "header", "cookie"],
                        },
                    },
                    "required": ["code", "detail"],
                    "additionalProperties": False,
                },
            },
        }
    )
    cast("list[str]", schema["required"]).append("errors")
    schema["title"] = "RequestValidationProblem"
    return schema


def _effective_http_contracts(
    app: FastAPI,
) -> list[tuple[str, set[str], tuple[AnyFault, ...]]]:
    contracts = list(_iter_http_contracts(app.router))
    contexts = _effective_api_routes(app)
    if len(contracts) != len(contexts):
        msg = "FastAPI route traversal changed; cannot compile fault contracts safely"
        raise FaultConfigurationError(msg)
    result: list[tuple[str, set[str], tuple[AnyFault, ...]]] = []
    for (route, faults), context in zip(contracts, contexts, strict=True):
        original = getattr(context, "original_route", context)
        if original is not route:
            msg = "FastAPI route order changed; cannot compile fault contracts safely"
            raise FaultConfigurationError(msg)
        path = cast("str", getattr(context, "path", route.path))
        methods = set(
            cast("set[str] | None", getattr(context, "methods", route.methods)) or ()
        )
        result.append((path, methods, faults))
    return result


def _effective_api_routes(app: FastAPI) -> list[object]:
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return [route for route in app.routes if isinstance(route, APIRoute)]
    return [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(getattr(context, "original_route", None), APIRoute)
    ]


def _add_component(
    components: dict[str, Any], name: str, schema: dict[str, Any]
) -> None:
    existing = components.get(name)
    if existing is not None and existing != schema:
        msg = f"OpenAPI component {name!r} already exists with another schema"
        raise FaultConfigurationError(msg)
    components[name] = schema


def _rewrite_local_definitions(value: Any, component: str) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item.replace("#/$defs/", f"#/components/schemas/{component}/$defs/")
                if key == "$ref" and isinstance(item, str)
                else _rewrite_local_definitions(item, component)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_local_definitions(item, component) for item in value]
    return value


def _schema_for_static_value(value: JsonValue) -> dict[str, Any]:
    thawed = _thaw(value)
    return {"const": thawed, "type": _json_type(thawed)}


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _validate_example(fault: AnyFault, type_uri: str, example: dict[str, Any]) -> None:
    expected = {
        "type": type_uri,
        "title": fault.title,
        "status": fault.status,
        "code": fault.code,
    }
    for name, value in expected.items():
        if example.get(name) != value:
            msg = f"example for fault {fault.code!r} has invalid {name!r}"
            raise FaultConfigurationError(msg)
    if isinstance(fault.extensions, Mapping):
        for name, value in fault.extensions.items():
            if example.get(name) != _thaw(value):
                msg = (
                    f"example for fault {fault.code!r} has invalid extension "
                    f"member {name!r}"
                )
                raise FaultConfigurationError(msg)
    try:
        Problem.model_validate(example)
        if fault.extensions_model is not None:
            extension_values = {
                name: value
                for name, value in example.items()
                if name not in {"type", "title", "status", "code", "detail", "instance"}
            }
            fault.extensions_model.model_validate(extension_values)
    except ValidationError as error:
        msg = f"example for fault {fault.code!r} has invalid extension members"
        raise FaultConfigurationError(msg) from error


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
