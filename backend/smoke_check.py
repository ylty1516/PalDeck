"""Opt-in packaged release self-check over the real loopback HTTP API."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPORT_PATH = re.compile(r"^[Ff]:[\\/]")
_HANDSHAKE = re.compile(r"^[0-9a-fA-F]{32}$")
_THEMES = ("aurora-glass", "ivory-sakura", "starlit-night")


@dataclass(frozen=True)
class SmokeContext:
    report_path: Path
    marker_path: Path


def smoke_report_path(value: str | None) -> Path | None:
    """Return an F-drive report path, or None when the opt-in is absent/invalid."""
    raw = (value or "").strip()
    if not raw or not _REPORT_PATH.match(raw):
        return None
    candidate = Path(raw).resolve(strict=False)
    if (
        candidate.drive.casefold() != "f:"
        or candidate.name in {"", ".", ".."}
        or candidate.suffix.casefold() != ".json"
        or candidate.is_dir()
    ):
        return None
    return candidate


def smoke_context(
    report_value: str | None,
    handshake: str | None,
    data_dir: str | os.PathLike[str],
    *,
    frozen: bool,
) -> SmokeContext | None:
    """Authorize the one-shot packaged smoke flow without exposing a runtime endpoint."""
    if not frozen or not _HANDSHAKE.fullmatch(handshake or ""):
        return None
    report_path = smoke_report_path(report_value)
    if report_path is None or report_path.exists():
        return None
    writable = Path(data_dir).resolve(strict=False)
    if writable.drive.casefold() != "f:":
        return None
    marker_path = (writable / f".paldeck-smoke-{handshake}").resolve(strict=False)
    if marker_path.parent != writable or not marker_path.is_file():
        return None
    try:
        marker_value = marker_path.read_text(encoding="ascii")
    except OSError:
        return None
    if marker_value != handshake:
        return None
    return SmokeContext(report_path=report_path, marker_path=marker_path)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def run_http_smoke(base_url: str, token: str, report_path: Path, *, frozen: bool) -> dict[str, Any]:
    """Exercise release-critical behavior through an authenticated HTTP cookie session."""
    items: list[dict[str, Any]] = []
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            base_url + path.lstrip("/"), data=payload, method=method,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        return opener.open(req, timeout=10)

    def passed(name: str, details: Any = None) -> None:
        items.append({"name": name, "pass": True, "details": details})

    def json_data(path: str, *, method: str = "GET", body: dict[str, Any] | None = None):
        with request(path, method=method, body=body) as response:
            if response.status != 200:
                raise AssertionError(f"{method} {path}: HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok") is not True:
            raise AssertionError(f"{method} {path}: ok != true")
        return payload.get("data")

    try:
        with request(f"?token={token}") as response:
            index = response.read().decode("utf-8")
            if response.status != 200:
                raise AssertionError(f"index: HTTP {response.status}")
        markers = [f'id="view-{name}"' for name in ("mods", "import", "nexus", "settings")]
        missing = [marker for marker in [*markers, 'id="petalCanvas"'] if marker not in index]
        if missing:
            raise AssertionError(f"index missing markers: {missing}")
        passed("index_four_views_and_petal_canvas", {"markers": [*markers, 'id="petalCanvas"']})

        health = json_data("api/health")
        if health.get("status") != "up" or health.get("frozen") is not True:
            raise AssertionError(f"health mismatch: {health}")
        passed("health", health)

        game = json_data("api/game/status")
        if game != {"configured": False, "path": None}:
            raise AssertionError(f"fresh data game status mismatch: {game}")
        passed("fresh_data_no_game_path", game)

        appearance = json_data("api/appearance")
        passed("appearance_get", appearance)

        for theme in _THEMES:
            updated = json_data("api/appearance", method="POST", body={"theme": theme})
            confirmed = json_data("api/appearance")
            if updated.get("theme") != theme or confirmed.get("theme") != theme:
                raise AssertionError(f"theme did not persist: {theme}")
            passed(f"theme_{theme}", {"post": updated.get("theme"), "get": confirmed.get("theme")})

        for petals in ("high", "off"):
            updated = json_data("api/appearance", method="POST", body={"petals": petals})
            confirmed = json_data("api/appearance")
            if updated.get("petals") != petals or confirmed.get("petals") != petals:
                raise AssertionError(f"petals did not persist: {petals}")
            passed(f"petals_{petals}", {"post": updated.get("petals"), "get": confirmed.get("petals")})

        with request("api/appearance/background/current") as response:
            content_type = response.headers.get_content_type()
            payload = response.read()
            if response.status != 200 or content_type != "image/webp" or not payload:
                raise AssertionError(
                    f"default background mismatch: status={response.status}, type={content_type}, bytes={len(payload)}"
                )
        passed("default_background_webp", {"content_type": content_type, "bytes": len(payload)})

        report = {"ok": True, "pid": os.getpid(), "frozen": frozen, "base_url": base_url, "items": items}
        _write_report(report_path, report)
        return report
    except Exception as exc:
        report = {
            "ok": False, "pid": os.getpid(), "frozen": frozen, "base_url": base_url,
            "items": items, "error": f"{type(exc).__name__}: {exc}",
        }
        _write_report(report_path, report)
        raise
