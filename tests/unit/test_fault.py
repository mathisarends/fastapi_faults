from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from pydantic import BaseModel, ConfigDict, Field

from fastapi_faults import Fault, FaultConfigurationError


class SessionNotFound(Exception):
    pass


class SessionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: int


def make_fault(**overrides: object) -> Fault[SessionNotFound]:
    values: dict[str, object] = {
        "status": 404,
        "code": "session_not_found",
        "title": "Session not found",
        "type": "https://api.example.com/problems/session_not_found",
    }
    values.update(overrides)
    factory = cast("Callable[..., Fault[SessionNotFound]]", Fault)
    return factory(SessionNotFound, **values)


def test_fault_exposes_stable_identity_and_schema_name() -> None:
    fault = make_fault()

    assert fault.exception is SessionNotFound
    assert fault.effective_schema_name == "SessionNotFoundProblem"


def test_fault_uses_explicit_schema_name() -> None:
    fault = make_fault(schema_name="MissingSession")

    assert fault.effective_schema_name == "MissingSession"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", 399),
        ("status", 600),
        ("status", True),
        ("code", "NOPE"),
        ("title", ""),
        ("title", "two\nlines"),
        ("type", "/relative"),
        ("detail", 123),
        ("description", 123),
        ("schema_name", ""),
        ("example", "not-a-mapping"),
        ("openapi_headers", "not-a-mapping"),
        ("extensions", object()),
        ("headers", object()),
    ],
)
def test_fault_rejects_invalid_configuration(field: str, value: object) -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(**{field: value})


def test_fault_rejects_non_exception_class() -> None:
    factory = cast("Callable[..., Fault[Exception]]", Fault)

    with pytest.raises(FaultConfigurationError):
        factory(str, status=400, code="bad_exception", title="Bad exception")


def test_fault_requires_extensions_for_model() -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(extensions_model=SessionFields)


def test_fault_requires_model_for_callable_extensions() -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(extensions=lambda _exception: {"session_id": 1})


def test_fault_rejects_invalid_extensions_model() -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(extensions_model=str, extensions={"session_id": 1})


@pytest.mark.parametrize(
    "member", ["type", "title", "status", "detail", "instance", "code"]
)
def test_fault_rejects_reserved_extension_members(member: str) -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(extensions={member: "collision"})


def test_fault_rejects_reserved_model_alias() -> None:
    class InvalidFields(BaseModel):
        value: str = Field(serialization_alias="detail")

    with pytest.raises(FaultConfigurationError):
        make_fault(extensions_model=InvalidFields, extensions={"value": "unsafe"})


def test_fault_validates_and_serializes_static_typed_extensions() -> None:
    source = {"session_id": "42"}
    fault = make_fault(extensions_model=SessionFields, extensions=source)

    source["session_id"] = "changed"
    assert fault.extensions == {"session_id": 42}


def test_fault_rejects_static_extensions_that_do_not_match_model() -> None:
    with pytest.raises(FaultConfigurationError) as error:
        make_fault(extensions_model=SessionFields, extensions={"session_id": "nope"})

    assert isinstance(error.value.__cause__, Exception)


@pytest.mark.parametrize(
    "extensions",
    [
        {1: "invalid key"},
        {"value": object()},
        {"value": float("inf")},
    ],
)
def test_fault_rejects_non_json_static_extensions(extensions: object) -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(extensions=extensions)


def test_fault_deeply_freezes_static_json() -> None:
    source = {
        "items": [1, {"nested": True}],
        "metadata": {"ready": True},
        "ratio": 0.5,
    }
    fault = make_fault(extensions=source)

    source["items"] = []
    assert fault.extensions == {
        "items": (1, {"nested": True}),
        "metadata": {"ready": True},
        "ratio": 0.5,
    }
    with pytest.raises(TypeError):
        cast("dict[str, object]", fault.extensions)["new"] = "value"


@pytest.mark.parametrize(
    "headers",
    [
        {"bad header": "value"},
        {"Content-Type": "text/plain"},
        {"Retry-After": 10},
    ],
)
def test_fault_rejects_invalid_static_headers(headers: object) -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(headers=headers)


def test_fault_freezes_static_headers_and_openapi_headers() -> None:
    headers = {"Retry-After": "10"}
    openapi_headers = {
        "Retry-After": {"schema": {"type": "string"}, "description": "Retry delay"}
    }
    fault = make_fault(headers=headers, openapi_headers=openapi_headers)

    headers["Retry-After"] = "20"
    openapi_headers["Retry-After"] = {}
    assert fault.headers == {"Retry-After": "10"}
    assert fault.openapi_headers == {
        "Retry-After": {
            "schema": {"type": "string"},
            "description": "Retry delay",
        }
    }


def test_fault_rejects_non_mapping_openapi_header_definition() -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(openapi_headers={"Retry-After": "invalid"})


def test_fault_freezes_static_example() -> None:
    example = {"type": "urn:example:problem", "status": 404}
    fault = make_fault(example=example)

    example["status"] = 500
    assert fault.example == {"type": "urn:example:problem", "status": 404}


def test_fault_accepts_callable_detail_headers_and_typed_extensions() -> None:
    fault = make_fault(
        detail=lambda _exception: "Missing",
        extensions_model=SessionFields,
        extensions=lambda _exception: SessionFields(session_id=42),
        headers=lambda _exception: {"Retry-After": "10"},
    )

    assert callable(fault.detail)
    assert callable(fault.extensions)
    assert callable(fault.headers)


def test_fault_is_frozen() -> None:
    fault = make_fault()

    with pytest.raises(FrozenInstanceError):
        fault.title = "Changed"  # type: ignore[misc]
