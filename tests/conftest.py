import pytest


@pytest.fixture(autouse=True)
def _isolated_recall_environment(tmp_path, monkeypatch):
    # The dev shell points RECALL_ROOT at a real archive; tests must never fall back to it.
    monkeypatch.setenv("RECALL_ROOT", str(tmp_path / "default-root"))
    monkeypatch.delenv("RECALL_RELOCATION_MAP", raising=False)
