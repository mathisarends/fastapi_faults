from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Self, cast
from urllib.parse import urlsplit

from ._types import FaultConfigurationError, is_absolute_uri
from .fault import Fault
from .websocket import WebSocketFault

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .router import FaultRouter

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
    _entries: tuple[_RegistryEntry, ...]
    _websocket_entries: tuple[_WebSocketRegistryEntry, ...]
    _by_exception: MappingProxyType[type[Exception], _RegistryEntry]
    _by_identity: MappingProxyType[int, _RegistryEntry]
    _websocket_by_exception: MappingProxyType[type[Exception], _WebSocketRegistryEntry]
    _websocket_by_identity: MappingProxyType[int, _WebSocketRegistryEntry]

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
        entries: list[_RegistryEntry] = []
        websocket_entries: list[_WebSocketRegistryEntry] = []
        seen_identities: set[int] = set()

        for index, candidate in enumerate(cast("Sequence[object]", faults)):
            fault = candidate
            if not isinstance(fault, Fault):
                msg = f"faults[{index}] must be a Fault instance"
                raise FaultConfigurationError(msg)
            identity = id(fault)
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            entries.append(
                _RegistryEntry(
                    fault=fault,
                    type_uri=_resolve_type_uri(fault, normalized_base),
                    source=source,
                )
            )

        seen_websocket_identities: set[int] = set()
        for index, candidate in enumerate(cast("Sequence[object]", websocket_faults)):
            websocket_fault = candidate
            if not isinstance(websocket_fault, WebSocketFault):
                msg = f"websocket_faults[{index}] must be a WebSocketFault instance"
                raise FaultConfigurationError(msg)
            identity = id(websocket_fault)
            if identity in seen_websocket_identities:
                continue
            seen_websocket_identities.add(identity)
            websocket_entries.append(
                _WebSocketRegistryEntry(fault=websocket_fault, source=source)
            )

        self._initialize(
            entries=tuple(entries),
            websocket_entries=tuple(websocket_entries),
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
        merged: list[_RegistryEntry] = []
        merged_websocket: list[_WebSocketRegistryEntry] = []
        identities: dict[int, int] = {}
        websocket_identities: set[int] = set()

        for index, candidate in enumerate(cast("tuple[object, ...]", registries)):
            registry = candidate
            if not isinstance(registry, FaultRegistry):
                msg = f"registries[{index}] must be a FaultRegistry instance"
                raise FaultConfigurationError(msg)
            for entry in registry._entries:
                identity = id(entry.fault)
                previous_index = identities.get(identity)
                if previous_index is None:
                    identities[identity] = len(merged)
                    merged.append(entry)
                    continue

                previous = merged[previous_index]
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
                    merged[previous_index] = _RegistryEntry(
                        fault=previous.fault,
                        type_uri=entry.type_uri,
                        source=previous.source,
                    )
            for websocket_entry in registry._websocket_entries:
                identity = id(websocket_entry.fault)
                if identity in websocket_identities:
                    continue
                websocket_identities.add(identity)
                merged_websocket.append(websocket_entry)

        resolved = tuple(
            entry
            if entry.type_uri is not None or normalized_base is None
            else _RegistryEntry(
                fault=entry.fault,
                type_uri=_resolve_type_uri(entry.fault, normalized_base),
                source=entry.source,
            )
            for entry in merged
        )
        instance = object.__new__(cls)
        instance._initialize(
            entries=resolved,
            websocket_entries=tuple(merged_websocket),
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

    def _type_uri_for(self, fault: AnyFault) -> str | None:
        entry = self._by_identity.get(id(fault))
        if entry is None or entry.fault is not fault:
            msg = f"fault {fault.code!r} does not belong to this registry"
            raise FaultConfigurationError(msg)
        return entry.type_uri

    def _contains(self, fault: AnyFault) -> bool:
        entry = self._by_identity.get(id(fault))
        return entry is not None and entry.fault is fault

    def _require_resolved(self) -> None:
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

    def _contains_websocket(self, fault: AnyWebSocketFault) -> bool:
        entry = self._websocket_by_identity.get(id(fault))
        return entry is not None and entry.fault is fault

    def router(self, **kwargs: Any) -> "FaultRouter":
        """Create a router bound to this feature registry."""
        from .router import FaultRouter

        return FaultRouter(registry=self, **kwargs)

    def responses(self, *faults: AnyFault) -> dict[int | str, dict[str, Any]]:
        """Compile fault responses for a stock FastAPI APIRouter."""
        from .openapi import compile_responses

        return compile_responses(self, faults)

    def install(
        self,
        app: "FastAPI",
        *,
        include_validation_error: bool = True,
        include_http_exceptions: bool = True,
        include_unhandled_error: bool = True,
    ) -> None:
        """Install runtime handlers for HTTP and WebSocket fault mappings."""
        from .handlers import install_handlers

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
        by_identity = {id(entry.fault): entry for entry in entries}
        websocket_by_identity = {id(entry.fault): entry for entry in websocket_entries}
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "type_base", type_base)
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_websocket_entries", websocket_entries)
        object.__setattr__(self, "_by_exception", MappingProxyType(by_exception))
        object.__setattr__(self, "_by_identity", MappingProxyType(by_identity))
        object.__setattr__(
            self,
            "_websocket_by_exception",
            MappingProxyType(websocket_by_exception),
        )
        object.__setattr__(
            self,
            "_websocket_by_identity",
            MappingProxyType(websocket_by_identity),
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
