from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Self, cast
from urllib.parse import urlsplit

from ._types import FaultConfigurationError, is_absolute_uri
from .fault import Fault

type AnyFault = Fault[Any]


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    fault: AnyFault
    type_uri: str | None
    source: str


@dataclass(frozen=True, slots=True, init=False)
class FaultRegistry:
    """An immutable, explicitly composable collection of fault definitions."""

    name: str | None
    type_base: str | None
    _entries: tuple[_RegistryEntry, ...]
    _by_exception: MappingProxyType[type[Exception], _RegistryEntry]
    _by_identity: MappingProxyType[int, _RegistryEntry]

    def __init__(
        self,
        *,
        faults: Sequence[AnyFault],
        name: str | None = None,
        type_base: str | None = None,
    ) -> None:
        normalized_name = _validate_name(name)
        normalized_base = _validate_type_base(type_base)
        source = normalized_name or "<anonymous>"
        entries: list[_RegistryEntry] = []
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

        self._initialize(
            entries=tuple(entries), name=normalized_name, type_base=normalized_base
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
        identities: dict[int, int] = {}

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
            entries=resolved, name=normalized_name, type_base=normalized_base
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

    def resolve(self, exception: Exception) -> AnyFault | None:
        """Resolve the most specific registered fault through normal Python MRO."""
        for exception_class in type(exception).__mro__:
            entry = self._by_exception.get(exception_class)
            if entry is not None:
                return entry.fault
        return None

    def _type_uri_for(self, fault: AnyFault) -> str | None:
        entry = self._by_identity.get(id(fault))
        if entry is None or entry.fault is not fault:
            msg = f"fault {fault.code!r} does not belong to this registry"
            raise FaultConfigurationError(msg)
        return entry.type_uri

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

    def _initialize(
        self,
        *,
        entries: tuple[_RegistryEntry, ...],
        name: str | None,
        type_base: str | None,
    ) -> None:
        by_exception = _validate_collisions(entries)
        by_identity = {id(entry.fault): entry for entry in entries}
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "type_base", type_base)
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_by_exception", MappingProxyType(by_exception))
        object.__setattr__(self, "_by_identity", MappingProxyType(by_identity))


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
