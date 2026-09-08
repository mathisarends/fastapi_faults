from examples.minimal.app import app
from fastapi.testclient import TestClient


def test_minimal_example_runtime_and_openapi() -> None:
    response = TestClient(app).get("/sessions/0198e2ef-799c-765d-9ab4-2130d11d1e70")
    document = app.openapi()

    assert response.status_code == 404
    assert response.json() == {
        "type": "https://api.example.com/problems/session_not_found",
        "title": "Session not found",
        "status": 404,
        "code": "session_not_found",
        "detail": ("Session 0198e2ef-799c-765d-9ab4-2130d11d1e70 does not exist."),
    }
    assert (
        document["paths"]["/sessions/{session_id}"]["get"]["responses"]["404"][
            "content"
        ]["application/problem+json"]["schema"]["$ref"]
        == "#/components/schemas/SessionNotFoundProblem"
    )
