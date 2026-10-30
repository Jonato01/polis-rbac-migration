import importlib
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "servicebus-bridge"


@pytest.fixture
def bridge_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    for name in ("servicebus_bridge", "azure_clients"):
        sys.modules.pop(name, None)
    for var in ("SERVICE_BUS_NAMESPACE", "SERVICE_BUS_CONNECTION_STRING"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE", "ns")
    mod = importlib.import_module("servicebus_bridge")
    return mod


def test_bridge_connects_with_namespace(bridge_module, monkeypatch, capsys):
    calls = []

    class FakeClient:
        pass

    monkeypatch.setattr(bridge_module, "get_servicebus_client", lambda **kw: calls.append(kw) or FakeClient())
    bridge = bridge_module.ServiceBusHTTPBridge()
    ok = bridge.connect()
    out = capsys.readouterr().out
    assert ok is True
    assert len(calls) == 1
    assert "non configurata" not in out


def test_bridge_connect_fails_cleanly_when_nothing_configured(bridge_module, monkeypatch, capsys):
    monkeypatch.delenv("SERVICE_BUS_NAMESPACE", raising=False)

    def boom(**kw):
        raise RuntimeError("Configurare SERVICE_BUS_NAMESPACE (managed identity) oppure SERVICE_BUS_CONNECTION_STRING (legacy)")

    monkeypatch.setattr(bridge_module, "get_servicebus_client", boom)
    bridge = bridge_module.ServiceBusHTTPBridge()
    assert bridge.connect() is False
    assert "SERVICE_BUS_NAMESPACE" in capsys.readouterr().out


def test_no_connection_string_outside_helper():
    offenders = [
        p.name for p in ROOT.glob("*.py")
        if p.name != "azure_clients.py" and "from_connection_string" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_dockerfile_and_requirements():
    assert "azure-identity" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY azure_clients.py" in df
    assert "COPY .env" not in df  # nessun file di segreti nell'immagine


def test_check_dlq_has_no_embedded_connection_string():
    src = (ROOT / "check_dlq.py").read_text(encoding="utf-8")
    assert "SharedAccessKey=" not in src
    assert "Endpoint=sb://" not in src
