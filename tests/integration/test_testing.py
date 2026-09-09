import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from fastapi_faults import Fault, FaultConfigurationError, FaultRegistry
from tests.helpers import (
    assert_no_undeclared_faults,
    assert_openapi_contract,
    assert_problem,
)


class Missing(Exception):
    pass


def make_app(*, declared: bool) -> tuple[FastAPI, Fault[Missing]]:
    missing = Fault(
        Missing,
        status=404,
        code="resource_missing",
        title="Resource missing",
    )
    registry = FaultRegistry(
        faults=[missing], type_base="https://example.test/problems"
    )
    router = APIRouter()

    @router.get(
        "/resources/{resource_id}",
        responses=registry.responses(missing) if declared else {},
    )
    async def endpoint(resource_id: int) -> None:
        del resource_id
        raise Missing

    app = FastAPI()
    app.include_router(router)
    registry.install(app)
    return app, missing


def test_assert_problem_returns_validated_payload() -> None:
    app, missing = make_app(declared=True)
    response = TestClient(app).get("/resources/7")

    payload = assert_problem(
        response,
        missing,
        type_uri="https://example.test/problems/resource_missing",
    )

    assert payload["code"] == "resource_missing"


@pytest.mark.parametrize(
    ("type_uri", "mutation", "message"),
    [
        (
            "https://example.test/problems/resource_missing",
            lambda response: setattr(response, "status_code", 409),
            "expected status",
        ),
        (
            "https://wrong.test/problem",
            lambda _response: None,
            "constants differ",
        ),
    ],
)
def test_assert_problem_explains_contract_mismatch(
    type_uri: str, mutation: object, message: str
) -> None:
    app, missing = make_app(declared=True)
    response = TestClient(app).get("/resources/7")
    assert callable(mutation)
    mutation(response)

    with pytest.raises(AssertionError, match=message):
        assert_problem(response, missing, type_uri=type_uri)


def test_assert_problem_rejects_wrong_media_type() -> None:
    app, missing = make_app(declared=True)
    response = TestClient(app).get("/resources/7")
    response.headers["content-type"] = "application/json"

    with pytest.raises(AssertionError, match="content type"):
        assert_problem(
            response,
            missing,
            type_uri="https://example.test/problems/resource_missing",
        )


def test_assert_openapi_contract_accepts_declared_faults() -> None:
    app, _ = make_app(declared=True)

    assert_openapi_contract(app)


@pytest.mark.anyio
async def test_undeclared_fault_monitor_fails_after_observation() -> None:
    app, _ = make_app(declared=False)

    with pytest.raises(AssertionError, match="endpoint_resources__resource_id__get"):
        async with assert_no_undeclared_faults(app) as findings:
            response = TestClient(app).get("/resources/7")
            assert response.status_code == 404
            assert len(findings) == 1


@pytest.mark.anyio
async def test_undeclared_fault_monitor_accepts_declared_fault() -> None:
    app, _ = make_app(declared=True)

    async with assert_no_undeclared_faults(app) as findings:
        response = TestClient(app).get("/resources/7")

    assert response.status_code == 404
    assert findings == []


def test_testing_helpers_require_installed_registry() -> None:
    with pytest.raises(FaultConfigurationError, match="install"):
        assert_openapi_contract(FastAPI())


@pytest.mark.anyio
async def test_undeclared_monitor_rejects_already_started_application() -> None:
    app, _ = make_app(declared=True)
    TestClient(app).get("/resources/7")

    with pytest.raises(FaultConfigurationError, match="before the first request"):
        async with assert_no_undeclared_faults(app):
            pass
