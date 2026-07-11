import pytest


@pytest.fixture
def fake_game_root(tmp_path):
    root = tmp_path / "Palworld"
    (root / "Pal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Pal" / "Content" / "Paks").mkdir(parents=True)
    (root / "Palworld.exe").touch()
    (root / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe").touch()
    return root
