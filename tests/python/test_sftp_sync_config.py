import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "polis-sal-sftp-sync"
FAKE_CS = "DefaultEndpointsProtocol=https;AccountName=hostacct;AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net"


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    for name in ("config", "azure_clients", "function_app"):
        sys.modules.pop(name, None)
    for var in ("DATA_STORAGE_ACCOUNT_NAME", "AzureWebJobsStorage", "SERVICE_BUS_NAMESPACE", "SERVICE_BUS_CONNECTION_STRING"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_data_storage_from_dedicated_var(fresh, monkeypatch):
    monkeypatch.setenv("DATA_STORAGE_ACCOUNT_NAME", "polisprodstorage")
    config = importlib.import_module("config")
    assert config.DATA_STORAGE_ACCOUNT_NAME == "polisprodstorage"


def test_legacy_fallback_to_webjobs_storage(fresh, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage", FAKE_CS)
    config = importlib.import_module("config")
    assert config.STORAGE_CONNECTION == FAKE_CS
    assert config.DATA_STORAGE_ACCOUNT_NAME == ""


def test_service_bus_namespace_exposed(fresh, monkeypatch):
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE", "polis-messaging-prod")
    config = importlib.import_module("config")
    assert config.SERVICE_BUS_NAMESPACE == "polis-messaging-prod"


def test_function_app_uses_helper():
    src = (ROOT / "function_app.py").read_text(encoding="utf-8")
    assert "from_connection_string" not in src
    assert "get_blob_service_client" in src
    assert "get_servicebus_client" in src
    assert "DATA_STORAGE_ACCOUNT_NAME" in src
    assert "azure-identity" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
