from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from fastapi_faults import (
    Fault,
    FaultConfigurationError,
    FaultRegistry,
)


class DomainError(Exception):
    pass


class SessionNotFound(DomainError):
    pass


class ExpiredSession(SessionNotFound):
    pass


class AccountNotFound(DomainError):
    pass


class UnknownError(Exception):
    pass


def make_fault(
    exception: type[Exception],
    code: str,
    *,
    type_uri: str | None = None,
    schema_name: str | None = None,
) -> Fault[Exception]:
    factory = cast("Callable[..., Fault[Exception]]", Fault)
    return factory(
        exception,
        status=404,
        code=code,
        title=code.replace("_", " ").title(),
        type=type_uri,
        schema_name=schema_name,
    )


def test_registry_preserves_order_and_resolves_type_base() -> None:
    session = make_fault(SessionNotFound, "session_not_found")
    account = make_fault(AccountNotFound, "account_not_found")

    registry = FaultRegistry(
        name="api",
        faults=[session, account],
        type_base="https://api.example.com/problems/",
    )

    assert registry.name == "api"
    assert registry.type_base == "https://api.example.com/problems"
    assert registry.faults == (session, account)
    assert tuple(registry) == (session, account)
    assert len(registry) == 2
    assert (
        registry.type_uri_for(session)
        == "https://api.example.com/problems/session_not_found"
    )


def test_registry_deduplicates_same_fault_identity() -> None:
    fault = make_fault(SessionNotFound, "session_not_found")
    registry = FaultRegistry(faults=[fault, fault])

    assert registry.faults == (fault,)


@pytest.mark.parametrize("name", ["", "two\nlines", 42])
def test_registry_rejects_invalid_name(name: object) -> None:
    with pytest.raises(FaultConfigurationError):
        FaultRegistry(faults=[], name=name)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "type_base",
    ["/relative", "https://api.example.com/problems?version=1", 42],
)
def test_registry_rejects_invalid_type_base(type_base: object) -> None:
    with pytest.raises(FaultConfigurationError):
        FaultRegistry(faults=[], type_base=type_base)  # type: ignore[arg-type]


def test_registry_rejects_non_fault_members() -> None:
    with pytest.raises(FaultConfigurationError):
        FaultRegistry(faults=[object()])  # type: ignore[list-item]


def test_registry_retains_explicit_type() -> None:
    fault = make_fault(
        SessionNotFound,
        "session_not_found",
        type_uri="urn:example:session-not-found",
    )
    registry = FaultRegistry(
        faults=[fault], type_base="https://api.example.com/problems"
    )

    assert registry.type_uri_for(fault) == "urn:example:session-not-found"


def test_registry_requires_all_types_before_installation() -> None:
    fault = make_fault(SessionNotFound, "session_not_found")
    registry = FaultRegistry(faults=[fault])

    with pytest.raises(FaultConfigurationError, match="session_not_found"):
        registry.require_resolved()


def test_registry_accepts_empty_resolved_collection() -> None:
    registry = FaultRegistry(faults=[])

    registry.require_resolved()


def test_merge_retains_feature_type_base_and_fills_unresolved_faults() -> None:
    session = make_fault(SessionNotFound, "session_not_found")
    account = make_fault(AccountNotFound, "account_not_found")
    sessions = FaultRegistry(
        name="sessions",
        faults=[session],
        type_base="https://sessions.example.com/problems",
    )
    accounts = FaultRegistry(name="accounts", faults=[account])

    merged = FaultRegistry.merge(
        sessions,
        accounts,
        type_base="https://api.example.com/problems",
    )

    assert (
        merged.type_uri_for(session)
        == "https://sessions.example.com/problems/session_not_found"
    )
    assert (
        merged.type_uri_for(account)
        == "https://api.example.com/problems/account_not_found"
    )


def test_merge_prefers_existing_resolution_for_shared_fault() -> None:
    shared = make_fault(SessionNotFound, "session_not_found")
    unresolved = FaultRegistry(name="unresolved", faults=[shared])
    resolved = FaultRegistry(
        name="resolved",
        faults=[shared],
        type_base="https://feature.example.com/problems",
    )

    merged = FaultRegistry.merge(
        unresolved,
        resolved,
        type_base="https://api.example.com/problems",
    )

    assert (
        merged.type_uri_for(shared)
        == "https://feature.example.com/problems/session_not_found"
    )


def test_merge_rejects_shared_fault_with_incompatible_feature_bases() -> None:
    shared = make_fault(SessionNotFound, "session_not_found")
    first = FaultRegistry(
        name="first", faults=[shared], type_base="https://one.example/problems"
    )
    second = FaultRegistry(
        name="second", faults=[shared], type_base="https://two.example/problems"
    )

    with pytest.raises(FaultConfigurationError, match="incompatible type URIs"):
        FaultRegistry.merge(first, second)


def test_merge_rejects_non_registry_argument() -> None:
    with pytest.raises(FaultConfigurationError):
        FaultRegistry.merge(object())  # type: ignore[arg-type]


def test_merge_reports_original_feature_owners_on_collision() -> None:
    sessions = FaultRegistry(
        name="sessions",
        faults=[make_fault(SessionNotFound, "resource_not_found")],
    )
    accounts = FaultRegistry(
        name="accounts",
        faults=[make_fault(AccountNotFound, "resource_not_found")],
    )

    with pytest.raises(FaultConfigurationError) as error:
        FaultRegistry.merge(sessions, accounts, name="api")

    assert "sessions" in str(error.value)
    assert "accounts" in str(error.value)


def test_resolution_uses_most_specific_registered_mro_class() -> None:
    domain = make_fault(DomainError, "domain_error")
    missing = make_fault(SessionNotFound, "session_not_found")
    registry = FaultRegistry(
        faults=[domain, missing], type_base="https://api.example.com/problems"
    )

    assert registry.resolve(ExpiredSession()) is missing
    assert registry.resolve(AccountNotFound()) is domain
    assert registry.resolve(UnknownError()) is None


def test_registry_rejects_lookup_for_foreign_fault() -> None:
    registry = FaultRegistry(faults=[])
    foreign = make_fault(SessionNotFound, "session_not_found")

    with pytest.raises(FaultConfigurationError):
        registry.type_uri_for(foreign)


def test_registry_is_frozen() -> None:
    registry = FaultRegistry(faults=[])

    with pytest.raises(FrozenInstanceError):
        registry.name = "changed"  # type: ignore[misc]
