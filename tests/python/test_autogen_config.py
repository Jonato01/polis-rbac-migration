import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "import-sal" / "services" / "import" / "autogen-pdf-import"


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    for name in list(sys.modules):
        if name in ("config_root", "config") or name.startswith("config."):
            del sys.modules[name]
    for var in (
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_STORAGE_CONNECTION_STRING",
        "SERVICE_BUS_NAMESPACE",
        "SERVICE_BUS_CONNECTION_STRING",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_config_root_exposes_identity_vars(fresh, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE", "ns")
    root = importlib.import_module("config_root")
    assert root.AZURE_STORAGE_ACCOUNT_NAME == "acct"
    assert root.SERVICE_BUS_NAMESPACE == "ns"


def test_required_accepts_identity_vars(fresh, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE", "ns")
    root = importlib.import_module("config_root")
    assert root.missing_required_vars() == []


def test_required_reports_pairs_when_nothing_set(fresh):
    root = importlib.import_module("config_root")
    missing = root.missing_required_vars()
    assert "AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING" in missing
    assert "SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING" in missing


def test_no_connection_string_clients_outside_helper():
    offenders = []
    for path in ROOT.rglob("*.py"):
        if path.name == "azure_clients.py":
            continue
        if "from_connection_string" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
