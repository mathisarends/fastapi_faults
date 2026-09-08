from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

from fastapi_faults.fault import Fault
from fastapi_faults.types import FaultConfigurationError, is_absolute_uri
from fastapi_faults.validation import unique_instances
from fastapi_faults.websocket import WebSocketFault

if TYPE_CHECKING:
    from fastapi import FastAPI

    from fastapi_faults.router import FaultRouter

type AnyFault = Fault[Any]
type AnyWebSocketFault = WebSocketFault[Any]


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    fault: AnyFault
    type_uri: str | None
    source: str


@dataclass(frozen=True, slots=True)
class _WebSocketRegistryEntry:
    fault: AnyWebSocketFault
    source: str


@dataclass(frozen=True, slots=True, init=False)
class FaultRegistry:
    """An immutable, explicitly composable collection of fault definitions."""

    name: str | None
    type_base: str | None
    _entries: tuple[_RegistryEntry, ...] = field(repr=False)
    _websocket_entries: tuple[_WebSocketRegistryEntry, ...] = field(repr=False)
    _by_exception: Mapping[type[Exception], _RegistryEntry] = field(
        repr=False, compare=False, hash=False
    )
    _websocket_by_exception: Mapping[type[Exception], _WebSocketRegistryEntry] = field(
        repr=False, compare=False, hash=False
    )

    def __init__(
        self,
        *,
        faults: Sequence[AnyFault],
        websocket_faults: Sequence[AnyWebSocketFault] = (),
        name: str | None = None,
        type_base: str | None = None,
    ) -> None:
        normalized_name = _validate_name(name)
        normalized_base = _validate_type_base(type_base)
        source = normalized_name or "<anonymous>"
        entries = tuple(
            _RegistryEntry(
                fault=fault,
                type_uri=_resolve_type_uri(fault, normalized_base),
                source=source,
            )
            for fault in unique_instances(faults, Fault, parameter="faults")
        )
        websocket_entries = tuple(
            _WebSocketRegistryEntry(fault=fault, source=source)
            for fault in unique_instances(
                websocket_faults, WebSocketFault, parameter="websocket_faults"
            )
        )

        self._initialize(
            entries=entries,
            websocket_entries=websocket_entries,
            name=normalized_name,
            type_base=normalized_base,
        )

    @classmethod
    def merge(
        cls,
        *registries: Self,
        name: str | None = None,
        type_base: str | None = None,
    ) -> Self:
        """Compose registries without mutating inputs or unpacking fault lists."""
        normalized_name = _validate_name(name)
        normalized_base = _validate_type_base(type_base)
        merged: dict[int, _RegistryEntry] = {}
        merged_websocket: dict[int, _WebSocketRegistryEntry] = {}

        for registry in unique_instances(
            registries, FaultRegistry, parameter="registries"
        ):
            for entry in registry._entries:
                identity = id(entry.fault)
                previous = merged.get(identity)
                if previous is None:
                    merged[identity] = entry
                    continue
                if (
                    previous.type_uri is not None
                    and entry.type_uri is not None
                    and previous.type_uri != entry.type_uri
                ):
                    msg = (
                        f"fault {entry.fault.code!r} is shared by registries "
                        f"{previous.source!r} and {entry.source!r} with incompatible "
                        f"type URIs {previous.type_uri!r} and {entry.type_uri!r}"
                    )
                    raise FaultConfigurationError(msg)
                if previous.type_uri is None and entry.type_uri is not None:
                    merged[identity] = replace(previous, type_uri=entry.type_uri)
            for websocket_entry in registry._websocket_entries:
                merged_websocket.setdefault(id(websocket_entry.fault), websocket_entry)

        resolved = tuple(
            entry
            if entry.type_uri is not None or normalized_base is None
            else replace(
                entry,
                type_uri=_resolve_type_uri(entry.fault, normalized_base),
            )
            for entry in merged.values()
        )
        instance = object.__new__(cls)
        instance._initialize(
            entries=resolved,
            websocket_entries=tuple(merged_websocket.values()),
            name=normalized_name,
            type_base=normalized_base,
        )
        return instance

    @property
    def faults(self) -> tuple[AnyFault, ...]:
        """Return fault definitions in deterministic declaration order."""
        return tuple(entry.fault for entry in self._entries)

    def __iter__(self) -> Iterator[AnyFault]:
        return iter(self.faults)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def websocket_faults(self) -> tuple[AnyWebSocketFault, ...]:
        """Return WebSocket close definitions in deterministic order."""
        return tuple(entry.fault for entry in self._websocket_entries)

    def resolve(self, exception: Exception) -> AnyFault | None:
        """Resolve the most specific registered fault through normal Python MRO."""
        for exception_class in type(exception).__mro__:
            entry = self._by_exception.get(exception_class)
            if entry is not None:
                return entry.fault
        return None

    def resolve_websocket(self, exception: Exception) -> AnyWebSocketFault | None:
        """Resolve the most specific registered WebSocket fault through MRO."""
        for exception_class in type(exception).__mro__:
            entry = self._websocket_by_exception.get(exception_class)
            if entry is not None:
                return entry.fault
        return None

    def type_uri_for(self, fault: AnyFault) -> str | None:
        """Return a member's resolved type URI, or None if still unresolved."""
        entry = self._by_exception.get(fault.exception)
        if entry is None or entry.fault is not fault:
            msg = f"fault {fault.code!r} does not belong to this registry"
            raise FaultConfigurationError(msg)
        return entry.type_uri

    def contains(self, fault: AnyFault) -> bool:
        """Check membership by definition identity, not exception class alone."""
        entry = self._by_exception.get(fault.exception)
        return entry is not None and entry.fault is fault

    def require_resolved(self) -> None:
        """Reject registries with unresolved HTTP problem type URIs."""
        unresolved = [
            entry.fault.code for entry in self._entries if entry.type_uri is None
        ]
        if unresolved:
            codes = ", ".join(repr(code) for code in unresolved)
            msg = (
                "registry cannot be installed with unresolved problem type URIs: "
                f"{codes}; provide explicit Fault.type values or a type_base"
            )
            raise FaultConfigurationError(msg)

    def contains_websocket(self, fault: AnyWebSocketFault) -> bool:
        """Check WebSocket membership by definition identity."""
        entry = self._websocket_by_exception.get(fault.exception)
        return entry is not None and entry.fault is fault

    def router(self, **kwargs: Any) -> FaultRouter:
        """Create a router bound to this feature registry."""
        from fastapi_faults.router import FaultRouter

        return FaultRouter(registry=self, **kwargs)

    def responses(self, *faults: AnyFault) -> dict[int | str, dict[str, Any]]:
        """Compile fault responses for a stock FastAPI APIRouter."""
        from fastapi_faults.openapi import compile_responses

        return compile_responses(self, faults)

    def install(
        self,
        app: FastAPI,
        *,
        include_validation_error: bool = True,
        include_http_exceptions: bool = True,
        include_unhandled_error: bool = True,
    ) -> None:
        """Install runtime handlers for HTTP and WebSocket fault mappings."""
        from fastapi_faults.handlers import install_handlers

        install_handlers(
            self,
            app,
            include_validation_error=include_validation_error,
            include_http_exceptions=include_http_exceptions,
            include_unhandled_error=include_unhandled_error,
        )

    def _initialize(
        self,
        *,
        entries: tuple[_RegistryEntry, ...],
        websocket_entries: tuple[_WebSocketRegistryEntry, ...],
        name: str | None,
        type_base: str | None,
    ) -> None:
        by_exception = _validate_collisions(entries)
        websocket_by_exception = _validate_websocket_collisions(websocket_entries)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "type_base", type_base)
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_websocket_entries", websocket_entries)
        object.__setattr__(self, "_by_exception", MappingProxyType(by_exception))
        object.__setattr__(
            self,
            "_websocket_by_exception",
            MappingProxyType(websocket_by_exception),
        )


def _validate_name(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        msg = "registry name must be a non-empty, single-line string or None"
        raise FaultConfigurationError(msg)
    return value


def _validate_type_base(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not is_absolute_uri(value):
        msg = "type_base must be an absolute URI without a fragment"
        raise FaultConfigurationError(msg)
    parsed = urlsplit(value)
    if parsed.query:
        msg = "type_base must not contain a query string"
        raise FaultConfigurationError(msg)
    return value.rstrip("/")


def _resolve_type_uri(fault: AnyFault, type_base: str | None) -> str | None:
    if fault.type is not None:
        return fault.type
    if type_base is None:
        return None
    return f"{type_base}/{fault.code}"


def _validate_collisions(
    entries: tuple[_RegistryEntry, ...],
) -> dict[type[Exception], _RegistryEntry]:
    dimensions: tuple[tuple[str, dict[object, _RegistryEntry]], ...] = (
        ("exception class", {}),
        ("code", {}),
        ("type URI", {}),
        ("schema name", {}),
    )
    by_exception: dict[type[Exception], _RegistryEntry] = {}

    for entry in entries:
        keys: tuple[object | None, ...] = (
            entry.fault.exception,
            entry.fault.code,
            entry.type_uri,
            entry.fault.effective_schema_name,
        )
        for (dimension, seen), key in zip(dimensions, keys, strict=True):
            if key is None:
                continue
            previous = seen.get(key)
            if previous is not None and previous.fault is not entry.fault:
                raise _collision_error(dimension, key, previous, entry)
            seen[key] = entry
        by_exception[entry.fault.exception] = entry

    return by_exception


def _collision_error(
    dimension: str,
    value: object,
    first: _RegistryEntry,
    second: _RegistryEntry,
) -> FaultConfigurationError:
    return FaultConfigurationError(
        f"conflicting {dimension} {value!r}: fault {first.fault.code!r} from "
        f"registry {first.source!r} conflicts with fault {second.fault.code!r} "
        f"from registry {second.source!r}"
    )


def _validate_websocket_collisions(
    entries: tuple[_WebSocketRegistryEntry, ...],
) -> dict[type[Exception], _WebSocketRegistryEntry]:
    by_exception: dict[type[Exception], _WebSocketRegistryEntry] = {}
    by_close_code: dict[int, _WebSocketRegistryEntry] = {}

    for entry in entries:
        previous_exception = by_exception.get(entry.fault.exception)
        if (
            previous_exception is not None
            and previous_exception.fault is not entry.fault
        ):
            raise _websocket_collision_error(
                "exception class",
                entry.fault.exception,
                previous_exception,
                entry,
            )
        previous_code = by_close_code.get(entry.fault.close_code)
        if previous_code is not None and previous_code.fault is not entry.fault:
            raise _websocket_collision_error(
                "close code", entry.fault.close_code, previous_code, entry
            )
        by_exception[entry.fault.exception] = entry
        by_close_code[entry.fault.close_code] = entry

    return by_exception


def _websocket_collision_error(
    dimension: str,
    value: object,
    first: _WebSocketRegistryEntry,
    second: _WebSocketRegistryEntry,
) -> FaultConfigurationError:
    return FaultConfigurationError(
        f"conflicting WebSocket {dimension} {value!r}: definition for "
        f"{first.fault.exception.__qualname__!r} from registry {first.source!r} "
        f"conflicts with {second.fault.exception.__qualname__!r} from registry "
        f"{second.source!r}"
    )
