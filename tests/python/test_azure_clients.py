import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "_shared"))

import azure_clients  # noqa: E402

FAKE_STORAGE_CS = (
    "DefaultEndpointsProtocol=https;AccountName=legacyacct;"
    "AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net"
)
FAKE_SB_CS = (
    "Endpoint=sb://legacy-ns.servicebus.windows.net/;"
    "SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=RkFLRQ=="
)


class FakeCred:
    instances = 0

    def __init__(self, *args, **kwargs):
        FakeCred.instances += 1

    def get_token(self, *scopes, **kwargs):  # pragma: no cover - never called
        raise AssertionError("token must not be requested in unit tests")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_STORAGE_CONNECTION_STRING",
        "SERVICE_BUS_NAMESPACE",
        "SERVICE_BUS_CONNECTION_STRING",
        "AZURE_SERVICEBUS_NAMESPACE",
        "AZURE_SERVICEBUS_CONNECTION_STRING",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(azure_clients, "DefaultAzureCredential", FakeCred)
    azure_clients._reset_credential_cache()
    FakeCred.instances = 0
    yield


def test_blob_identity_when_account_set(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    client = azure_clients.get_blob_service_client()
    assert client.url == "https://acct.blob.core.windows.net/"
    assert azure_clients.describe_auth_mode() == "identity"


def test_blob_connection_string_fallback_warns(monkeypatch, caplog):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", FAKE_STORAGE_CS)
    with caplog.at_level(logging.WARNING):
        client = azure_clients.get_blob_service_client()
    assert client.account_name == "legacyacct"
    assert "legacy" in caplog.text
    assert "AZURE_STORAGE_ACCOUNT_NAME" in caplog.text
    assert azure_clients.describe_auth_mode() == "connection_string"


def test_blob_missing_raises():
    with pytest.raises(RuntimeError) as exc:
        azure_clients.get_blob_service_client()
    assert "AZURE_STORAGE_ACCOUNT_NAME" in str(exc.value)
    assert "AZURE_STORAGE_CONNECTION_STRING" in str(exc.value)
    assert azure_clients.describe_auth_mode() == "missing"


def test_prefers_identity_when_both_set(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", FAKE_STORAGE_CS)
    client = azure_clients.get_blob_service_client()
    assert client.account_name == "acct"
    assert azure_clients.describe_auth_mode() == "identity"


def test_credential_is_cached_per_process(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    azure_clients.get_blob_service_client()
    azure_clients.get_blob_service_client()
    assert FakeCred.instances == 1


def test_servicebus_namespace_short_name_gets_suffix(monkeypatch):
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE", "polis-messaging-prod")
    client = azure_clients.get_servicebus_client()
    assert client.fully_qualified_namespace == "polis-messaging-prod.servicebus.windows.net"


def test_servicebus_fqdn_kept_as_is(monkeypatch):
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE", "polis-messaging-prod.servicebus.windows.net")
    client = azure_clients.get_servicebus_client()
    assert client.fully_qualified_namespace == "polis-messaging-prod.servicebus.windows.net"


def test_servicebus_connection_string_fallback(monkeypatch, caplog):
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", FAKE_SB_CS)
    with caplog.at_level(logging.WARNING):
        client = azure_clients.get_servicebus_client()
    assert client.fully_qualified_namespace == "legacy-ns.servicebus.windows.net"
    assert "legacy" in caplog.text


def test_servicebus_custom_var_names(monkeypatch):
    monkeypatch.setenv("AZURE_SERVICEBUS_NAMESPACE", "custom-ns")
    client = azure_clients.get_servicebus_client(
        namespace_var="AZURE_SERVICEBUS_NAMESPACE",
        conn_str_var="AZURE_SERVICEBUS_CONNECTION_STRING",
    )
    assert client.fully_qualified_namespace == "custom-ns.servicebus.windows.net"


def test_servicebus_missing_raises():
    with pytest.raises(RuntimeError) as exc:
        azure_clients.get_servicebus_client()
    assert "SERVICE_BUS_NAMESPACE" in str(exc.value)
    assert "SERVICE_BUS_CONNECTION_STRING" in str(exc.value)


class _FakeBlobService:
    def __init__(self, with_key):
        self.account_name = "acct"
        self.credential = type("C", (), {"account_key": "k"})() if with_key else FakeCred()
        self.udk_calls = []

    def get_user_delegation_key(self, key_start_time, key_expiry_time):
        self.udk_calls.append((key_start_time, key_expiry_time))
        return "UDK"


def test_sas_uses_user_delegation_key_without_account_key(monkeypatch):
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return "sig=abc"

    monkeypatch.setattr(azure_clients, "generate_blob_sas", fake_generate)
    svc = _FakeBlobService(with_key=False)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    token = azure_clients.get_blob_sas_token(svc, "cont", "path/blob.pdf", "r", expiry)
    assert token == "sig=abc"
    assert len(svc.udk_calls) == 1
    assert captured["user_delegation_key"] == "UDK"
    assert "account_key" not in captured
    assert captured["container_name"] == "cont" and captured["blob_name"] == "path/blob.pdf"


def test_sas_uses_account_key_when_available(monkeypatch):
    captured = {}
    monkeypatch.setattr(azure_clients, "generate_blob_sas", lambda **kw: captured.update(kw) or "sig")
    svc = _FakeBlobService(with_key=True)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    azure_clients.get_blob_sas_token(svc, "cont", "b", "r", expiry)
    assert captured["account_key"] == "k"
    assert svc.udk_calls == []


def test_sas_caps_expiry_to_7_days(monkeypatch):
    captured = {}
    monkeypatch.setattr(azure_clients, "generate_blob_sas", lambda **kw: captured.update(kw) or "sig")
    svc = _FakeBlobService(with_key=False)
    now = datetime.now(timezone.utc)
    azure_clients.get_blob_sas_token(svc, "cont", "b", "r", now + timedelta(days=30))
    _, key_expiry = svc.udk_calls[0]
    assert key_expiry <= now + timedelta(days=7, minutes=1)
    assert captured["expiry"] <= now + timedelta(days=7, minutes=1)
