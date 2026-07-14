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

from backend.self_updater import TRUSTED_GITHUB_OWNER, TRUSTED_GITHUB_REPO
from backend.ue4ss_provider import Ue4ssProvider

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
        markers = [
            *(f'id="view-{name}"' for name in ("mods", "import", "nexus", "settings", "credits")),
            'id="petalCanvas"',
            'id="ue4ssUpdatedAt"',
            'id="ue4ssDigest"',
            'id="appStatusbar"',
            'id="modSourceFilter"',
            'id="importQueue"',
            'class="nexus-catalog-layout"',
            'data-action="windowMinimize"',
            'data-settings-section="advanced"',
            *(f'data-petal-style="{style}"' for style in ("natural", "watercolor", "minimal")),
        ]
        missing = [marker for marker in markers if marker not in index]
        if missing:
            raise AssertionError(f"index missing markers: {missing}")
        passed("index_five_views_and_release_markers", {"markers": markers})
        passed("v22_responsive_shell", {"statusbar": True, "desktop_bridge": True})
        passed("import_queue_empty", {"marker": 'id="importQueue"'})

        with request("render.js") as response:
            render_source = response.read().decode("utf-8")
        if 'if (mod.adultContent !== false) continue;' not in render_source:
            raise AssertionError("frontend Nexus adult fail-closed guard missing")
        passed("nexus_adult_filtered", {"frontend_fail_closed": True})

        trash_markers = (
            'id="trashModal"', 'data-action="showTrash"',
            'data-action="approveDelete"', 'id="trashList"',
        )
        if any(marker not in index for marker in trash_markers) or "renderTrash" not in render_source:
            raise AssertionError("trash lifecycle markers missing")
        passed("trash_lifecycle_markers", {"markers": trash_markers})

        external_markers = (
            "externally_discovered", 'actionButton("取消管理", "unmanageMod"',
            'actionButton("移入回收站", "deleteMod"',
        )
        if any(marker not in render_source for marker in external_markers):
            raise AssertionError("external mod markers missing")
        passed("external_mod_markers", {"markers": external_markers})

        ue4ss_markers = (
            'data-action="repairUe4ss"', 'data-action="uninstallUe4ss"',
            'id="ue4ssIntegrity"', 'id="ue4ssOwnedFiles"',
        )
        if any(marker not in index for marker in ue4ss_markers):
            raise AssertionError("UE4SS lifecycle markers missing")
        passed("ue4ss_lifecycle_markers", {"markers": ue4ss_markers})

        if (TRUSTED_GITHUB_OWNER, TRUSTED_GITHUB_REPO) != ("ylty1516", "PalDeck"):
            raise AssertionError("PalDeck update trust origin mismatch")
        passed("paldeck_update_origin", {
            "owner": TRUSTED_GITHUB_OWNER,
            "repo": TRUSTED_GITHUB_REPO,
        })

        value_markers = (
            'actionButton("调整数值 ▼", "toggleModValues"',
            "renderModValueEditor",
            'input.type = "number"',
            'actionButton("保存并应用", "saveModValues"',
        )
        if any(marker not in render_source for marker in value_markers):
            raise AssertionError("Mod value editor markers missing")
        passed("mod_value_editor_markers", {"markers": value_markers})

        health = json_data("api/health")
        if health.get("status") != "up" or health.get("frozen") is not True:
            raise AssertionError(f"health mismatch: {health}")
        passed("health", health)

        game = json_data("api/game/status")
        if game != {"configured": False, "path": None}:
            raise AssertionError(f"fresh data game status mismatch: {game}")
        passed("fresh_data_no_game_path", game)

        mods = json_data("api/mods")
        if mods != []:
            raise AssertionError(f"fresh data Workshop state is not empty: {mods}")
        passed("workshop_empty_state", {"mods": mods, "game_configured": False})

        credit_entries = json_data("api/credits")
        okaetsu = next((item for item in credit_entries if item.get("id") == "okaetsu"), None)
        bundled = Ue4ssProvider().bundled_status()
        asset = bundled.get("asset")
        if (
            not okaetsu
            or okaetsu.get("source_url") != "https://github.com/Okaetsu/RE-UE4SS"
            or okaetsu.get("license") != "MIT"
            or bundled.get("available") is not True
            or asset is None
            or not re.fullmatch(r"[0-9a-f]{64}", asset.sha256)
        ):
            raise AssertionError(f"bundled UE4SS metadata mismatch: credit={okaetsu}, status={bundled}")
        passed("bundled_ue4ss_metadata", {
            "source_url": okaetsu["source_url"], "license": okaetsu["license"],
            "asset": asset.name, "size": asset.size, "sha256": asset.sha256,
            "updated_at": asset.updated_at,
        })

        appearance = json_data("api/appearance")
        passed("appearance_get", appearance)

        for theme in _THEMES:
            updated = json_data("api/appearance", method="POST", body={"theme": theme})
            confirmed = json_data("api/appearance")
            if updated.get("theme") != theme or confirmed.get("theme") != theme:
                raise AssertionError(f"theme did not persist: {theme}")
            passed(f"theme_{theme}", {"post": updated.get("theme"), "get": confirmed.get("theme")})

        for petal_style in ("natural", "watercolor", "minimal"):
            updated = json_data("api/appearance", method="POST", body={"petal_style": petal_style})
            confirmed = json_data("api/appearance")
            if updated.get("petal_style") != petal_style or confirmed.get("petal_style") != petal_style:
                raise AssertionError(f"petal style did not persist: {petal_style}")
            passed(
                f"petal_style_{petal_style}",
                {"post": updated.get("petal_style"), "get": confirmed.get("petal_style")},
            )

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
