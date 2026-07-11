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
DYNAMIC_ACTIONS = {"toggleMod", "openModFolder", "deleteMod", "rescanMods", "useGamePath", "openNexus", "copyNexusId"}


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
    assert {"backgroundLayer", "petalCanvas", "toastHost", "busyOverlay", "conflictModal", "deleteModal", "modConflictNotice"} <= set(parser.ids)
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
    assert 'error.code === "modified_files"' in app
    assert 'error.code === "mod_conflict"' in app
    cancel = re.search(r"async function cancelImportConflict\(\) \{(.*?)^\}", app, re.S | re.M)
    assert cancel
    assert cancel.group(1).index('await request("/api/mods/import"') < cancel.group(1).index('$("#conflictModal").close()')
    assert 'importSelected("replace")' in app
    assert 'importSelected("keep_both")' in app
    assert not re.search(r'case "[^"]+":\s*break;', app)


def test_local_inputs_are_not_disabled_or_dispatched_through_run():
    app = APP.read_text(encoding="utf-8")
    assert "async function run(trigger, operation, { disable = true" in app
    assert 'trigger.tagName === "BUTTON" || trigger.type === "submit"' in app
    assert "const LOCAL_INPUT_ACTIONS = new Set" in app
    dispatch = re.search(r"async function dispatchStatic\(event\) \{(.*?)^\}", app, re.S | re.M)
    assert dispatch
    body = dispatch.group(1)
    assert body.index("LOCAL_INPUT_ACTIONS.has") < body.index("await run(")
    assert "ACTION_HANDLERS[target.dataset.action](actionEvent);" in body
    input_branch = 'if (target.tagName === "INPUT" || target.tagName === "SELECT")'
    assert input_branch in body
    assert body.index(input_branch) < body.index("await run(")
    assert "await ACTION_HANDLERS[target.dataset.action](actionEvent)" in body
    assert "filterMods" in app and "changeMask" in app and "changeBlur" in app


def test_request_sequences_conflict_feedback_and_nexus_validation():
    app = APP.read_text(encoding="utf-8")
    render = RENDER.read_text(encoding="utf-8")
    assert "modsRequestSequence" in app and "nexusRequestSequence" in app
    assert "sequence !== state.modsRequestSequence" in app
    assert "sequence !== state.nexusRequestSequence" in app
    assert 'error.code === "mod_conflict"' in app
    assert 'renderConflict($("#modConflictNotice"), error.details)' in app
    assert 'await loadMods()' in app
    assert "details?.files" in render and "details?.conflicts" in render
    assert "validatedNexusUrl" in app


def test_nexus_url_validator_executable_contract():
    script = """
      import { validatedNexusUrl } from './frontend/render.js';
      const valid = [
        'https://www.nexusmods.com/palworld/mods/123',
        'https://nexusmods.com/palworld/mods/9/'
      ];
      const invalid = [
        'http://www.nexusmods.com/palworld/mods/123',
        'https://evil.example/palworld/mods/123',
        'https://nexusmods.com/skyrim/mods/123',
        'https://nexusmods.com/palworld/mods/not-a-number',
        'https://nexusmods.com.evil.example/palworld/mods/123'
      ];
      if (!valid.every(value => validatedNexusUrl(value))) process.exit(1);
      if (!invalid.every(value => validatedNexusUrl(value) === null)) process.exit(2);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_resize_is_single_raf_throttled_and_destroy_cancels_pending_resize():
    effects = EFFECTS.read_text(encoding="utf-8")
    assert "let resizeFrame = 0" in effects
    assert "function scheduleResize()" in effects
    assert 'window.addEventListener("resize", scheduleResize)' in effects
    assert "cancelAnimationFrame(resizeFrame)" in effects
    assert 'window.removeEventListener("resize", scheduleResize)' in effects


def test_switch_view_awaits_page_loaders_and_handlers_reference_functions():
    app = APP.read_text(encoding="utf-8")
    assert "async function switchView" in app
    for call in ("await loadMods()", "await loadNexus(\"popular\")", "await loadSettings()"):
        assert call in app
    assert "showMods: async () => switchView" in app


def test_local_mod_cards_cover_audit_states_integrity_and_safe_toggle_contract():
    render = RENDER.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for status in ("enabled", "disabled", "modified", "missing", "conflict"):
        assert f'{status}:' in render
    for label in ("已启用", "已禁用", "文件已修改", "文件缺失", "文件冲突", "完整性正常", "请修复文件或重扫"):
        assert label in render
    assert 'const toggleAllowed = status === "enabled" || status === "disabled"' in render
    assert "toggle.disabled = !toggleAllowed" in render
    assert "mod.manifest_files" in render
    assert "mod.install_path" in render
    assert "mod.mod_type" in render
    assert "mod.nexus_id" in render
    assert "mod.size_bytes" in render
    assert "const previousMods = state.mods" in app
    assert "const updated = await request" in app
    assert "state.mods = state.mods.map" in app
    assert "state.mods = previousMods" in app
    assert "target.disabled = true" in app
    assert "target.disabled = false" in app


def test_mod_filter_stats_import_rollback_and_error_guidance_contract():
    app = APP.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert "filtered.length" in app
    assert "异常" in app
    assert "input" in app and "filterMods" in app
    assert 'accept=".zip,.pak,application/zip,application/octet-stream"' in html
    for stage in ("已选择", "正在上传", "正在识别并安装", "安装成功"):
        assert stage in app
    assert '正在识别并安装：正在上传 ${state.selectedModFile.name}' in app
    assert 'error.status === 410 && error.code === "upload_expired"' in app
    assert "请重新选择文件" in app
    assert 'error.status === 423 && error.code === "game_running"' in app
    assert "请先退出游戏" in app
    assert 'error.status === 403 && error.code === "permission_denied"' in app
    assert "管理员身份重启" in app
    assert 'renderConflict($("#deleteDetails"), error.details)' in app
    assert 'id="deleteDetails"' in html
    assert "isSupportedModFile" in app


def test_dynamic_mod_actions_are_delegated_and_guard_duplicate_submissions():
    app = APP.read_text(encoding="utf-8")
    assert 'case "rescanMods":' in app
    assert '"rescanMods"' in RENDER.read_text(encoding="utf-8")
    assert "if (target.disabled) return" in app
    assert "inFlightDynamicActions" in app
    assert "innerHTML" not in app


def test_all_javascript_modules_pass_node_syntax_check():
    for path in (APP, API, EFFECTS, RENDER):
        result = subprocess.run(
            ["node", "--check", str(path)], cwd=ROOT, text=True,
            capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
