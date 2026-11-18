import pytest

from isakisi.db import Store, stamp
from isakisi.profile import save_profile
from isakisi.settings import Settings
from isakisi.sources import scan


@pytest.fixture
def store(tmp_path):
    result = Store(Settings(app_data_dir=tmp_path / "data", _env_file=None))
    result.initialize()
    return result


@pytest.fixture
def candidate(store):
    with store.connect(write=True) as conn:
        save_profile(conn, {"name": "Deniz Öztürk", "email": "deniz@example.invalid"}, [
            {"kind": "skill", "text": "Python, SQL, FastAPI ve Docker", "source": "Doğrulanan beceriler", "verified_at": stamp()},
            {"kind": "experience", "text": "REST API geliştirme", "source": "Proje kaydı", "start": "2020-01-01",
             "end": "2025-01-01", "verified_at": stamp()},
            {"kind": "certification", "text": "Doğrulanmamış sertifika", "source": "Taslak"},
        ], [{"kind": "title", "value": "Backend"}, {"kind": "remote_region", "value": "TR", "required": True}])
    return store


@pytest.fixture
def seeded(candidate):
    scan(candidate, candidate.settings.sources()[0])
    return candidate
