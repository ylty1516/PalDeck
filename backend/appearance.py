"""Persistent, validated appearance settings and managed background images."""

from __future__ import annotations

import io
import json
import os
import stat
import uuid
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

MAX_BACKGROUND_BYTES = 25 * 1024 * 1024
MAX_BACKGROUND_DIMENSION = 12000
THEMES = frozenset({"aurora-glass", "ivory-sakura", "starlit-night"})
POSITIONS = frozenset({
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right",
})
PETAL_LEVELS = frozenset({"off", "low", "medium", "high"})
DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "aurora-glass",
    "mask": 0.35,
    "blur": 0,
    "position": "center",
    "petals": "medium",
    "background": "default",
}
_FORMAT_SUFFIXES = {
    "PNG": frozenset({".png"}),
    "JPEG": frozenset({".jpg", ".jpeg"}),
    "WEBP": frozenset({".webp"}),
}
_FORMAT_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class AppearanceService:
    def __init__(self, data_dir: str | os.PathLike[str], default: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self.default_background = Path(default)
        self.background_dir = self.data_dir / "backgrounds"
        self.config_path = self.data_dir / "config.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.background_dir.mkdir(parents=True, exist_ok=True)

    def _read_config(self) -> dict[str, Any]:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_config(self, config: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.data_dir / f".config-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_settings(self) -> dict[str, Any]:
        stored = self._read_config().get("appearance", {})
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(stored, dict):
            for key in settings:
                if key in stored:
                    settings[key] = stored[key]
        try:
            self._validate_settings(settings, allow_background=True)
        except ValueError:
            return dict(DEFAULT_SETTINGS)
        return settings

    @staticmethod
    def _validate_settings(settings: dict[str, Any], *, allow_background: bool = False) -> None:
        allowed = set(DEFAULT_SETTINGS) if allow_background else set(DEFAULT_SETTINGS) - {"background"}
        if set(settings) - allowed:
            raise ValueError("包含未知的外观设置")
        if "theme" in settings and (type(settings["theme"]) is not str or settings["theme"] not in THEMES):
            raise ValueError("主题无效")
        if "mask" in settings and (
            type(settings["mask"]) not in (int, float) or not 0 <= settings["mask"] <= 0.85
        ):
            raise ValueError("遮罩强度必须介于 0 和 0.85")
        if "blur" in settings and (
            type(settings["blur"]) not in (int, float) or not 0 <= settings["blur"] <= 24
        ):
            raise ValueError("模糊度必须介于 0 和 24")
        if "position" in settings and (
            type(settings["position"]) is not str or settings["position"] not in POSITIONS
        ):
            raise ValueError("背景位置无效")
        if "petals" in settings and (
            type(settings["petals"]) is not str or settings["petals"] not in PETAL_LEVELS
        ):
            raise ValueError("樱花密度无效")
        if allow_background and (
            type(settings.get("background")) is not str or not settings["background"]
        ):
            raise ValueError("背景设置无效")

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if type(patch) is not dict:
            raise ValueError("外观设置必须是 JSON 对象")
        self._validate_settings(patch)
        settings = self.get_settings()
        settings.update(patch)
        config = self._read_config()
        config["appearance"] = settings
        self._write_config(config)
        return settings

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError("背景文件不存在或无法读取") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return path.is_symlink() or bool(attributes & reparse_flag)

    def _validated_bytes(self, source: Path, declared_mime: str | None) -> tuple[bytes, str]:
        if self._is_reparse(source):
            raise ValueError("背景文件不能是链接或 reparse point")
        try:
            info = source.stat()
        except OSError as exc:
            raise ValueError("背景文件不存在或无法读取") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("背景文件必须是普通文件")
        if info.st_size > MAX_BACKGROUND_BYTES:
            raise ValueError("背景图片不能超过 25 MiB")
        try:
            with source.open("rb") as handle:
                payload = handle.read(MAX_BACKGROUND_BYTES + 1)
        except OSError as exc:
            raise ValueError("背景文件不存在或无法读取") from exc
        if len(payload) > MAX_BACKGROUND_BYTES:
            raise ValueError("背景图片不能超过 25 MiB")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    actual_format = image.format
                    width, height = image.size
                    if actual_format not in _FORMAT_SUFFIXES:
                        raise ValueError("仅支持 PNG、JPEG 和 WEBP 图片")
                    if source.suffix.casefold() not in _FORMAT_SUFFIXES[actual_format]:
                        raise ValueError("图片实际格式与扩展名不一致")
                    if declared_mime and declared_mime.casefold() != _FORMAT_MIMES[actual_format]:
                        raise ValueError("图片实际格式与 MIME 类型不一致")
                    if width > MAX_BACKGROUND_DIMENSION or height > MAX_BACKGROUND_DIMENSION:
                        raise ValueError("背景图片尺寸不能超过 12000×12000")
                    if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                        raise ValueError("不支持动画或多帧背景图片")
                    image.load()
        except ValueError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError("背景图片可能是解压炸弹") from exc
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ValueError("文件不是有效图片") from exc
        return payload, actual_format

    def _managed_path(self, setting: str) -> Path | None:
        if setting == "default" or Path(setting).name != setting:
            return None
        candidate = self.background_dir / setting
        if not candidate.is_file() or self._is_reparse(candidate):
            return None
        return candidate

    def set_background(self, source: str | os.PathLike[str], *, declared_mime: str | None = None) -> Path:
        source_path = Path(source)
        payload, actual_format = self._validated_bytes(source_path, declared_mime)
        suffix = next(iter(_FORMAT_SUFFIXES[actual_format]))
        if actual_format == "JPEG":
            suffix = ".jpg"
        destination = self.background_dir / f"{uuid.uuid4().hex}{suffix}"
        old_settings = self.get_settings()
        old_managed = self._managed_path(old_settings["background"])
        try:
            with destination.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            config = self._read_config()
            settings = dict(old_settings)
            settings["background"] = destination.name
            config["appearance"] = settings
            self._write_config(config)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if old_managed is not None and old_managed != destination:
            try:
                old_managed.unlink(missing_ok=True)
            except OSError:
                # A concurrently streamed Windows response may temporarily hold the file.
                pass
        return destination

    def reset_background(self) -> dict[str, Any]:
        settings = self.get_settings()
        old_managed = self._managed_path(settings["background"])
        settings["background"] = "default"
        config = self._read_config()
        config["appearance"] = settings
        self._write_config(config)
        if old_managed is not None:
            try:
                old_managed.unlink(missing_ok=True)
            except OSError:
                # The setting is already committed; never report a false rollback.
                pass
        return settings

    def current_background(self) -> Path:
        managed = self._managed_path(self.get_settings()["background"])
        return managed if managed is not None else self.default_background
