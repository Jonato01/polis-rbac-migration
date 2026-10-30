import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "import_fatture"


@pytest.fixture
def fresh_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    for name in list(sys.modules):
        if name == "container_app" or name.startswith("container_app."):
            del sys.modules[name]
    for var in (
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_STORAGE_CONNECTION_STRING",
        "AZURE_SERVICEBUS_NAMESPACE",
        "AZURE_SERVICEBUS_CONNECTION_STRING",
        "AZURE_AI_API_KEY",
        "AZURE_AI_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_config_exposes_identity_vars(fresh_modules, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "a")
    monkeypatch.setenv("AZURE_SERVICEBUS_NAMESPACE", "n")
    c = importlib.import_module("container_app.config")
    assert c.AZURE_STORAGE_ACCOUNT_NAME == "a"
    assert c.AZURE_SERVICEBUS_NAMESPACE == "n"


def test_main_required_vars_accept_identity(fresh_modules, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "a")
    monkeypatch.setenv("AZURE_SERVICEBUS_NAMESPACE", "n")
    monkeypatch.setenv("AZURE_AI_API_KEY", "k")
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://x")
    c = importlib.import_module("container_app.config")
    assert c.missing_required_vars() == []


def test_main_required_vars_report_pairs_when_nothing_set(fresh_modules):
    c = importlib.import_module("container_app.config")
    missing = c.missing_required_vars()
    assert "AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING" in missing
    assert "AZURE_SERVICEBUS_NAMESPACE|AZURE_SERVICEBUS_CONNECTION_STRING" in missing
    assert "AZURE_AI_API_KEY" in missing
    assert "AZURE_AI_ENDPOINT" in missing


def test_managers_use_shared_helper(fresh_modules, monkeypatch):
    """blob_manager e queue_manager non devono più costruire client da connection string."""
    blob_src = (ROOT / "container_app" / "blob_manager.py").read_text(encoding="utf-8")
    queue_src = (ROOT / "container_app" / "queue_manager.py").read_text(encoding="utf-8")
    assert "from_connection_string" not in blob_src
    assert "from_connection_string" not in queue_src
    assert "get_blob_service_client" in blob_src
    assert "get_servicebus_client" in queue_src
