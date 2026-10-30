"""
Client Azure con autenticazione identity-first.

Fonte unica: src/_shared/azure_clients.py — copiato nei build context dei servizi
da tools/Sync-Shared.ps1. Non modificare le copie, modificare qui.

Regola di selezione (storage e Service Bus):
  1. variabile "identity" presente (nome account / namespace) -> DefaultAzureCredential
  2. altrimenti connection string presente -> from_connection_string + warning "legacy"
  3. altrimenti RuntimeError che nomina entrambe le variabili
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient
from azure.storage.blob import BlobServiceClient, generate_blob_sas

logger = logging.getLogger("azure_clients")

SERVICEBUS_SUFFIX = ".servicebus.windows.net"
MAX_USER_DELEGATION_SAS = timedelta(days=7)

_credential: Optional[DefaultAzureCredential] = None
_credential_aio = None


def _reset_credential_cache() -> None:
    """Solo per i test."""
    global _credential, _credential_aio
    _credential = None
    _credential_aio = None


def get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_credential_aio():
    global _credential_aio
    if _credential_aio is None:
        from azure.identity.aio import DefaultAzureCredential as AioCredential

        _credential_aio = AioCredential()
    return _credential_aio


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _missing(identity_var: str, conn_str_var: str) -> RuntimeError:
    return RuntimeError(
        f"Configurare {identity_var} (managed identity) oppure {conn_str_var} (legacy)"
    )


def _warn_legacy(identity_var: str, conn_str_var: str) -> None:
    logger.warning(
        "%s assente: uso la connection string legacy %s. Migrare a %s + managed identity.",
        identity_var,
        conn_str_var,
        identity_var,
    )


def describe_auth_mode(
    account_name_var: str = "AZURE_STORAGE_ACCOUNT_NAME",
    conn_str_var: str = "AZURE_STORAGE_CONNECTION_STRING",
) -> str:
    if _env(account_name_var):
        return "identity"
    if _env(conn_str_var):
        return "connection_string"
    return "missing"


def _blob_account_url(account_name: str) -> str:
    return f"https://{account_name}.blob.core.windows.net"


def get_blob_service_client(
    account_name_var: str = "AZURE_STORAGE_ACCOUNT_NAME",
    conn_str_var: str = "AZURE_STORAGE_CONNECTION_STRING",
) -> BlobServiceClient:
    account = _env(account_name_var)
    if account:
        return BlobServiceClient(account_url=_blob_account_url(account), credential=get_credential())
    conn = _env(conn_str_var)
    if conn:
        _warn_legacy(account_name_var, conn_str_var)
        return BlobServiceClient.from_connection_string(conn)
    raise _missing(account_name_var, conn_str_var)


def get_blob_service_client_aio(
    account_name_var: str = "AZURE_STORAGE_ACCOUNT_NAME",
    conn_str_var: str = "AZURE_STORAGE_CONNECTION_STRING",
):
    from azure.storage.blob.aio import BlobServiceClient as AioBlobServiceClient

    account = _env(account_name_var)
    if account:
        return AioBlobServiceClient(
            account_url=_blob_account_url(account), credential=get_credential_aio()
        )
    conn = _env(conn_str_var)
    if conn:
        _warn_legacy(account_name_var, conn_str_var)
        return AioBlobServiceClient.from_connection_string(conn)
    raise _missing(account_name_var, conn_str_var)


def servicebus_fqdn(namespace: str) -> str:
    ns = namespace.strip().rstrip("/")
    if ns.startswith("sb://"):
        ns = ns[len("sb://") :]
    if "." not in ns:
        ns = ns + SERVICEBUS_SUFFIX
    return ns


def get_servicebus_client(
    namespace_var: str = "SERVICE_BUS_NAMESPACE",
    conn_str_var: str = "SERVICE_BUS_CONNECTION_STRING",
) -> ServiceBusClient:
    ns = _env(namespace_var)
    if ns:
        return ServiceBusClient(fully_qualified_namespace=servicebus_fqdn(ns), credential=get_credential())
    conn = _env(conn_str_var)
    if conn:
        _warn_legacy(namespace_var, conn_str_var)
        return ServiceBusClient.from_connection_string(conn)
    raise _missing(namespace_var, conn_str_var)


def get_servicebus_client_aio(
    namespace_var: str = "SERVICE_BUS_NAMESPACE",
    conn_str_var: str = "SERVICE_BUS_CONNECTION_STRING",
):
    from azure.servicebus.aio import ServiceBusClient as AioServiceBusClient

    ns = _env(namespace_var)
    if ns:
        return AioServiceBusClient(
            fully_qualified_namespace=servicebus_fqdn(ns), credential=get_credential_aio()
        )
    conn = _env(conn_str_var)
    if conn:
        _warn_legacy(namespace_var, conn_str_var)
        return AioServiceBusClient.from_connection_string(conn)
    raise _missing(namespace_var, conn_str_var)


def get_blob_sas_token(
    blob_service: BlobServiceClient,
    container: str,
    blob: str,
    permission,
    expiry: datetime,
) -> str:
    """
    SAS per un blob. Con account key usa la firma classica; con credenziale Entra ID usa
    una user delegation key (scadenza massima 7 giorni, limite Azure).
    """
    account_key = getattr(blob_service.credential, "account_key", None)
    if account_key:
        return generate_blob_sas(
            account_name=blob_service.account_name,
            container_name=container,
            blob_name=blob,
            account_key=account_key,
            permission=permission,
            expiry=expiry,
        )

    now = datetime.now(timezone.utc)
    capped_expiry = min(expiry, now + MAX_USER_DELEGATION_SAS)
    udk = blob_service.get_user_delegation_key(
        key_start_time=now - timedelta(minutes=5),
        key_expiry_time=capped_expiry,
    )
    return generate_blob_sas(
        account_name=blob_service.account_name,
        container_name=container,
        blob_name=blob,
        user_delegation_key=udk,
        permission=permission,
        expiry=capped_expiry,
    )
