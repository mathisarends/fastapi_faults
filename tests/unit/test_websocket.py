from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from fastapi_faults import FaultConfigurationError, WebSocketFault


class SessionExpired(Exception):
    pass


def make_fault(**overrides: object) -> WebSocketFault[SessionExpired]:
    values: dict[str, object] = {"close_code": 4001, "reason": "Session expired"}
    values.update(overrides)
    factory = cast("Callable[..., WebSocketFault[SessionExpired]]", WebSocketFault)
    return factory(SessionExpired, **values)


def test_websocket_fault_renders_static_and_dynamic_reasons() -> None:
    static = make_fault()
    dynamic = make_fault(reason=lambda _exception: "Expired dynamically")
    empty = make_fault(reason=None)

    assert static.render_reason(SessionExpired()) == "Session expired"
    assert dynamic.render_reason(SessionExpired()) == "Expired dynamically"
    assert empty.render_reason(SessionExpired()) == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close_code", 3999),
        ("close_code", 5000),
        ("close_code", True),
        ("reason", object()),
        ("reason", "x" * 124),
        ("description", 42),
    ],
)
def test_websocket_fault_rejects_invalid_configuration(
    field: str, value: object
) -> None:
    with pytest.raises(FaultConfigurationError):
        make_fault(**{field: value})


def test_websocket_fault_rejects_non_exception_class() -> None:
    factory = cast("Callable[..., WebSocketFault[Exception]]", WebSocketFault)

    with pytest.raises(FaultConfigurationError):
        factory(str, close_code=4001)


def test_websocket_fault_measures_utf8_bytes_not_characters() -> None:
    assert len("🙂" * 30) < 123

    with pytest.raises(FaultConfigurationError):
        make_fault(reason="🙂" * 31)


def test_websocket_fault_validates_callback_result() -> None:
    invalid = make_fault(reason=cast("Callable[[SessionExpired], str]", lambda _: 42))
    too_long = make_fault(reason=lambda _: "🙂" * 31)

    with pytest.raises(FaultConfigurationError):
        invalid.render_reason(SessionExpired())
    with pytest.raises(FaultConfigurationError):
        too_long.render_reason(SessionExpired())


def test_websocket_fault_is_frozen() -> None:
    fault = make_fault()

    with pytest.raises(FrozenInstanceError):
        fault.close_code = 4002  # type: ignore[misc]
