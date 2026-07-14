"""Strict discovery and editing primitives for UE4SS JSON numeric settings."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from .domain import ManifestFile, ModKind, ModManifest
from .manifest_store import _is_reparse, _relative_path_key, validate_no_reparse_ancestors

MAX_CONFIG_BYTES = 256 * 1024
MAX_CONFIG_FIELDS = 64
MAX_SCHEMA_FIELDS = 32
SAFE_MINIMUM = -1_000_000_000
SAFE_MAXIMUM = 1_000_000_000
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_SCHEMA_FIELDS = {
    "schema_version", "mod_id", "display_name", "description", "version", "fields",
}
_FIELD_REQUIRED = {"key", "label", "type", "default"}
_FIELD_OPTIONAL = {"min", "max", "step", "description"}


class ModValueError(RuntimeError):
    def __init__(self, message: str, details: dict[str, object] | None = None):
        self.details = details or {}
        super().__init__(message)


class ModValueNotSupported(ModValueError):
    pass


class ModValueInvalid(ModValueError):
    pass


class ModValueStale(ModValueError):
    pass


class ModValueConflict(ModValueError):
    pass


@dataclass(frozen=True)
class NumericField:
    key: str
    label: str
    kind: Literal["int", "float"]
    value: int | float
    minimum: int | float
    maximum: int | float
    step: int | float | None
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.kind,
            "value": self.value,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "description": self.description,
        }


@dataclass(frozen=True)
class ModValueCapability:
    display_name: str
    description: str
    revision: str
    fields: tuple[NumericField, ...]
    config_relative_path: str

    def to_dict(self, manifest_id: str) -> dict[str, object]:
        return {
            "mod_id": manifest_id,
            "display_name": self.display_name,
            "description": self.description,
            "revision": self.revision,
            "fields": [field.to_dict() for field in self.fields],
        }


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(data: bytes) -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ModValueInvalid("Mod 数值配置不是严格 UTF-8 JSON") from exc


def _finite_number(value: Any, *, integer: bool = False) -> int | float:
    if integer:
        if type(value) is not int:
            raise ModValueInvalid("整数配置字段类型无效")
        return value
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ModValueInvalid("浮点配置字段类型无效")
    return float(value) if type(value) is float else value


def _text(value: Any, name: str, *, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ModValueInvalid(f"{name} 文本无效")
    return value.strip()


class ModValueService:
    @staticmethod
    def _manifest_candidate(manifest: ModManifest, filename: str) -> tuple[str, Path] | None:
        matches: list[str] = []
        for item in manifest.files:
            _relative_path_key(item.relative_path)
            parts = PureWindowsPath(item.relative_path).parts
            if len(parts) == 1 and parts[0].casefold() == filename.casefold():
                matches.append(item.relative_path)
        if len(matches) > 1:
            raise ModValueInvalid(f"存在多个大小写冲突的 {filename}")
        if not matches:
            return None
        relative = matches[0]
        root = validate_no_reparse_ancestors(manifest.install_root)
        path = validate_no_reparse_ancestors(root / Path(*PureWindowsPath(relative).parts))
        if path.parent != root:
            raise ModValueInvalid("配置文件不在 Mod 根目录")
        return relative, path

    @staticmethod
    def _read_file(path: Path, label: str) -> bytes:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ModValueInvalid(f"{label} 缺失") from exc
        if _is_reparse(path) or not stat.S_ISREG(metadata.st_mode):
            raise ModValueInvalid(f"{label} 不是安全普通文件")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ModValueInvalid(f"{label} 超过 256 KiB")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ModValueInvalid(f"无法读取 {label}") from exc

    @staticmethod
    def _generic_fields(config: dict[str, Any]) -> tuple[NumericField, ...]:
        if len(config) > MAX_CONFIG_FIELDS:
            raise ModValueInvalid("config.json 顶层字段过多")
        fields: list[NumericField] = []
        for key, value in config.items():
            if type(value) not in (int, float):
                continue
            if not _KEY.fullmatch(key):
                raise ModValueInvalid("config.json 数值字段名无效")
            number = _finite_number(value, integer=type(value) is int)
            if not SAFE_MINIMUM <= number <= SAFE_MAXIMUM:
                raise ModValueInvalid("config.json 数值超出安全范围")
            fields.append(
                NumericField(
                    key=key,
                    label=key,
                    kind="int" if type(value) is int else "float",
                    value=number,
                    minimum=SAFE_MINIMUM,
                    maximum=SAFE_MAXIMUM,
                    step=None,
                    description="",
                )
            )
        return tuple(fields)

    @staticmethod
    def _schema_fields(
        schema: dict[str, Any], config: dict[str, Any], directory_name: str
    ) -> tuple[str, str, tuple[NumericField, ...]]:
        if set(schema) != _SCHEMA_FIELDS:
            raise ModValueInvalid("palmod_config.json 顶层字段无效")
        if type(schema["schema_version"]) is not int or schema["schema_version"] != 1:
            raise ModValueInvalid("palmod_config.json schema_version 无效")
        mod_id = _text(schema["mod_id"], "mod_id", maximum=128)
        if mod_id.casefold() != directory_name.casefold():
            raise ModValueInvalid("palmod_config.json mod_id 与目录不匹配")
        display_name = _text(schema["display_name"], "display_name", maximum=160)
        description = _text(
            schema["description"], "description", maximum=2000, allow_empty=True
        )
        _text(schema["version"], "version", maximum=80)
        raw_fields = schema["fields"]
        if not isinstance(raw_fields, list) or not 1 <= len(raw_fields) <= MAX_SCHEMA_FIELDS:
            raise ModValueInvalid("palmod_config.json fields 数量无效")
        fields: list[NumericField] = []
        seen: set[str] = set()
        for raw in raw_fields:
            if (
                not isinstance(raw, dict)
                or not _FIELD_REQUIRED.issubset(raw)
                or set(raw) - (_FIELD_REQUIRED | _FIELD_OPTIONAL)
            ):
                raise ModValueInvalid("palmod_config.json field 字段无效")
            key = _text(raw["key"], "field key", maximum=64)
            if not _KEY.fullmatch(key) or key in seen:
                raise ModValueInvalid("palmod_config.json field key 无效或重复")
            seen.add(key)
            if key not in config:
                raise ModValueInvalid("schema 字段在 config.json 中不存在")
            label = _text(raw["label"], "field label", maximum=160)
            field_description = _text(
                raw.get("description", ""),
                "field description",
                maximum=2000,
                allow_empty=True,
            )
            kind = raw["type"]
            if kind not in {"int", "float", "bool"} or type(kind) is not str:
                raise ModValueInvalid("schema field type 无效")
            if kind == "bool":
                if type(raw["default"]) is not bool or type(config[key]) is not bool:
                    raise ModValueInvalid("schema bool 字段类型无效")
                if {"min", "max", "step"} & set(raw):
                    raise ModValueInvalid("schema bool 字段不能声明数值约束")
                continue
            integer = kind == "int"
            current = _finite_number(config[key], integer=integer)
            default = _finite_number(raw["default"], integer=integer)
            minimum = _finite_number(raw.get("min", SAFE_MINIMUM), integer=integer)
            maximum = _finite_number(raw.get("max", SAFE_MAXIMUM), integer=integer)
            step_value = raw.get("step")
            step = None if step_value is None else _finite_number(step_value, integer=integer)
            if minimum > maximum or not minimum <= default <= maximum or not minimum <= current <= maximum:
                raise ModValueInvalid("schema 数值范围无效")
            if step is not None and step <= 0:
                raise ModValueInvalid("schema step 必须为正数")
            fields.append(
                NumericField(
                    key=key,
                    label=label,
                    kind=kind,
                    value=current,
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                    description=field_description,
                )
            )
        if not fields:
            raise ModValueNotSupported("Mod 没有可调整的数值字段")
        return display_name, description, tuple(fields)

    def _capability(self, manifest: ModManifest) -> ModValueCapability:
        if manifest.kind is not ModKind.UE4SS:
            raise ModValueNotSupported("只有 UE4SS Mod 支持数值调整")
        config_candidate = self._manifest_candidate(manifest, "config.json")
        if config_candidate is None:
            raise ModValueNotSupported("Mod 没有受管 config.json")
        config_relative, config_path = config_candidate
        config_bytes = self._read_file(config_path, "config.json")
        config = _strict_json(config_bytes)
        if not isinstance(config, dict):
            raise ModValueInvalid("config.json 顶层必须是 object")
        if len(config) > MAX_CONFIG_FIELDS:
            raise ModValueInvalid("config.json 顶层字段过多")
        schema_candidate = self._manifest_candidate(manifest, "palmod_config.json")
        if schema_candidate is None:
            display_name = manifest.name
            description = ""
            fields = self._generic_fields(config)
            if not fields:
                raise ModValueNotSupported("config.json 没有顶层数值字段")
        else:
            _schema_relative, schema_path = schema_candidate
            schema = _strict_json(self._read_file(schema_path, "palmod_config.json"))
            if not isinstance(schema, dict):
                raise ModValueInvalid("palmod_config.json 顶层必须是 object")
            display_name, description, fields = self._schema_fields(
                schema, config, manifest.install_root.name
            )
        digest = hashlib.sha256(config_bytes).hexdigest()
        return ModValueCapability(
            display_name=display_name,
            description=description,
            revision=f"sha256:{digest}",
            fields=fields,
            config_relative_path=config_relative,
        )

    @staticmethod
    def _validate_new_value(field: NumericField, value: Any) -> int | float:
        if field.kind == "int":
            number = _finite_number(value, integer=True)
        else:
            number = _finite_number(value, integer=False)
        if not field.minimum <= number <= field.maximum:
            raise ModValueInvalid(f"{field.key} 超出允许范围")
        if field.step is not None:
            ratio = (number - field.minimum) / field.step
            if not math.isclose(ratio, round(ratio), rel_tol=1e-9, abs_tol=1e-9):
                raise ModValueInvalid(f"{field.key} 不符合步长")
        return number

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        validate_no_reparse_ancestors(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def update_values(
        self,
        manifest: ModManifest,
        values: dict[str, object],
        revision: str,
        persist,
    ) -> tuple[ModManifest, dict[str, object]]:
        if type(revision) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None:
            raise ModValueInvalid("配置 revision 格式无效")
        if not isinstance(values, dict) or not values or any(type(key) is not str for key in values):
            raise ModValueInvalid("values 必须是非空对象")
        capability = self._capability(manifest)
        fields = {field.key: field for field in capability.fields}
        if set(values) - set(fields):
            raise ModValueInvalid("提交了不支持的数值字段")
        validated = {
            key: self._validate_new_value(fields[key], value)
            for key, value in values.items()
        }
        candidate = self._manifest_candidate(manifest, "config.json")
        if candidate is None:
            raise ModValueNotSupported("Mod 没有受管 config.json")
        relative, path = candidate
        original = self._read_file(path, "config.json")
        current_revision = f"sha256:{hashlib.sha256(original).hexdigest()}"
        if not hmac.compare_digest(revision, current_revision):
            raise ModValueStale(
                "Mod 配置已被其他程序修改，请重新加载",
                {"current_revision": current_revision},
            )
        config = _strict_json(original)
        if not isinstance(config, dict):
            raise ModValueInvalid("config.json 顶层必须是 object")
        updated = config.copy()
        updated.update(validated)
        try:
            published = (
                json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ModValueInvalid("更新后的 config.json 无法安全序列化") from exc
        self._write_atomic(path, published)
        changed_files = tuple(
            ManifestFile(
                item.relative_path,
                len(published),
                hashlib.sha256(published).hexdigest(),
            )
            if _relative_path_key(item.relative_path) == _relative_path_key(relative)
            else item
            for item in manifest.files
        )
        changed = replace(manifest, files=changed_files)
        try:
            persist(changed)
        except BaseException:
            try:
                self._write_atomic(path, original)
            except BaseException as rollback_error:
                raise ModValueConflict(
                    "数值配置保存失败且回滚失败",
                    {"rollback_error": type(rollback_error).__name__},
                ) from rollback_error
            raise
        return changed, self._capability(changed).to_dict(changed.id)

    def inspect_manifest(self, manifest: ModManifest) -> ModValueCapability | None:
        try:
            return self._capability(manifest)
        except (ModValueError, OSError, ValueError):
            return None

    def read_values(self, manifest: ModManifest) -> dict[str, object]:
        return self._capability(manifest).to_dict(manifest.id)
