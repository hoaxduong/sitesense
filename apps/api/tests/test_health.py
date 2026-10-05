import pytest
from fastapi.testclient import TestClient

from sitesense_api.application import create_app
from sitesense_api.config import Settings


@pytest.mark.parametrize("path", ["/healthz", "/api/v1/health"])
def test_health_is_available_in_production(path: str) -> None:
    app = create_app(Settings(environment="production", _env_file=None))
    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "sitesense-api"}


def test_contract_has_one_public_health_operation() -> None:
    app = create_app(Settings(environment="test", _env_file=None))
    schema = app.openapi()

    assert "/healthz" not in schema["paths"]
    operation = schema["paths"]["/api/v1/health"]["get"]
    assert operation["operationId"] == "getHealth"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthResponse"
    }


def test_production_does_not_serve_interactive_docs() -> None:
    app = create_app(Settings(environment="production", _env_file=None))
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
