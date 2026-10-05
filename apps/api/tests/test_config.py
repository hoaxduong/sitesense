import pytest
from pydantic import ValidationError

from sitesense_api.config import Settings


def test_environment_uses_sitesense_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESENSE_ENVIRONMENT", "production")

    assert Settings(_env_file=None).environment == "production"


def test_invalid_environment_fails_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESENSE_ENVIRONMENT", "produciton")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
