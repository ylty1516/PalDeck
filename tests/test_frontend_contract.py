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
INTERACTION = FRONTEND / "interaction-policy.js"
CSS = FRONTEND / "styles.css"

REQUIRED_ACTIONS = {
    "refreshMods", "openModsFolder", "chooseModFile", "importMod",
    "searchNexus", "refreshNexus", "autoDetectGame", "saveGamePath",
    "repairFolders", "chooseBackground", "resetBackground", "saveAppearance",
    "installUe4ss", "checkUpdate", "restartAdmin",
}
DYNAMIC_ACTIONS = {"toggleMod", "openModFolder", "deleteMod", "rescanMods", "toggleWorkshop", "openWorkshopFolder", "openSteamWorkshop", "useGamePath", "openNexus", "copyNexusId"}


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


def test_html_has_exactly_five_pages_and_required_layers():
    parser = parsed_html()
    assert parser.views == ["view-mods", "view-import", "view-nexus", "view-settings", "view-credits"]
    assert {"backgroundLayer", "petalCanvas", "toastHost", "busyOverlay", "conflictModal", "deleteModal", "modConflictNotice"} <= set(parser.ids)
    assert parser.scripts == [{"type": "module", "src": "app.js"}]


def test_navigation_tracks_current_page_for_initial_and_switched_views():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    parser = parsed_html()
    assert html.count('aria-current="page"') == 1
    assert re.search(r'class="nav-item active"[^>]*data-view="mods"[^>]*aria-current="page"', html)
    switch = re.search(r"async function switchView\(name\) \{(.*?)^\}", app, re.S | re.M)
    assert switch
    assert 'item.setAttribute("aria-current", "page")' in switch.group(1)
    assert 'item.removeAttribute("aria-current")' in switch.group(1)
    assert len(parser.views) == 5


def test_credits_view_uses_fixed_ids_real_buttons_and_native_details():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'data-view="credits"' in html
    assert 'id="view-credits"' in html
    assert "开源项目致谢" in html and "开放源代码" in html
    assert '<details class="credits-dependencies' in html
    assert 'id="creditsCore"' in html and 'id="creditsDependencies"' in html
    assert 'request("/api/credits")' in app
    assert 'request("/api/system/open-trusted-link", { method: "POST", body: { id } })' in app
    assert 'case "openTrustedLink":' in app
    assert "source_url" not in app
    loader = re.search(r"async function loadCredits\(\) \{(.*?)^\}", app, re.S | re.M)
    assert loader and "window.open" not in loader.group(1)
    assert ".credits-grid" in css and ".credit-card" in css


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
    joined = "\n".join(p.read_text(encoding="utf-8") for p in (APP, API, EFFECTS, RENDER, INTERACTION))
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
        "prefers-reduced-motion", "@media (max-width: 959px)", ".ripple",
        "pointer-events: none",
    ):
        assert token in css
    html = HTML.read_text(encoding="utf-8").lower()
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "segoe ui variable" in css.lower()
    assert "microsoft yahei ui" in css.lower()


def test_v22_settings_page_has_seven_real_sections_and_live_preview():
    html = HTML.read_text(encoding="utf-8")
    for section in ("game", "update", "ue4ss", "theme", "background", "effects", "advanced"):
        assert f'data-settings-section="{section}"' in html
    assert 'class="appearance-live-preview"' in html
    assert "效果预览" in html
    assert '<details class="advanced-settings' in html


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
    assert cancel.group(1).index('await request("/api/mods/import"') < cancel.group(1).rindex('$("#conflictModal").close()')
    assert 'resolveImportConflict("replace")' in app
    assert 'resolveImportConflict("keep_both")' in app
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
    assert "nexusRequestController" in app
    assert "state.nexusRequestController?.abort()" in app
    assert 'signal: controller.signal' in app
    assert 'error.code === "request_cancelled"' in app
    assert "sequence !== state.modsRequestSequence" in app
    assert "sequence !== state.nexusRequestSequence" in app
    assert 'error.code === "mod_conflict"' in app
    assert 'renderConflict($("#modConflictNotice"), error.details)' in app
    assert 'await loadMods()' in app
    assert "details?.files" in render and "details?.conflicts" in render
    assert "validatedNexusUrl" in app


def test_nexus_catalog_ui_contract_has_sources_tabs_safe_images_and_skips_adult_content():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    render = RENDER.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for action in ("downloadsNexus", "endorsementsNexus", "latestNexus", "refreshNexus"):
        assert f'data-action="{action}"' in html
    assert "result.source" in app and "result.fetched_at" in app and "result.warning" in app
    assert "force=1" in app
    assert "if (mod.adultContent !== false) continue;" in render
    for forbidden in ("adultCardData", "revealAdult", "显示成人内容"):
        assert forbidden not in render and forbidden not in app
    assert 'addEventListener("error"' in render
    assert "/^https:\\/\\//i" in render
    assert "下载" in render and "推荐" in render and "版本" in render and "作者" in render
    assert ".adult-hidden" not in css and ".adult-label" not in css
    assert ".adult-hidden .nexus-picture" not in css
    assert "下载模组" not in render
    readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
    assert "Nexus 成人内容会被彻底过滤" in readme
    assert "显示成人内容" not in readme


def test_v22_nexus_page_has_read_only_layout_and_no_fake_actions():
    html = HTML.read_text(encoding="utf-8")
    render = RENDER.read_text(encoding="utf-8")
    assert 'class="nexus-catalog-layout"' in html
    assert 'class="nexus-side-rail"' in html
    assert "匿名只读" in html and "成人内容已过滤" in html
    assert "打开 N 网" in render and "复制尾号" in render
    assert "mod.adultContent !== false" in render
    for forbidden in ("加入收藏", "一键安装", "编辑精选", "Nexus 登录"):
        assert forbidden not in html + render


def test_nexus_url_validator_executable_contract():
    script = """
      import { validatedNexusUrl } from './frontend/render.js';
      const valid = [
        'https://www.nexusmods.com/palworld/mods/123'
      ];
      const invalid = [
        'http://www.nexusmods.com/palworld/mods/123',
        'https://nexusmods.com/palworld/mods/9',
        'https://cdn.nexusmods.com/palworld/mods/123',
        'https://evil.example/palworld/mods/123',
        'https://www.nexusmods.com/skyrim/mods/123',
        'https://www.nexusmods.com/palworld/mods/not-a-number',
        'https://www.nexusmods.com/palworld/mods/123/',
        'https://www.nexusmods.com/palworld/mods/123?tracking=1',
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


def test_three_petal_styles_share_engine_lifecycle_and_old_ellipse_is_gone():
    effects = EFFECTS.read_text(encoding="utf-8")
    engine = (FRONTEND / "petal-engine.js").read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'from "./petal-engine.js"' in effects
    assert "createParticles" in effects and "stepParticles" in effects
    for renderer in ("renderNaturalPetal", "renderWatercolorPetal", "renderMinimalPetal"):
        assert f"export function {renderer}" in effects
    assert "context.ellipse" not in effects
    assert 'fillStyle = "rgba(255, 190, 210, .72)"' not in effects
    assert "Math.min(window.devicePixelRatio || 1, 2)" in effects
    assert "document.hidden" in effects
    assert "previous = performance.now()" in effects
    assert "clearRect" in effects
    assert "createWatercolorSpriteSet" in effects
    assert "naturalPalette" in effects
    assert "createNaturalSpriteAtlas" in effects
    assert "NATURAL_GRADIENT_CACHE" not in effects
    assert "quadraticCurveTo" in effects
    assert "watercolorSpriteKind" in effects
    assert 'spriteIndex === 0 ? "bloom" : "petal"' in effects
    assert "Math.abs(index) % sprites.length" not in effects
    assert "createPetalUpdateCache" in effects
    assert "minimalSafeX" not in effects
    assert "lane" in engine
    assert "querySelector" not in engine
    assert "effects.update({ level:" in app
    assert "petal_style" in app
    for style in ("natural", "watercolor", "minimal"):
        assert f'data-petal-style="{style}"' in html
    assert html.count('class="petal-style-card"') == 3
    assert html.count('aria-pressed="false"') >= 3
    assert ".petal-style-card" in css and ".petal-preview" in css


def test_petal_style_preview_persists_only_on_save_and_recovers_after_failure():
    app = APP.read_text(encoding="utf-8")
    assert "function choosePetalStyle" in app
    assert "previewAppearance({ petal_style:" in app
    save = re.search(r"saveAppearance: async \(\) => \{(.*?)\},$", app, re.S | re.M)
    assert save
    assert "petal_style" in save.group(1)
    assert 'request("/api/appearance", { method: "POST"' in save.group(1)
    assert 'request("/api/appearance")' in save.group(1)
    assert "applyAppearance(recovered)" in save.group(1)
    assert "appearanceRevisions.capture()" in save.group(1)
    assert "appearanceRevisions.apply" in save.group(1)
    assert "previewAppearance" in app
    assert "const backgroundWriteQueue = createSerialQueue()" in app
    assert app.count("backgroundWriteQueue.enqueue") == 2
    completion = re.search(r"function completeBackgroundWrite\([^)]*\) \{(.*?)^\}", app, re.S | re.M)
    assert completion
    assert completion.group(1).index("appearanceRevisions.apply") < completion.group(1).index("refreshBackground()")
    assert completion.group(1).index("refreshBackground()") < completion.group(1).index("toast(")
    for action in ("resetBackground", "selectBackground"):
        entry = re.search(rf"^  {action}: (.*?)(?=^  [A-Za-z]|^\}}\);)", app, re.S | re.M)
        assert entry and "appearanceRevisions.bump()" in entry.group(1)
        assert "completeBackgroundWrite" in entry.group(1)


def test_switch_view_awaits_page_loaders_and_handlers_reference_functions():
    app = APP.read_text(encoding="utf-8")
    assert "async function switchView" in app
    for call in ("await loadMods()", "await loadNexus(state.nexusMode, true)", "await loadSettings()"):
        assert call in app
    assert "showMods: async () => switchView" in app


def test_local_mod_cards_cover_audit_states_integrity_and_safe_toggle_contract():
    render = RENDER.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for status in ("enabled", "disabled", "modified", "missing", "conflict"):
        assert f'{status}:' in render
    for label in ("已启用", "已禁用", "文件已修改", "文件缺失", "文件冲突", "完整性正常", "请修复文件或重扫"):
        assert label in render
    assert '["enabled", "disabled"].includes(status)' in render
    assert "toggle.disabled = !toggleAllowed" in render
    assert "mod.manifest_files" in render
    assert "mod.install_path" in render
    assert "mod.mod_type" in render
    assert "mod.nexus_id" in render
    assert "mod.size_bytes" in render
    assert "previousMods" not in app
    toggle = re.search(r"async function toggleMod\(target, id\) \{(.*?)^\}", app, re.S | re.M)
    assert toggle
    assert "beginModsWrite()" in toggle.group(1)
    assert toggle.group(1).count("await loadMods()") >= 2
    assert toggle.group(1).index("await loadMods()") < toggle.group(1).index('error.code === "mod_conflict"')
    assert "target.disabled = true" in app
    assert "target.disabled = false" in app


def test_mod_filter_stats_import_rollback_and_error_guidance_contract():
    app = APP.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert "view.items.length" in app
    assert "deriveModView" in app
    assert "异常" in html
    assert "input" in app and "filterMods" in app
    assert 'accept=".zip,.pak,application/zip,application/octet-stream"' in html
    for stage in ("已选择", "正在上传", "正在识别并安装", "安装成功"):
        assert stage in app
    assert '正在识别并安装：正在上传 ${state.selectedModFile.name}' in app
    assert "executeModFileSelection" in app
    assert 'error.status === 410 && error.code === "upload_expired"' in app
    assert "请重新选择文件" in INTERACTION.read_text(encoding="utf-8")
    assert "请先退出游戏" in INTERACTION.read_text(encoding="utf-8")
    assert "管理员身份重启" in INTERACTION.read_text(encoding="utf-8")
    assert 'renderConflict($("#deleteDetails"), error.details)' in app
    assert 'id="deleteDetails"' in html
    assert "isSupportedModFile" in app


def test_conflict_decisions_have_real_requests_timeout_and_visible_result_feedback():
    app = APP.read_text(encoding="utf-8")
    resolver = re.search(r"async function resolveImportConflict\(decision\) \{(.*?)^\}", app, re.S | re.M)
    assert resolver
    body = resolver.group(1)
    assert body.index("modal.close()") < body.index("await importSelected(decision)")
    assert "正在替换冲突文件" in body and "正在保留两份并安装" in body
    assert "冲突处理完成" in body and "conflict-inline-error" in body
    assert "处理失败" in body and "openModal(modal)" in body
    retry_branch = re.search(r"if \(retryToken\) \{(.*?)\n\s*\} else if", app, re.S)
    assert retry_branch and "timeout: 120000" in retry_branch.group(1)
    assert 'resolveImportConflict("replace")' in app
    assert 'resolveImportConflict("keep_both")' in app


def test_dynamic_mod_actions_are_delegated_and_guard_duplicate_submissions():
    app = APP.read_text(encoding="utf-8")
    assert 'case "rescanMods":' in app
    assert '"rescanMods"' in RENDER.read_text(encoding="utf-8")
    assert "if (target.disabled) return" in app
    assert "inFlightDynamicActions" in app
    assert 'dynamicActionKey(target.dataset.dynamicAction, id)' in app
    assert "innerHTML" not in app


def test_all_async_file_branches_use_shared_actionable_error_mapping():
    app = APP.read_text(encoding="utf-8")
    assert "toast(error.message" not in app
    executor = re.search(r"async function executeFileOperation\(operation\) \{(.*?)^\}", app, re.S | re.M)
    assert executor
    assert "actionableErrorMessage(error)" in executor.group(1)
    handlers = re.search(r"export const ACTION_HANDLERS.*?Object\.freeze\(\{(.*?)^\}\);", app, re.S | re.M)
    assert handlers
    for action in ("selectModFile", "selectUe4ssZip", "selectBackground"):
        entry = re.search(rf"^  {action}: (.*?)(?=^  [A-Za-z]|\Z)", handlers.group(1), re.S | re.M)
        assert entry, action
        assert "executeFileOperation" in entry.group(1) or "executeModFileSelection" in entry.group(1)
    dropzone = re.search(r"function setupDropzone\(\) \{(.*?)^\}", app, re.S | re.M)
    assert dropzone
    assert "executeModFileSelection" in dropzone.group(1)


def test_interaction_policy_executable_contract():
    script = """
      import {
        actionableErrorMessage, dynamicActionKey, nextModsGeneration,
        pendingUploadTokenAfterError, resetModFileSelectionState
      } from './frontend/interaction-policy.js';
      if (dynamicActionKey('rescanMods', 'card-a') !== 'rescan-mods') process.exit(1);
      if (dynamicActionKey('rescanMods', 'card-b') !== 'rescan-mods') process.exit(2);
      if (dynamicActionKey('toggleMod', 'card-a') !== 'toggleMod:card-a') process.exit(3);
      const cases = [
        [{ status: 403, code: 'permission_denied' }, '管理员身份重启'],
        [{ status: 423, code: 'game_running' }, '退出游戏'],
        [{ status: 410, code: 'upload_expired' }, '重新选择文件'],
      ];
      if (!cases.every(([error, expected]) => actionableErrorMessage(error).includes(expected))) process.exit(4);
      const replacement = { status: 409, code: 'mod_conflict', details: { upload_token: 'new-token' } };
      if (pendingUploadTokenAfterError('old-token', replacement) !== 'new-token') process.exit(5);
      for (const error of cases.map(([value]) => value)) {
        if (pendingUploadTokenAfterError('old-token', error) !== null) process.exit(6);
      }
      const selected = { pendingUploadToken: 'token', selectedModFile: { name: 'old.zip' }, other: 1 };
      const reset = resetModFileSelectionState(selected);
      if (reset.pendingUploadToken !== null || reset.selectedModFile !== null || reset.other !== 1) process.exit(7);
      if (nextModsGeneration(4) !== 5) process.exit(8);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_import_lifecycle_and_mod_generation_contract():
    app = APP.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    assert "function resetModFileSelection()" in app
    reset = re.search(r"function resetModFileSelection\(\) \{(.*?)^\}", app, re.S | re.M)
    assert reset
    for token in ('state.pendingUploadToken = reset.pendingUploadToken', 'state.selectedModFile = reset.selectedModFile', '$("#modFileInput").value = ""', '$("#importResult").textContent = ""'):
        assert token in reset.group(1)
    select = re.search(r"function selectModFiles\(files\) \{(.*?)^\}", app, re.S | re.M)
    assert select and "isSupportedModFile" in select.group(1) and "createImportQueue" in select.group(1)
    assert "pendingUploadTokenAfterError" in app
    assert "selectionToken" in app and "transitionActiveImport" in app and "processImportQueue" in app
    assert "nextModsGeneration" in app
    assert "modsRequestController.abort()" in app
    assert 'request("/api/mods", { signal: controller.signal })' in app
    assert "generation !== state.modsRequestGeneration" in app
    for operation in ("importSelected", "toggleMod", "deletePendingMod"):
        body = re.search(rf"async function {operation}\([^)]*\) \{{(.*?)^\}}", app, re.S | re.M)
        assert body and "beginModsWrite()" in body.group(1), operation
    assert 'case "rescanMods": beginModsWrite();' in app
    assert "externalSignal" in api


def test_ue4ss_card_uses_fixed_palworld_sources_and_real_actions():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for label in ("Palworld 专用", "Okaetsu", "安装内置", "检查 GitHub", "本地 ZIP"):
        assert label in html
    for endpoint in (
        "/api/ue4ss/install-bundled", "/api/ue4ss/check-upstream",
        "/api/ue4ss/install-upstream",
    ):
        assert endpoint in app
    assert "/api/ue4ss/install-latest" not in app
    assert "在线安装 UE4SS" not in html
    assert "ue4ssUpdatedAt" in html and "ue4ssDigest" in html
    assert 'update_available' in app
    assert 'confirm_replace: true' in app
    assert 'error.code === "ue4ss_conflict"' in app
    assert 'hidden = !state.ue4ssUpdateAvailable' in app
    assert "UE4SS_WRITE_ACTIONS" in app
    for action in ("installUe4ss", "installUe4ssUpdate", "selectUe4ssZip"):
        assert f'"{action}"' in app
    assert 'endpoint === "/api/ue4ss/install-upstream"' in app
    assert "state.ue4ssUpdateAvailable = false" in app
    assert '$("#installUe4ssUpdate").hidden = true' in app
    assert ".ue4ss-card" in css
    for forbidden in ("browser_download_url", "download_url", "ue4ssUrl", "asset_url"):
        assert forbidden not in app


def test_workshop_cards_have_source_metadata_real_actions_and_no_delete():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    render = RENDER.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for token in ("Steam Workshop", "Workshop ID", "author", "version", "install_types", "dependencies"):
        assert token in render
    for action in ("toggleWorkshop", "openWorkshopFolder", "openSteamWorkshop"):
        assert f'"{action}"' in render
        assert f'case "{action}":' in app
    assert 'const workshop = mod.source === "steam_workshop"' in render
    workshop_branch = re.search(r'if \(workshop\) \{\s+const toggle = actionButton\(mod\.enabled.*?\n\s{4}\} else \{', render, re.S)
    assert workshop_branch
    assert '"deleteMod"' not in workshop_branch.group(0)
    assert "steamcommunity.com/sharedfiles" not in app
    assert "steamcommunity.com/sharedfiles" not in render
    assert "validatedSteamWorkshopUrl" not in app
    assert "validatedSteamWorkshopUrl" not in render
    assert 'request(`/api/workshop/${encodeURIComponent(id)}/open-folder`)' in app
    assert 'request(`/api/workshop/${encodeURIComponent(id)}/open-page`, { method: "POST", body: {} })' in app
    assert 'request(`/api/workshop/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`' in app
    assert 'id="workshopDependencyModal"' in html
    assert 'data-action="cancelWorkshopDependency"' in html
    assert 'data-action="approveWorkshopDependency"' in html
    assert "confirm_dependents: true" in app
    assert "workshop_dependency_conflict" in app
    assert "replaceWorkshopMods" in app
    assert "authoritative.mods" in app
    assert "authoritative.cleanup_pending" in app
    assert "检测到无法安全自动清理的 Workshop 事务文件" in app
    for field in ("global_enabled", "deployed", "needs_restart"):
        assert f"mod.{field}" in render
    for label in ("全局开关", "已部署", "下次启动生效"):
        assert label in render
    assert "下次启动生效" in app
    updater = re.search(r"function replaceWorkshopMods\(authoritative\) \{(.*?)^\}", app, re.S | re.M)
    assert updater
    assert "authoritative.map" in updater.group(1)
    assert 'mod.source !== "steam_workshop"' in updater.group(1)
    assert "state.mods = [...localMods, ...workshopMods]" in updater.group(1)
    assert ".source-workshop" in css


def test_workshop_writes_are_globally_serialized_and_dependency_escape_clears_pending():
    app = APP.read_text(encoding="utf-8")
    assert "let workshopWriteQueue = Promise.resolve()" in app
    queue = re.search(r"function queueWorkshopWrite\(operation\) \{(.*?)^\}", app, re.S | re.M)
    assert queue
    assert "workshopWriteQueue.then(operation, operation)" in queue.group(1)
    assert 'case "toggleWorkshop": await run(target, () => queueWorkshopWrite(' in app
    assert '$("#workshopDependencyModal").addEventListener("cancel"' in app
    cancel_handler = re.search(r'\$\("#workshopDependencyModal"\)\.addEventListener\("cancel", \(event\) => \{(.*?)^\s{2}\}\);', app, re.S | re.M)
    assert cancel_handler
    assert "cancelWorkshopDependency()" in cancel_handler.group(1)
    assert "event.preventDefault()" in cancel_handler.group(1)
    cancel_function = re.search(r"function cancelWorkshopDependency\(\) \{(.*?)^\}", app, re.S | re.M)
    assert cancel_function and "state.pendingWorkshopDependency = null" in cancel_function.group(1)


def test_workshop_filter_includes_server_metadata_fields():
    model = FRONTEND.joinpath("ui-model.js").read_text(encoding="utf-8")
    for field in ("author", "install_types", "workshop_id"):
        assert f"mod.{field}" in model


def test_v22_mod_page_has_real_filters_stats_and_unified_list_header():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    render = RENDER.read_text(encoding="utf-8")
    for identifier in ("modSourceFilter", "modStatusFilter", "statTotal", "statEnabled", "statDisabled", "statAbnormal"):
        assert f'id="{identifier}"' in html
    assert 'data-action="filterModSource"' in html
    assert 'data-action="filterModStatus"' in html
    assert "deriveModView" in app and "renderModStats" in app
    assert "mod-list-header" in html
    assert "mod-row" in render


def test_v22_shell_has_brand_sidebar_workspace_statusbar_and_three_breakpoints():
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for marker in ('class="brand-logo"', 'class="sidebar glass-panel"', 'class="workspace"', 'id="appStatusbar"', 'id="statusGamePath"'):
        assert marker in html
    for width in (1440, 1100, 960):
        assert str(width) in css
    assert "overflow-x: hidden" in css
    assert "--statusbar-height" in css
    assert "updateShellStatus" in app
    assert 'data-action="showSettingsStatus"' in html


def test_v22_custom_window_chrome_is_accessible_and_has_native_frame_fallback():
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    backend = ROOT.joinpath("backend/app.py").read_text(encoding="utf-8")
    for action, label in (("windowMinimize", "最小化"), ("windowMaximize", "最大化或还原"), ("windowClose", "关闭")):
        assert f'data-action="{action}"' in html
        assert f'aria-label="{label}"' in html
        assert f"{action}:" in app
    assert "pywebview-drag-region" in html
    assert ".window-chrome[hidden]" in css
    assert 'os.environ.get("PALDECK_NATIVE_FRAME"' in backend
    assert "frameless=not use_native_frame" in backend
    assert "easy_drag=False" in backend
    assert "js_api=bridge" in backend
    assert '&chrome={0 if use_native_frame else 1}' in backend
    controls = FRONTEND.joinpath("window-controls.js").read_text(encoding="utf-8")
    assert "requestedChrome" in controls and "waitForOperation" in controls


def test_all_javascript_modules_pass_node_syntax_check():
    for path in sorted(FRONTEND.glob("*.js")):
        result = subprocess.run(
            ["node", "--check", str(path)], cwd=ROOT, text=True,
            capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
