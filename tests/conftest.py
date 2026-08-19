import pytest


@pytest.fixture(autouse=True)
def _no_real_supabase_writes(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
