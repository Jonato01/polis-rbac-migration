import filecmp
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "src" / "_shared" / "azure_clients.py"
MAPPING = ROOT / "tools" / "mapping.json"


def test_all_copies_identical():
    if not MAPPING.exists():
        pytest.skip("tools/mapping.json non ancora presente")
    targets = json.loads(MAPPING.read_text(encoding="utf-8")).get("sharedTargets", [])
    assert targets, "sharedTargets vuoto"
    for rel in targets:
        copy = ROOT / rel / "azure_clients.py"
        assert copy.exists(), f"manca {copy}"
        assert filecmp.cmp(SHARED, copy, shallow=False), f"copia divergente: {copy}"
