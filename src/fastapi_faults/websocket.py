import builtins
from dataclasses import dataclass, field
from typing import Protocol

from .types import FaultConfigurationError


class ReasonRenderer[ExceptionT: Exception](Protocol):
    """Render the advisory reason of a WebSocket close frame."""

    def __call__(self, exception: ExceptionT, /) -> str | None: ...


type Reason[ExceptionT: Exception] = str | ReasonRenderer[ExceptionT] | None


@dataclass(frozen=True, slots=True, eq=False)
class WebSocketFault[ExceptionT: Exception]:
    """An immutable mapping from a domain exception to a WebSocket close."""

    exception: builtins.type[ExceptionT]
    close_code: int = field(kw_only=True)
    reason: Reason[ExceptionT] = field(default=None, kw_only=True)
    description: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        exception: object = self.exception
        close_code: object = self.close_code
        reason: object = self.reason
        description: object = self.description

        if not isinstance(exception, builtins.type) or not issubclass(
            exception, Exception
        ):
            msg = "exception must be an Exception subclass"
            raise FaultConfigurationError(msg)
        if (
            not isinstance(close_code, int)
            or isinstance(close_code, bool)
            or not 4000 <= close_code <= 4999
        ):
            msg = "close_code must be an application code between 4000 and 4999"
            raise FaultConfigurationError(msg)
        if reason is not None and not isinstance(reason, str) and not callable(reason):
            msg = "reason must be a string, callable, or None"
            raise FaultConfigurationError(msg)
        if isinstance(reason, str):
            _validate_reason(reason)
        if description is not None and not isinstance(description, str):
            msg = "description must be a string or None"
            raise FaultConfigurationError(msg)

    def _render_reason(self, exception: ExceptionT) -> str:
        renderer: object = self.reason
        rendered: object = renderer(exception) if callable(renderer) else renderer
        if rendered is None:
            return ""
        if not isinstance(rendered, str):
            msg = "reason callback must return a string or None"
            raise FaultConfigurationError(msg)
        _validate_reason(rendered)
        return rendered


def _validate_reason(reason: str) -> None:
    if len(reason.encode("utf-8")) > 123:
        msg = "WebSocket close reason must not exceed 123 UTF-8 bytes"
        raise FaultConfigurationError(msg)
