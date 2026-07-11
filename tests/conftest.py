import pytest

from backend.app import create_app


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def fake_game_root(tmp_path):
    root = tmp_path / "Palworld"
    (root / "Pal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Pal" / "Content" / "Paks").mkdir(parents=True)
    (root / "Palworld.exe").touch()
    (root / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe").touch()
    return root


@pytest.fixture
def app(tmp_path, fake_game_root, monkeypatch):
    monkeypatch.setenv("PALMOD_GAME_PATH", str(fake_game_root))
    return create_app(
        root=tmp_path,
        data_dir=tmp_path / "data",
        session_token="test-token",
        testing=True,
    )


@pytest.fixture
def auth_client(app):
    client = app.test_client()
    response = client.get("/?token=test-token")
    assert response.status_code == 302
    return client
