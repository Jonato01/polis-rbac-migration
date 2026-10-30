import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "import-sal"
AUTOGEN = ROOT / "services" / "import" / "autogen-pdf-import"


def _py_files():
    for p in ROOT.rglob("*.py"):
        if AUTOGEN in p.parents or p.name == "azure_clients.py":
            continue
        yield p


def _dockerfiles():
    for p in ROOT.rglob("Dockerfile*"):
        if AUTOGEN in p.parents:
            continue
        yield p


def test_no_connection_string_clients_outside_helper():
    offenders = [
        str(p.relative_to(ROOT))
        for p in _py_files()
        if "from_connection_string" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []


def test_no_connection_string_only_guards():
    """I servizi non devono più fermarsi se manca la connection string ma c'è il namespace."""
    pattern = re.compile(r"if not (SERVICE_BUS_CONN|SERVICE_BUS|STORAGE_CONN|service_bus_conn|storage_conn)\b")
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in _py_files()
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_required_dicts_accept_identity_alternative():
    """Le mappe 'required' non devono esigere la sola connection string."""
    pattern = re.compile(r"'(SERVICE_BUS_CONNECTION_STRING|AZURE_STORAGE_CONNECTION_STRING)':\s*(STORAGE_CONN|SERVICE_BUS\w*|storage_conn|service_bus_conn)\b")
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in _py_files()
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_every_dockerfile_copies_helper_and_context_has_it():
    problems = []
    for df in _dockerfiles():
        text = df.read_text(encoding="utf-8", errors="ignore")
        if "COPY azure_clients.py" not in text:
            problems.append(f"{df.relative_to(ROOT)}: manca COPY azure_clients.py")
        # contesto = cartella del Dockerfile, tranne pdf/<tipo>/ che usano services/import/pdf
        context = df.parent
        if context.parent.name == "pdf":
            context = context.parent
        if not (context / "azure_clients.py").exists():
            problems.append(f"{df.relative_to(ROOT)}: helper assente nel contesto {context.relative_to(ROOT)}")
    assert problems == []


def test_orchestrator_has_no_aci_dependency():
    orch = (ROOT / "import_sal_orchestrator.py").read_text(encoding="utf-8")
    utils = (ROOT / "import_sal_utils.py").read_text(encoding="utf-8")
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "containerinstance" not in orch
    assert "containerinstance" not in utils
    assert "create_container_group" not in utils
    assert "azure-mgmt-containerinstance" not in req


def test_validate_configuration_accepts_identity(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    for name in ("import_sal_orchestrator", "import_sal_service_manager", "import_sal_utils", "azure_clients"):
        sys.modules.pop(name, None)
    env = {
        "SERVICE_BUS_NAMESPACE": "polis-messaging-prod",
        "POLIS_API_DATABASE_URL": "http://db", "AZURE_STORAGE_API_URL": "http://st",
        "AZURE_SUBSCRIPTION_ID": "s", "AZURE_RESOURCE_GROUP": "rg", "AZURE_LOCATION": "we",
        "IMPORT_QUEUE": "q1", "ROLLBACK_QUEUE": "q2", "RESPONSE_QUEUE": "q3",
        "STORAGE_CONTAINER": "c", "NOTIFICATION_USER_ID": "1", "NOTIFICATION_ENDPOINT": "n",
        "DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "x", "DB_DATABASE": "d",
        "SFTP_HOST": "h", "SFTP_PORT": "22", "SFTP_USER": "u", "SFTP_PASSWORD": "x", "SFTP_BASE_PATH": "/",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("SERVICE_BUS_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING"):
        monkeypatch.delenv(k, raising=False)
    mod = importlib.import_module("import_sal_orchestrator")
    orch = mod.ImportSalOrchestrator()  # non deve chiamare sys.exit(1)
    assert orch.RESPONSE_QUEUE == "q3"


def test_no_hardcoded_secrets_in_import_sal():
    # password letterali in dict di config e chiavi OpenAI nel sorgente
    pattern = re.compile(r"'password':\s*'[^'{%]{3,}'|sk-[A-Za-z0-9]{20,}")
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in ROOT.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
        if pattern.search(line) and "os.getenv" not in line.split("'password'")[-1][:40]
    ]
    assert offenders == []
