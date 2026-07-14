"""Versioned identities for externally discovered mods users chose to ignore."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from .manifest_store import _is_reparse, _relative_path_key, validate_no_reparse_ancestors
from .storage import JsonStore

_ALLOWED_ROOTS = {
    "pak": {"tilde_mods"},
    "logicpak": {"logic_mods"},
    "ue4ss": {"ue4ss_classic", "ue4ss_nested"},
}
_ENTRY_FIELDS = {"kind", "root", "key"}
_TOP_FIELDS = {"schema_version", "games"}


class IgnoredStoreInvalid(ValueError):
    pass


@dataclass(frozen=True)
class IgnoredIdentity:
    kind: Literal["pak", "logicpak", "ue4ss"]
    root: Literal["tilde_mods", "logic_mods", "ue4ss_classic", "ue4ss_nested"]
    key: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or type(self.root) is not str or type(self.key) is not str:
            raise ValueError("ignored identity fields must be strings")
        kind = self.kind.casefold()
        root = self.root.casefold()
        if kind not in _ALLOWED_ROOTS or root not in _ALLOWED_ROOTS[kind]:
            raise ValueError("ignored identity kind/root pair is invalid")
        normalized_key = _relative_path_key(self.key)
        if len(PureWindowsPath(self.key).parts) != 1:
            raise ValueError("ignored identity key must be one path component")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "key", normalized_key)


class IgnoredModStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._json = JsonStore(self.path)

    @staticmethod
    def _fingerprint(value: str) -> str:
        if type(value) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ValueError("game fingerprint must be 64 hexadecimal characters")
        return value.casefold()

    @staticmethod
    def _entry(value: Any) -> IgnoredIdentity:
        if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
            raise IgnoredStoreInvalid("ignored identity fields are invalid")
        try:
            return IgnoredIdentity(value["kind"], value["root"], value["key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IgnoredStoreInvalid("ignored identity is invalid") from exc

    def _read_locked(self) -> dict[str, tuple[IgnoredIdentity, ...]]:
        if not self.path.exists():
            return {}
        try:
            validate_no_reparse_ancestors(self.path)
            if _is_reparse(self.path) or not self.path.is_file():
                raise IgnoredStoreInvalid("ignored store path is unsafe")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != _TOP_FIELDS:
                raise IgnoredStoreInvalid("ignored store fields are invalid")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
                raise IgnoredStoreInvalid("ignored store schema is invalid")
            games = raw["games"]
            if not isinstance(games, dict):
                raise IgnoredStoreInvalid("ignored games must be an object")
            parsed: dict[str, tuple[IgnoredIdentity, ...]] = {}
            for fingerprint, entries in games.items():
                normalized = self._fingerprint(fingerprint)
                if not isinstance(entries, list):
                    raise IgnoredStoreInvalid("ignored game entries must be a list")
                identities = tuple(self._entry(entry) for entry in entries)
                if len(set(identities)) != len(identities):
                    raise IgnoredStoreInvalid("ignored identities contain duplicates")
                parsed[normalized] = tuple(sorted(identities, key=self._sort_key))
            return parsed
        except IgnoredStoreInvalid:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IgnoredStoreInvalid("ignored store is invalid") from exc

    @staticmethod
    def _sort_key(identity: IgnoredIdentity) -> tuple[str, str, str]:
        return identity.kind, identity.root, identity.key

    @staticmethod
    def _serialize(games: dict[str, tuple[IgnoredIdentity, ...]]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "games": {
                fingerprint: [
                    {"kind": item.kind, "root": item.root, "key": item.key}
                    for item in sorted(entries, key=IgnoredModStore._sort_key)
                ]
                for fingerprint, entries in sorted(games.items())
                if entries
            },
        }

    def _write_locked(self, games: dict[str, tuple[IgnoredIdentity, ...]]) -> None:
        validate_no_reparse_ancestors(self.path)
        self._json._write_unlocked(self._serialize(games))

    def add(self, game_fingerprint: str, identity: IgnoredIdentity) -> None:
        fingerprint = self._fingerprint(game_fingerprint)
        if not isinstance(identity, IgnoredIdentity):
            raise TypeError("identity must be IgnoredIdentity")
        with self._json.locked():
            games = self._read_locked()
            entries = set(games.get(fingerprint, ()))
            entries.add(identity)
            games[fingerprint] = tuple(sorted(entries, key=self._sort_key))
            self._write_locked(games)

    def remove(self, game_fingerprint: str, identity: IgnoredIdentity) -> None:
        fingerprint = self._fingerprint(game_fingerprint)
        if not isinstance(identity, IgnoredIdentity):
            raise TypeError("identity must be IgnoredIdentity")
        with self._json.locked():
            games = self._read_locked()
            entries = set(games.get(fingerprint, ()))
            entries.discard(identity)
            if entries:
                games[fingerprint] = tuple(sorted(entries, key=self._sort_key))
            else:
                games.pop(fingerprint, None)
            self._write_locked(games)

    def contains(self, game_fingerprint: str, identity: IgnoredIdentity) -> bool:
        return identity in self.list(game_fingerprint)

    def list(self, game_fingerprint: str) -> tuple[IgnoredIdentity, ...]:
        fingerprint = self._fingerprint(game_fingerprint)
        with self._json.locked():
            return self._read_locked().get(fingerprint, ())

    def replace(
        self, game_fingerprint: str, entries: tuple[IgnoredIdentity, ...]
    ) -> None:
        fingerprint = self._fingerprint(game_fingerprint)
        if not all(isinstance(item, IgnoredIdentity) for item in entries):
            raise TypeError("entries must contain IgnoredIdentity values")
        normalized = tuple(sorted(set(entries), key=self._sort_key))
        with self._json.locked():
            games = self._read_locked()
            if normalized:
                games[fingerprint] = normalized
            else:
                games.pop(fingerprint, None)
            self._write_locked(games)

    def reset(self, game_fingerprint: str) -> int:
        fingerprint = self._fingerprint(game_fingerprint)
        with self._json.locked():
            games = self._read_locked()
            removed = len(games.pop(fingerprint, ()))
            self._write_locked(games)
            return removed
