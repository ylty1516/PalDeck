from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest
from PIL import Image

import backend.appearance as appearance_module
from backend.appearance import AppearanceService
from scripts.prepare_default_background import prepare


def _image(path: Path, *, size: tuple[int, int] = (64, 48), format: str | None = None) -> Path:
    Image.new("RGB", size, "royalblue").save(path, format=format)
    return path


def _service(tmp_path: Path) -> AppearanceService:
    default = _image(tmp_path / "default.webp", format="WEBP")
    return AppearanceService(tmp_path / "data", default)


def test_prepare_crops_header_and_writes_rgb_webp(tmp_path):
    source = tmp_path / "source.png"
    target = tmp_path / "nested" / "default.webp"
    image = Image.new("RGB", (100, 1000), "red")
    image.paste("blue", (0, 64, 100, 1000))
    image.save(source)

    prepare(source, target)

    with Image.open(target) as result:
        assert result.format == "WEBP"
        assert result.mode == "RGB"
        assert result.size == (100, 936)
        assert result.getpixel((50, 0))[2] > result.getpixel((50, 0))[0]


def test_rejects_extension_disguised_as_image(tmp_path):
    fake = tmp_path / "wallpaper.png"
    fake.write_text("not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="有效图片"):
        _service(tmp_path).set_background(fake)


def test_rejects_real_image_with_mismatched_extension(tmp_path):
    disguised = _image(tmp_path / "wallpaper.png", format="JPEG")
    with pytest.raises(ValueError, match="扩展名"):
        _service(tmp_path).set_background(disguised)


def test_rejects_oversized_file_without_changing_settings(tmp_path):
    service = _service(tmp_path)
    before = service.get_settings()
    oversized = tmp_path / "large.png"
    oversized.write_bytes(b"x" * (25 * 1024 * 1024 + 1))

    with pytest.raises(ValueError, match="25 MiB"):
        service.set_background(oversized)

    assert service.get_settings() == before
    assert not list((tmp_path / "data" / "backgrounds").glob("*"))


def test_rejects_image_over_dimension_limit(tmp_path):
    too_wide = _image(tmp_path / "wide.png", size=(12001, 1))
    with pytest.raises(ValueError, match="12000"):
        _service(tmp_path).set_background(too_wide)


def test_rejects_animated_multiframe_image(tmp_path):
    animated = tmp_path / "animated.png"
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    frames[0].save(animated, save_all=True, append_images=frames[1:], duration=20)

    with pytest.raises(ValueError, match="动画"):
        _service(tmp_path).set_background(animated)


def test_rejects_pillow_decompression_bomb(tmp_path, monkeypatch):
    source = _image(tmp_path / "bomb.png", size=(10, 3))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)

    with pytest.raises(ValueError, match="解压炸弹"):
        _service(tmp_path).set_background(source)


def test_rejects_symlink_or_reparse_source(tmp_path):
    source = _image(tmp_path / "real.png")
    link = tmp_path / "linked.png"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(ValueError, match="链接"):
        _service(tmp_path).set_background(link)


def test_source_validation_and_copy_use_same_no_follow_handle(tmp_path, monkeypatch):
    source = _image(tmp_path / "user.png")
    replacement = _image(tmp_path / "replacement.png")
    service = _service(tmp_path)
    original_open = appearance_module._open_read_nofollow

    def replace_after_open(path):
        handle = original_open(path)
        source.rename(tmp_path / "detached.png")
        os.replace(replacement, source)
        return handle

    monkeypatch.setattr(appearance_module, "_open_read_nofollow", replace_after_open)
    saved = service.set_background(source)

    with Image.open(saved) as result:
        assert result.getpixel((0, 0)) == (65, 105, 225)


def test_copies_background_with_uuid_and_never_deletes_source(tmp_path):
    source = _image(tmp_path / "user.png")
    service = _service(tmp_path)

    saved = service.set_background(source)

    assert source.exists()
    assert saved.exists()
    assert saved.parent == tmp_path / "data" / "backgrounds"
    assert len(saved.stem) == 32
    assert service.current_background() == saved


def test_settings_are_strictly_validated_and_persisted(tmp_path):
    service = _service(tmp_path)
    settings = service.update_settings({
        "theme": "ivory-sakura",
        "mask": 0.85,
        "blur": 24,
        "position": "bottom-right",
        "petals": "high",
    })

    assert settings == {
        "theme": "ivory-sakura", "mask": 0.85, "blur": 24,
        "position": "bottom-right", "petals": "high", "background": "default",
    }
    assert AppearanceService(tmp_path / "data", tmp_path / "default.webp").get_settings() == settings
    assert json.loads((tmp_path / "data" / "config.json").read_text(encoding="utf-8"))["appearance"] == settings


@pytest.mark.parametrize("theme", ["aurora-glass", "ivory-sakura", "starlit-night"])
def test_accepts_all_three_themes(tmp_path, theme):
    assert _service(tmp_path).update_settings({"theme": theme})["theme"] == theme


@pytest.mark.parametrize("patch", [
    {"theme": "other"}, {"mask": -0.01}, {"mask": 0.86}, {"mask": True},
    {"blur": 25}, {"blur": "2"}, {"position": "center-ish"}, {"petals": "many"},
    {"unknown": 1},
])
def test_rejects_invalid_or_unknown_settings_without_partial_update(tmp_path, patch):
    service = _service(tmp_path)
    before = service.get_settings()
    with pytest.raises(ValueError):
        service.update_settings(patch)
    assert service.get_settings() == before


def test_replacement_rolls_back_new_file_and_keeps_old_on_config_failure(tmp_path, monkeypatch):
    service = _service(tmp_path)
    old = service.set_background(_image(tmp_path / "old.png"))
    before = service.get_settings()

    def fail(_mutator, default=None):
        raise OSError("disk full")

    monkeypatch.setattr(service.store, "update", fail)
    with pytest.raises(OSError):
        service.set_background(_image(tmp_path / "new.png"))

    assert old.exists()
    assert service.get_settings() == before
    assert list((tmp_path / "data" / "backgrounds").iterdir()) == [old]


def test_successful_replacement_deletes_only_old_managed_copy(tmp_path):
    service = _service(tmp_path)
    first_source = _image(tmp_path / "first.png")
    second_source = _image(tmp_path / "second.jpg", format="JPEG")
    old = service.set_background(first_source)

    new = service.set_background(second_source)

    assert not old.exists()
    assert new.exists() and first_source.exists() and second_source.exists()


def test_cleanup_orphans_retries_failed_deletion(tmp_path, monkeypatch):
    service = _service(tmp_path)
    orphan = service.background_dir / ("a" * 32 + ".png")
    orphan.write_bytes(b"orphan")
    unrelated = service.background_dir / "keep.txt"
    unrelated.write_bytes(b"keep")
    original_unlink = Path.unlink
    attempts = 0

    def flaky_unlink(path, *args, **kwargs):
        nonlocal attempts
        if path == orphan and attempts == 0:
            attempts += 1
            raise PermissionError("busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    service.cleanup_orphans()
    assert orphan.exists()
    service.cleanup_orphans()
    assert not orphan.exists()
    assert unrelated.exists()


def test_service_startup_removes_unreferenced_managed_uuid_files(tmp_path):
    data = tmp_path / "data"
    backgrounds = data / "backgrounds"
    backgrounds.mkdir(parents=True)
    orphan = backgrounds / ("b" * 32 + ".webp")
    orphan.write_bytes(b"orphan")
    default = _image(tmp_path / "default.webp", format="WEBP")

    AppearanceService(data, default)

    assert not orphan.exists()


def test_reset_restores_default_and_removes_managed_copy(tmp_path):
    service = _service(tmp_path)
    managed = service.set_background(_image(tmp_path / "custom.webp", format="WEBP"))

    settings = service.reset_background()

    assert settings["background"] == "default"
    assert service.current_background() == tmp_path / "default.webp"
    assert not managed.exists()


def test_appearance_api_requires_authentication(app):
    client = app.test_client()
    for method, url in [
        ("get", "/api/appearance"), ("post", "/api/appearance"),
        ("post", "/api/appearance/background"), ("delete", "/api/appearance/background"),
        ("get", "/api/appearance/background/current"),
    ]:
        assert getattr(client, method)(url).status_code == 403


def test_appearance_api_updates_uploads_serves_and_resets(app, auth_client):
    updated = auth_client.post("/api/appearance", json={
        "theme": "starlit-night", "mask": 0.4, "blur": 3,
        "position": "top-center", "petals": "low",
    })
    assert updated.status_code == 200
    assert updated.json["data"]["theme"] == "starlit-night"

    payload = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(payload, "PNG")
    payload.seek(0)
    uploaded = auth_client.post(
        "/api/appearance/background",
        data={"file": (payload, "自定义背景.png")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    current = auth_client.get("/api/appearance/background/current")
    assert current.status_code == 200
    assert current.mimetype == "image/png"
    assert current.headers["Cache-Control"] == "no-store, max-age=0"

    reset = auth_client.delete("/api/appearance/background")
    assert reset.status_code == 200
    assert reset.json["data"]["background"] == "default"


@pytest.mark.parametrize(
    ("image_format", "filename", "mime"),
    [("JPEG", "photo.jpg", "image/jpeg"), ("WEBP", "photo.webp", "image/webp")],
)
def test_current_background_sets_mime_for_supported_formats(app, auth_client, image_format, filename, mime):
    payload = io.BytesIO()
    Image.new("RGB", (20, 20), "navy").save(payload, image_format)
    payload.seek(0)
    uploaded = auth_client.post(
        "/api/appearance/background",
        data={"file": (payload, filename)},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    assert auth_client.get("/api/appearance/background/current").mimetype == mime


def test_default_background_response_is_webp(app, auth_client):
    response = auth_client.get("/api/appearance/background/current")
    assert response.status_code == 200, (response.json, app.extensions["appearance_service"].default_background)
    assert response.mimetype == "image/webp"


def test_current_api_streams_the_once_opened_handle_during_path_replacement(app, auth_client, tmp_path, monkeypatch):
    service = app.extensions["appearance_service"]
    current_path = service.set_background(_image(tmp_path / "current.png"))
    original_open = service.open_current_background

    def replace_after_open():
        handle, filename = original_open()
        current_path.rename(current_path.with_name("detached.png"))
        Image.new("RGB", (64, 48), "red").save(current_path, "PNG")
        return handle, filename

    monkeypatch.setattr(service, "open_current_background", replace_after_open)
    response = auth_client.get("/api/appearance/background/current")

    with Image.open(io.BytesIO(response.data)) as streamed:
        assert streamed.getpixel((0, 0)) == (65, 105, 225)


def test_game_path_update_preserves_appearance_config(app, auth_client, fake_game_root):
    assert auth_client.post("/api/appearance", json={"theme": "ivory-sakura"}).status_code == 200

    response = auth_client.post("/api/game/set", json={"path": str(fake_game_root)})

    assert response.status_code == 200
    assert auth_client.get("/api/appearance").json["data"]["theme"] == "ivory-sakura"


def test_concurrent_theme_and_game_path_updates_preserve_both(app, fake_game_root):
    barrier = threading.Barrier(2)
    responses = []

    def update_theme():
        client = app.test_client()
        client.get("/?token=test-token")
        barrier.wait(timeout=5)
        responses.append(client.post("/api/appearance", json={"theme": "starlit-night"}))

    def update_game():
        client = app.test_client()
        client.get("/?token=test-token")
        barrier.wait(timeout=5)
        responses.append(client.post("/api/game/set", json={"path": str(fake_game_root)}))

    threads = [threading.Thread(target=update_theme), threading.Thread(target=update_game)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert [response.status_code for response in responses] == [200, 200]
    config = json.loads((Path(app.config["DATA_DIR"]) / "config.json").read_text(encoding="utf-8"))
    assert config["appearance"]["theme"] == "starlit-night"
    assert config["game_path"] == str(fake_game_root)


def test_failed_api_upload_preserves_current_background(app, auth_client):
    payload = io.BytesIO()
    Image.new("RGB", (20, 20), "purple").save(payload, "PNG")
    payload.seek(0)
    assert auth_client.post(
        "/api/appearance/background",
        data={"file": (payload, "good.png")},
        content_type="multipart/form-data",
    ).status_code == 200
    before = app.extensions["appearance_service"].current_background()

    failed = auth_client.post(
        "/api/appearance/background",
        data={"file": (io.BytesIO(b"fake"), "bad.png")},
        content_type="multipart/form-data",
    )

    assert failed.status_code == 400
    assert app.extensions["appearance_service"].current_background() == before
    assert before.exists()
