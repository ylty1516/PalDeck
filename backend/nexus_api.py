"""Anonymous, read-only Nexus Mods GraphQL catalog for Palworld."""

from __future__ import annotations

import hashlib
import json
import time
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.storage import JsonStore

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
GAME_DOMAIN = "palworld"
GAME_ID = 6063
USER_AGENT = "PalworldModManager/1.0 (desktop; anonymous-read-only)"


class NexusError(RuntimeError):
    """A readable Nexus transport or response error."""


Transport = Callable[[str, dict[str, Any]], Any]


MOD_FIELDS = """
  modId
  name
  summary
  pictureUrl
  thumbnailUrl
  author
  version
  downloads
  endorsements
  createdAt
  updatedAt
  adultContent
"""

_LIST_QUERIES = {
    "downloads": f"""
      query PopularDownloads($count: Int!, $game: String!) {{
        mods(count: $count, filter: {{ gameDomainName: [{{ value: $game, op: EQUALS }}] }},
          sort: [{{ downloads: {{ direction: DESC }} }}]) {{ nodes {{ {MOD_FIELDS} }} }}
      }}
    """,
    "endorsements": f"""
      query PopularEndorsements($count: Int!, $game: String!) {{
        mods(count: $count, filter: {{ gameDomainName: [{{ value: $game, op: EQUALS }}] }},
          sort: [{{ endorsements: {{ direction: DESC }} }}]) {{ nodes {{ {MOD_FIELDS} }} }}
      }}
    """,
    "latest": f"""
      query LatestMods($count: Int!, $game: String!) {{
        mods(count: $count, filter: {{ gameDomainName: [{{ value: $game, op: EQUALS }}] }},
          sort: [{{ createdAt: {{ direction: DESC }} }}]) {{ nodes {{ {MOD_FIELDS} }} }}
      }}
    """,
}
_SEARCH_QUERY = f"""
  query SearchMods($count: Int!, $game: String!, $keyword: String!) {{
    mods(count: $count, filter: {{
      gameDomainName: [{{ value: $game, op: EQUALS }}]
      name: [{{ value: $keyword, op: WILDCARD }}]
    }}, sort: [{{ downloads: {{ direction: DESC }} }}]) {{ nodes {{ {MOD_FIELDS} }} }}
  }}
"""
_MOD_QUERY = f"""
  query ModById($gameId: ID!, $modId: ID!) {{
    mod(gameId: $gameId, modId: $modId) {{ {MOD_FIELDS} }}
  }}
"""


def _default_transport(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Application-Name": "PalworldModManager",
            "Application-Version": "1.0.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise NexusError(f"Nexus 请求失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NexusError(f"无法连接 Nexus: {reason}") from exc
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NexusError("Nexus 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise NexusError("Nexus JSON 结构无效")
    return value


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalize(node: dict[str, Any]) -> dict[str, Any]:
    mod_id = _number(node.get("modId")) or None
    picture = node.get("pictureUrl") or node.get("thumbnailUrl") or ""
    if not isinstance(picture, str) or not picture.lower().startswith("https://"):
        picture = ""
    text = lambda key: node.get(key) if isinstance(node.get(key), str) else ""
    return {
        "nexus_id": mod_id,
        "name": text("name"),
        "summary": text("summary"),
        "picture_url": picture,
        "author": text("author"),
        "version": text("version"),
        "downloads": _number(node.get("downloads")),
        "endorsements": _number(node.get("endorsements")),
        "created": text("createdAt"),
        "updated": text("updatedAt"),
        "url": f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{mod_id}" if mod_id else "",
        "adultContent": node.get("adultContent") is True,
    }


class NexusCatalog:
    """TTL-backed anonymous Nexus catalog with injectable GraphQL transport."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}
    _max_cache_files = 200
    _max_cache_age = 30 * 24 * 60 * 60

    def __init__(self, cache_dir: str | Path, transport: Transport | None = None, ttl: int = 600):
        self.cache_dir = Path(cache_dir)
        self.transport = transport or _default_transport
        self.ttl = int(ttl)
        if self.ttl < 0:
            raise ValueError("ttl 不能为负数")
        self._cleanup_cache()

    @classmethod
    def _key_lock(cls, path: Path) -> threading.RLock:
        key = str(path.resolve(strict=False)).casefold()
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.RLock())

    def _cleanup_cache(self) -> None:
        try:
            files = list(self.cache_dir.glob("*.json"))
        except OSError:
            return
        now = time.time()
        entries: list[tuple[float, Path]] = []
        for path in files:
            try:
                timestamp = path.stat().st_mtime
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict) and isinstance(value.get("timestamp"), (int, float)):
                        timestamp = float(value["timestamp"])
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
                if now - timestamp > self._max_cache_age:
                    path.unlink(missing_ok=True)
                else:
                    entries.append((timestamp, path))
            except OSError:
                continue
        entries.sort(key=lambda item: item[0], reverse=True)
        for _timestamp, path in entries[self._max_cache_files:]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _count(value: Any) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("count 必须是整数") from exc
        if not 1 <= count <= 50:
            raise ValueError("count 必须介于 1 和 50")
        return count

    def _store(self, kind: str, value: Any) -> JsonStore:
        canonical = json.dumps([kind, value], ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return JsonStore(self.cache_dir / f"{digest}.json")

    @staticmethod
    def _decode(response: Any) -> dict[str, Any]:
        if isinstance(response, (str, bytes, bytearray)):
            try:
                response = json.loads(response)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NexusError("Nexus 返回了无效 JSON") from exc
        if not isinstance(response, dict):
            raise NexusError("Nexus JSON 结构无效")
        errors = response.get("errors")
        if errors:
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = errors[0].get("message") or str(errors[0])
            else:
                message = str(errors)
            raise NexusError(f"Nexus GraphQL 错误: {message}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise NexusError("Nexus 响应结构无效：缺少 data")
        return data

    def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.transport(query, variables)
        except NexusError:
            raise
        except TimeoutError as exc:
            raise NexusError(f"Nexus 请求失败: {exc}") from exc
        except Exception as exc:
            raise NexusError("Nexus 请求失败") from exc
        return self._decode(response)

    def _cached(self, store: JsonStore) -> dict[str, Any] | None:
        cached = store.read(None)
        if not isinstance(cached, dict) or not isinstance(cached.get("items"), list):
            return None
        if not isinstance(cached.get("timestamp"), (int, float)) or not isinstance(cached.get("fetched_at"), str):
            return None
        return cached

    @staticmethod
    def _safe_items(items: list[Any]) -> list[dict[str, Any]]:
        return [item for item in items if isinstance(item, dict) and item.get("adultContent") is not True]

    @classmethod
    def _cache_result(cls, cached: dict[str, Any], *, stale: bool = False, warning: str = "") -> dict[str, Any]:
        return {"items": cls._safe_items(cached["items"]), "source": "cache", "stale": stale,
                "fetched_at": cached["fetched_at"], "warning": warning}

    def _load(self, store: JsonStore, fetch: Callable[[], list[dict[str, Any]]], force: bool) -> dict[str, Any]:
        request_started = time.time()
        request_started_ns = time.time_ns()
        initial = self._cached(store)
        initial_timestamp = initial["timestamp"] if initial is not None else None
        if not force and initial is not None and request_started - initial["timestamp"] < self.ttl:
            return self._cache_result(initial)

        with self._key_lock(store.path):
            cached = self._cached(store)
            completed_ns = cached.get("_completed_ns") if cached is not None else None
            cache_changed_while_waiting = cached is not None and (
                (isinstance(completed_ns, int) and completed_ns >= request_started_ns)
                or (cached["timestamp"] >= request_started and cached["timestamp"] != initial_timestamp)
            )
            if cache_changed_while_waiting:
                return self._cache_result(cached)
            if not force and cached is not None and time.time() - cached["timestamp"] < self.ttl:
                return self._cache_result(cached)
            fallback = cached or initial
            try:
                items = self._safe_items(fetch())
            except NexusError as exc:
                if fallback is None:
                    raise
                return self._cache_result(fallback, stale=True, warning=str(exc))

            timestamp = time.time()
            fetched_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
            warning = ""
            try:
                store.write({
                    "items": items,
                    "timestamp": timestamp,
                    "fetched_at": fetched_at,
                    "_completed_ns": time.time_ns(),
                })
            except Exception:
                warning = "缓存写入失败"
            else:
                self._cleanup_cache()
            return {"items": items, "source": "live", "stale": False,
                    "fetched_at": fetched_at, "warning": warning}

    def popular(self, sort: str = "downloads", force: bool = False, count: int = 24) -> dict[str, Any]:
        if sort not in _LIST_QUERIES:
            raise ValueError("sort 必须是 downloads、endorsements 或 latest")
        count = self._count(count)
        store = self._store("sort", {"sort": sort, "count": count})

        def fetch() -> list[dict[str, Any]]:
            data = self._request(_LIST_QUERIES[sort], {"count": count, "game": GAME_DOMAIN})
            mods = data.get("mods")
            if not isinstance(mods, dict) or not isinstance(mods.get("nodes"), list):
                raise NexusError("Nexus 响应结构无效：mods.nodes")
            return [_normalize(item) for item in mods["nodes"] if isinstance(item, dict)]

        return self._load(store, fetch, bool(force))

    def search(self, keyword: str, force: bool = False, count: int = 24) -> dict[str, Any]:
        keyword = str(keyword or "").strip()
        count = self._count(count)
        if not keyword:
            return self.popular(count=count, force=force)
        if keyword.isdecimal():
            return self.get(int(keyword), force=force)
        store = self._store("query", {"query": keyword, "count": count})

        def fetch() -> list[dict[str, Any]]:
            data = self._request(_SEARCH_QUERY, {"count": count, "game": GAME_DOMAIN, "keyword": keyword})
            mods = data.get("mods")
            if not isinstance(mods, dict) or not isinstance(mods.get("nodes"), list):
                raise NexusError("Nexus 响应结构无效：mods.nodes")
            return [_normalize(item) for item in mods["nodes"] if isinstance(item, dict)]

        return self._load(store, fetch, bool(force))

    def get(self, mod_id: Any, force: bool = False) -> dict[str, Any]:
        if isinstance(mod_id, bool) or not str(mod_id).isdecimal() or int(mod_id) < 1:
            raise ValueError("mod id 必须是正整数")
        mod_id = int(mod_id)
        store = self._store("id", mod_id)

        def fetch() -> list[dict[str, Any]]:
            data = self._request(_MOD_QUERY, {"gameId": str(GAME_ID), "modId": str(mod_id)})
            if "mod" not in data:
                raise NexusError("Nexus 响应结构无效：缺少 mod")
            item = data["mod"]
            if item is None:
                return []
            if not isinstance(item, dict):
                raise NexusError("Nexus 响应结构无效：mod")
            return [_normalize(item)]

        return self._load(store, fetch, bool(force))
