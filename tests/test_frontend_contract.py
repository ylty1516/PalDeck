from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[1]
FRONTEND = ROOT / "frontend"
HTML = FRONTEND / "index.html"
APP = FRONTEND / "app.js"
API = FRONTEND / "api.js"
EFFECTS = FRONTEND / "effects.js"
RENDER = FRONTEND / "render.js"
CSS = FRONTEND / "styles.css"

REQUIRED_ACTIONS = {
    "refreshMods", "openModsFolder", "chooseModFile", "importMod",
    "searchNexus", "refreshNexus", "autoDetectGame", "saveGamePath",
    "repairFolders", "chooseBackground", "resetBackground", "saveAppearance",
    "installUe4ss", "checkUpdate", "restartAdmin",
}
DYNAMIC_ACTIONS = {"toggleMod", "openModFolder", "deleteMod", "useGamePath", "openNexus", "copyNexusId"}


class ContractParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.actions: list[str] = []
        self.interactive_without_action: list[tuple[str, dict[str, str | None]]] = []
        self.views: list[str] = []
        self.ids: list[str] = []
        self.scripts: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if "view" in (values.get("class") or "").split():
            self.views.append(values.get("id", ""))
        if tag in {"button", "input", "select"} and values.get("type") != "hidden":
            action = values.get("data-action")
            if action:
                self.actions.append(action)
            else:
                self.interactive_without_action.append((tag, values))
        if tag == "script":
            self.scripts.append(values)


def parsed_html() -> ContractParser:
    parser = ContractParser()
    parser.feed(HTML.read_text(encoding="utf-8"))
    return parser


def test_html_has_exactly_four_pages_and_required_layers():
    parser = parsed_html()
    assert parser.views == ["view-mods", "view-import", "view-nexus", "view-settings"]
    assert {"backgroundLayer", "petalCanvas", "toastHost", "busyOverlay", "conflictModal", "deleteModal"} <= set(parser.ids)
    assert parser.scripts == [{"type": "module", "src": "app.js"}]


def test_every_static_control_has_one_unique_registered_action():
    parser = parsed_html()
    assert not parser.interactive_without_action
    assert len(parser.actions) == len(set(parser.actions))
    app = APP.read_text(encoding="utf-8")
    match = re.search(r"export const ACTION_HANDLERS\s*=\s*Object\.freeze\(\{(.*?)^\}\);", app, re.S | re.M)
    assert match, "ACTION_HANDLERS must be an exported frozen object literal"
    registered = set(re.findall(r"^\s{2}([A-Za-z][A-Za-z0-9]*):", match.group(1), re.M))
    assert registered == set(parser.actions)
    assert REQUIRED_ACTIONS <= registered
    for line in match.group(1).splitlines():
        entry = re.match(r"\s{2}([A-Za-z][A-Za-z0-9]*):\s*(.+),$", line)
        if not entry:
            continue
        handler = entry.group(2).strip()
        assert handler not in {"", "undefined", "null", "() => {}", "async () => {}"}
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", handler):
            assert re.search(rf"(?:function|const)\s+{handler}\b", app), handler


def test_dynamic_cards_use_delegation_and_cover_all_cases():
    app = APP.read_text(encoding="utf-8")
    render = RENDER.read_text(encoding="utf-8")
    assert 'addEventListener("click", handleDynamicAction)' in app
    assert 'addEventListener("change", handleDynamicAction)' in app
    assert '$("#nexusGrid").addEventListener("click", handleDynamicAction)' in app
    for action in DYNAMIC_ACTIONS:
        assert f'case "{action}":' in app
        assert f'"{action}"' in render
        assert not re.search(rf'case "{action}":\s*break;', app)
    assert ".querySelectorAll(" not in render
    assert ".hidden = true" not in render
    assert 'actionButton("打开 N 网", "openNexus"' in render
    assert 'actionButton("复制尾号", "copyNexusId"' in render
    assert 'window.open(url, "_blank", "noopener")' in app
    assert "navigator.clipboard.writeText" in app
    assert "getSelection" in app


def test_modules_are_safe_and_have_required_contracts():
    joined = "\n".join(p.read_text(encoding="utf-8") for p in (APP, API, EFFECTS, RENDER))
    assert "innerHTML" not in joined
    assert "mod-config" not in joined.lower()
    assert "textContent" in RENDER.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    for token in ("AbortController", "timeout", "429", "423", "409", "ApiError", "payload?.error_code"):
        assert token in api
    effects = EFFECTS.read_text(encoding="utf-8")
    for token in ("requestAnimationFrame", "visibilitychange", "prefers-reduced-motion", "devicePixelRatio", "80", "pointerdown", "destroy", "update"):
        assert token in effects
    for exported in ("installRipple", "createPetalEffect", "updatePetalEffect"):
        assert re.search(rf"export function {exported}\b", effects)
    assert "const ripple = installRipple(root)" in effects
    assert "const petals = createPetalEffect(canvas)" in effects


def test_css_has_three_themes_accessibility_effects_and_responsive_rules():
    css = CSS.read_text(encoding="utf-8")
    for token in (
        '[data-theme="aurora-glass"]', '[data-theme="ivory-sakura"]',
        '[data-theme="starlit-night"]', "--background-mask", "--background-blur",
        "--background-position", "--background-url", ":focus-visible", "prefers-contrast: more",
        "prefers-reduced-motion", "@media (max-width: 960px)", ".ripple",
        "pointer-events: none",
    ):
        assert token in css
    html = HTML.read_text(encoding="utf-8").lower()
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "segoe ui variable" in css.lower()
    assert "microsoft yahei ui" in css.lower()


def test_background_upload_and_appearance_persistence_contract():
    app = APP.read_text(encoding="utf-8")
    assert 'accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"' in HTML.read_text(encoding="utf-8")
    assert 'request("/api/appearance/background"' in app
    assert 'request("/api/appearance"' in app
    assert 'request("/api/appearance/background", { method: "DELETE" })' in app
    assert 'background/current?v=${Date.now()}' in app
    assert 'setProperty("--background-url"' in app
    assert "applyAppearance(saved)" in app


def test_modals_and_aria_are_real_accessible_actions():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for action in ("cancelConflict", "replaceConflict", "keepBothConflict", "cancelDelete", "approveDelete"):
        assert f'data-action="{action}"' in html
    assert "autofocus" in html
    assert html.count('aria-label="左上"') == 1
    for label in ("顶部居中", "右上", "左侧居中", "居中", "右侧居中", "左下", "底部居中", "右下"):
        assert f'aria-label="{label}"' in html
    assert 'setAttribute("aria-pressed"' in app
    assert 'addEventListener("cancel"' in app
    assert "force_modified=true" in app
    assert 'importSelected("replace")' in app
    assert 'importSelected("keep_both")' in app
    assert not re.search(r'case "[^"]+":\s*break;', app)


def test_switch_view_awaits_page_loaders_and_handlers_reference_functions():
    app = APP.read_text(encoding="utf-8")
    assert "async function switchView" in app
    for call in ("await loadMods()", "await loadNexus(\"popular\")", "await loadSettings()"):
        assert call in app
    assert "showMods: async () => switchView" in app


def test_all_javascript_modules_pass_node_syntax_check():
    for path in (APP, API, EFFECTS, RENDER):
        result = subprocess.run(
            ["node", "--check", str(path)], cwd=ROOT, text=True,
            capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
