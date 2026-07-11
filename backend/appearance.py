"""Persistent, validated appearance settings and managed background images."""

from __future__ import annotations

import io
import os
import re
import stat
import uuid
import warnings
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, UnidentifiedImageError

from backend.storage import JsonStore

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
_MANAGED_NAME = re.compile(r"^[0-9a-f]{32}\.(?:png|jpe?g|webp)$")


def _open_windows_nofollow(path: Path) -> BinaryIO:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse_point = 0x00200000
    sequential_scan = 0x08000000
    reparse_attribute = 0x00000400

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path), generic_read, share_all, None, open_existing,
        open_reparse_point | sequential_scan, None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), f"无法安全打开文件: {path}")
    try:
        information = ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise OSError(ctypes.get_last_error(), f"无法检查文件: {path}")
        if information.dwFileAttributes & reparse_attribute:
            raise ValueError("背景文件不能是链接或 reparse point")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        descriptor = msvcrt.open_osfhandle(handle, flags)
        handle = None
        return os.fdopen(descriptor, "rb")
    finally:
        if handle is not None:
            close_handle(handle)


def _open_read_nofollow(path: Path) -> BinaryIO:
    """Open one regular file handle without ever following its final link."""
    try:
        if os.name == "nt":
            handle = _open_windows_nofollow(path)
        else:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            handle = os.fdopen(os.open(path, flags), "rb")
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            handle.close()
            raise ValueError("背景文件必须是普通文件")
        return handle
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("背景文件不存在、无法读取或是链接") from exc


class AppearanceService:
    def __init__(self, data_dir: str | os.PathLike[str], default: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self.default_background = Path(default)
        self.background_dir = self.data_dir / "backgrounds"
        self.config_path = self.data_dir / "config.json"
        self.store = JsonStore(self.config_path)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.background_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_orphans()

    @staticmethod
    def _settings_from_config(config: Any) -> dict[str, Any]:
        stored = config.get("appearance", {}) if isinstance(config, dict) else {}
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(stored, dict):
            for key in settings:
                if key in stored:
                    settings[key] = stored[key]
        try:
            AppearanceService._validate_settings(settings, allow_background=True)
        except ValueError:
            return dict(DEFAULT_SETTINGS)
        return settings

    def get_settings(self) -> dict[str, Any]:
        return self._settings_from_config(self.store.read({}))

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
        saved: dict[str, Any] = {}

        def mutate(config: Any) -> dict[str, Any]:
            nonlocal saved
            if not isinstance(config, dict):
                config = {}
            saved = self._settings_from_config(config)
            saved.update(patch)
            config["appearance"] = saved
            return config

        self.store.update(mutate)
        return saved

    def _validated_bytes(self, source: Path, declared_mime: str | None) -> tuple[bytes, str]:
        try:
            with _open_read_nofollow(source) as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > MAX_BACKGROUND_BYTES:
                    raise ValueError("背景图片不能超过 25 MiB")
                payload = handle.read(MAX_BACKGROUND_BYTES + 1)
        except ValueError:
            raise
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

    def _configured_background_path(self) -> Path:
        setting = self.get_settings()["background"]
        if setting != "default" and _MANAGED_NAME.fullmatch(setting):
            return self.background_dir / setting
        return self.default_background

    def current_background(self) -> Path:
        candidate = self._configured_background_path()
        try:
            with _open_read_nofollow(candidate):
                return candidate
        except ValueError:
            return self.default_background

    def open_current_background(self) -> tuple[BinaryIO, str]:
        candidate = self._configured_background_path()
        try:
            return _open_read_nofollow(candidate), candidate.name
        except ValueError:
            return _open_read_nofollow(self.default_background), self.default_background.name

    def cleanup_orphans(self) -> None:
        with self.store.locked():
            current = self._settings_from_config(self.store.read({}))["background"]
            for candidate in self.background_dir.iterdir():
                if candidate.name == current or not _MANAGED_NAME.fullmatch(candidate.name):
                    continue
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    continue

    def set_background(self, source: str | os.PathLike[str], *, declared_mime: str | None = None) -> Path:
        source_path = Path(source)
        payload, actual_format = self._validated_bytes(source_path, declared_mime)
        suffix = ".jpg" if actual_format == "JPEG" else next(iter(_FORMAT_SUFFIXES[actual_format]))
        destination = self.background_dir / f"{uuid.uuid4().hex}{suffix}"
        with self.store.locked():
            try:
                with destination.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())

                def mutate(config: Any) -> dict[str, Any]:
                    if not isinstance(config, dict):
                        config = {}
                    settings = self._settings_from_config(config)
                    settings["background"] = destination.name
                    config["appearance"] = settings
                    return config

                self.store.update(mutate)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            self.cleanup_orphans()
        return destination

    def reset_background(self) -> dict[str, Any]:
        saved: dict[str, Any] = {}
        with self.store.locked():
            def mutate(config: Any) -> dict[str, Any]:
                nonlocal saved
                if not isinstance(config, dict):
                    config = {}
                saved = self._settings_from_config(config)
                saved["background"] = "default"
                config["appearance"] = saved
                return config

            self.store.update(mutate)
            self.cleanup_orphans()
        return saved
