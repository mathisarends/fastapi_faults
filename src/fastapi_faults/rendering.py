from collections.abc import Mapping
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from ._types import FaultConfigurationError, JsonValue
from .fault import Fault, _freeze_headers
from .problem import Problem

type AnyFault = Fault[Any]

_RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance", "code"})


def render_problem(
    fault: AnyFault, exception: Exception, *, type_uri: str
) -> tuple[Problem, dict[str, str]]:
    """Render and validate one registered fault occurrence atomically."""
    if not isinstance(exception, fault.exception):
        msg = (
            f"cannot render {type(exception).__qualname__} with fault for "
            f"{fault.exception.__qualname__}"
        )
        raise FaultConfigurationError(msg)

    detail = _render_detail(fault, exception)
    extensions = _render_extensions(fault, exception)
    overlap = _RESERVED_MEMBERS.intersection(extensions)
    if overlap:
        names = ", ".join(sorted(overlap))
        msg = f"rendered extensions must not redefine reserved members: {names}"
        raise FaultConfigurationError(msg)

    payload: dict[str, object] = {
        "type": type_uri,
        "title": fault.title,
        "status": fault.status,
        "code": fault.code,
        "detail": detail,
        **extensions,
    }
    try:
        problem = Problem.model_validate(payload)
    except ValidationError as error:
        msg = f"fault {fault.code!r} rendered an invalid Problem Details payload"
        raise FaultConfigurationError(msg) from error

    return problem, _render_headers(fault, exception)


def _render_detail(fault: AnyFault, exception: Exception) -> str | None:
    renderer: object = fault.detail
    rendered: object = renderer(exception) if callable(renderer) else renderer
    if rendered is not None and not isinstance(rendered, str):
        msg = "detail callback must return a string or None"
        raise FaultConfigurationError(msg)
    return rendered


def _render_extensions(fault: AnyFault, exception: Exception) -> dict[str, JsonValue]:
    renderer: object = fault.extensions
    rendered: object = renderer(exception) if callable(renderer) else renderer
    if rendered is None:
        return {}
    if isinstance(rendered, BaseModel):
        rendered = rendered.model_dump(mode="python", by_alias=False)
    if not isinstance(rendered, Mapping):
        msg = "extensions callback must return a mapping or BaseModel"
        raise FaultConfigurationError(msg)

    model = fault.extensions_model
    if model is not None:
        try:
            rendered_model = model.model_validate(dict(rendered))
        except ValidationError as error:
            msg = "rendered extensions do not validate against extensions_model"
            raise FaultConfigurationError(msg) from error
        rendered = rendered_model.model_dump(mode="json", by_alias=True)

    return cast("dict[str, JsonValue]", dict(rendered))


def _render_headers(fault: AnyFault, exception: Exception) -> dict[str, str]:
    renderer: object = fault.headers
    rendered: object = renderer(exception) if callable(renderer) else renderer
    if rendered is None:
        return {}
    if not isinstance(rendered, Mapping):
        msg = "headers callback must return a mapping"
        raise FaultConfigurationError(msg)
    return dict(_freeze_headers(rendered, path="rendered headers"))
