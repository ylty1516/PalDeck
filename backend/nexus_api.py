"""Nexus Mods (N网) GraphQL client for Palworld mods."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
GAME_DOMAIN = "palworld"
GAME_ID = 6063  # Nexus internal game id for Palworld
USER_AGENT = "PalworldModManager/1.0 (desktop; +local)"


def _post_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"N网请求失败 HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 N 网: {e.reason}") from e

    if "errors" in data and data["errors"]:
        msg = data["errors"][0].get("message", str(data["errors"][0]))
        raise RuntimeError(f"N网 GraphQL 错误: {msg}")
    return data.get("data") or {}


MOD_FIELDS = """
  modId
  name
  summary
  downloads
  endorsements
  author
  status
  pictureUrl
  thumbnailUrl
  version
  updatedAt
  createdAt
  uid
"""


def _normalize(node: dict[str, Any]) -> dict[str, Any]:
    mod_id = node.get("modId")
    return {
        "mod_id": mod_id,
        "nexus_id": mod_id,  # 用户说的「N网尾号」
        "name": node.get("name") or "",
        "summary": node.get("summary") or "",
        "downloads": node.get("downloads") or 0,
        "endorsements": node.get("endorsements") or 0,
        "author": node.get("author") or "",
        "status": node.get("status") or "",
        "picture_url": node.get("pictureUrl") or node.get("thumbnailUrl") or "",
        "version": node.get("version") or "",
        "updated_at": node.get("updatedAt") or "",
        "created_at": node.get("createdAt") or "",
        "uid": node.get("uid") or "",
        "url": f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{mod_id}" if mod_id else "",
    }


def fetch_popular(count: int = 24, sort: str = "downloads") -> list[dict[str, Any]]:
    """Hot / popular mods sorted by downloads or endorsements."""
    count = max(1, min(int(count), 50))
    sort_field = "endorsements" if sort == "endorsements" else "downloads"
    query = f"""
    query Popular($count: Int) {{
      mods(
        count: $count
        filter: {{ gameDomainName: [{{ value: "{GAME_DOMAIN}", op: EQUALS }}] }}
        sort: [{{ {sort_field}: {{ direction: DESC }} }}]
      ) {{
        nodes {{ {MOD_FIELDS} }}
      }}
    }}
    """
    data = _post_graphql(query, {"count": count})
    nodes = (((data.get("mods") or {}).get("nodes")) or [])
    return [_normalize(n) for n in nodes if n]


def search_mods(keyword: str, count: int = 24) -> list[dict[str, Any]]:
    """Search Palworld mods by name (wildcard)."""
    keyword = (keyword or "").strip()
    if not keyword:
        return fetch_popular(count)

    # Pure numeric → treat as mod id lookup
    if keyword.isdigit():
        mod = get_mod(int(keyword))
        return [mod] if mod else []

    count = max(1, min(int(count), 50))
    # Escape quotes in keyword for GraphQL string
    safe = keyword.replace("\\", "\\\\").replace('"', '\\"')
    query = f"""
    query Search($count: Int) {{
      mods(
        count: $count
        filter: {{
          gameDomainName: [{{ value: "{GAME_DOMAIN}", op: EQUALS }}]
          name: [{{ value: "{safe}", op: WILDCARD }}]
        }}
        sort: [{{ downloads: {{ direction: DESC }} }}]
      ) {{
        nodes {{ {MOD_FIELDS} }}
      }}
    }}
    """
    data = _post_graphql(query, {"count": count})
    nodes = (((data.get("mods") or {}).get("nodes")) or [])
    return [_normalize(n) for n in nodes if n]


def fetch_latest(count: int = 24) -> list[dict[str, Any]]:
    count = max(1, min(int(count), 50))
    query = f"""
    query Latest($count: Int) {{
      mods(
        count: $count
        filter: {{ gameDomainName: [{{ value: "{GAME_DOMAIN}", op: EQUALS }}] }}
        sort: [{{ createdAt: {{ direction: DESC }} }}]
      ) {{
        nodes {{ {MOD_FIELDS} }}
      }}
    }}
    """
    data = _post_graphql(query, {"count": count})
    nodes = (((data.get("mods") or {}).get("nodes")) or [])
    return [_normalize(n) for n in nodes if n]


def get_mod(mod_id: int) -> dict[str, Any] | None:
    """Lookup a single mod by N网尾号 (modId)."""
    mid = int(mod_id)
    # gameId/modId are GraphQL ID scalars — inline is simplest and reliable
    query = f"""
    query {{
      mod(gameId: {GAME_ID}, modId: {mid}) {{
        {MOD_FIELDS}
      }}
    }}
    """
    try:
        data = _post_graphql(query)
        node = data.get("mod")
        if node:
            return _normalize(node)
    except RuntimeError:
        pass
    return None
