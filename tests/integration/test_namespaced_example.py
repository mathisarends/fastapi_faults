from fastapi.testclient import TestClient

from examples.namespaced.api import faults
from examples.namespaced.app import app
from examples.namespaced.sessions import EXPIRED_SESSION_ID


def test_namespaced_example_composes_features_and_contracts() -> None:
    client = TestClient(app)

    missing_user = client.get("/api/v1/users/00000000-0000-0000-0000-000000000000")
    expired_session = client.get(f"/api/v1/sessions/{EXPIRED_SESSION_ID}")
    document = app.openapi()

    assert missing_user.status_code == 404
    assert missing_user.headers["content-type"] == "application/problem+json"
    assert missing_user.json()["code"] == "user_not_found"
    assert expired_session.status_code == 410
    assert expired_session.json()["code"] == "session_expired"
    assert [fault.code for fault in faults] == [
        "user_not_found",
        "session_not_found",
        "session_expired",
    ]
    assert document["paths"]["/api/v1/users/{user_id}"]["get"]["responses"]["404"][
        "content"
    ]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/UserNotFoundProblem"
    }
    assert document["paths"]["/api/v1/sessions/{session_id}"]["get"]["responses"][
        "410"
    ]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/SessionExpiredProblem"
    }
