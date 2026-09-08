"""Shared internal types and configuration validation helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type OpenAPIHeader = Mapping[str, JsonValue]

ExceptionT_contra = TypeVar("ExceptionT_contra", bound=Exception, contravariant=True)


class DetailRenderer(Protocol[ExceptionT_contra]):
    """Render occurrence-specific human-readable detail."""

    def __call__(self, exception: ExceptionT_contra, /) -> str | None: ...


class ExtensionsRenderer(Protocol[ExceptionT_contra]):
    """Render typed or JSON-native Problem Details extension members."""

    def __call__(
        self, exception: ExceptionT_contra, /
    ) -> Mapping[str, JsonValue] | BaseModel: ...


class HeadersRenderer(Protocol[ExceptionT_contra]):
    """Render response headers for a fault occurrence."""

    def __call__(self, exception: ExceptionT_contra, /) -> Mapping[str, str]: ...


type Detail[ExceptionT: Exception] = str | DetailRenderer[ExceptionT] | None
type Extensions[ExceptionT: Exception] = (
    Mapping[str, JsonValue] | ExtensionsRenderer[ExceptionT] | None
)
type Headers[ExceptionT: Exception] = (
    Mapping[str, str] | HeadersRenderer[ExceptionT] | None
)

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,}$")


class FaultConfigurationError(ValueError):
    """Raised when fault definitions cannot form a safe, coherent contract."""


def is_absolute_uri(value: str) -> bool:
    """Return whether *value* is a usable RFC 3986 absolute URI."""
    if not value or any(character.isspace() for character in value):
        return False

    try:
        parsed = urlsplit(value)
    except ValueError:
        return False

    if not parsed.scheme or parsed.fragment:
        return False
    return not (parsed.scheme in {"http", "https"} and not parsed.netloc)


def is_uri_reference(value: str) -> bool:
    """Return whether *value* is a non-empty RFC 3986 URI reference."""
    if not value or any(character.isspace() for character in value):
        return False
    try:
        urlsplit(value)
    except ValueError:
        return False
    return True
