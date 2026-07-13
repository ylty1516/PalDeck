# PalDeck v2.2 概念图高保真界面实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在保留 PalDeck 全部真实 Mod、Workshop、UE4SS、安全事务和 Nexus 匿名只读能力的前提下，将五个页面重构为概念图所示的高保真蓝白玻璃界面，并发布 v2.2.0。

**架构：** 保持 Flask + pywebview + 原生 ES Modules。前端新增纯 UI 派生模型、固定 SVG 图标和桌面窗口桥接模块；后端只新增最小桌面桥接与受控本地文件选择授权，不改变既有 Mod 磁盘语义。视觉验收使用测试专用 fixture server 和本机 Edge headless 生成三档截图，不把 fixture 或概念图打进发布包。

**技术栈：** Python 3.13、Flask、pywebview 6.2.1、原生 HTML/CSS/JavaScript ES Modules、Node test runner、pytest、PowerShell 5.1、Microsoft Edge headless、PyInstaller。

---

## 文件结构

### 新建文件

- `frontend/ui-model.js`：筛选、统计、状态和导入队列的纯函数。
- `frontend/icons.js`：固定本地线性 SVG 图标工厂，不接受任意 SVG/path。
- `frontend/window-controls.js`：等待 `pywebviewready` 并调用最小桌面桥。
- `frontend/a11y.js`：模态框焦点返回、可访问状态和 tooltip 辅助。
- `backend/desktop_bridge.py`：窗口最小化/最大化/关闭与原生文件夹选择的白名单桥。
- `backend/import_selection.py`：顶层 ZIP/PAK 安全枚举和短时选择授权。
- `tests/js/ui-model.test.mjs`：纯 UI 派生与队列测试。
- `tests/js/icons.test.mjs`：固定 SVG 安全测试。
- `tests/js/window-controls.test.mjs`：桥缺失、就绪和动作测试。
- `tests/js/a11y.test.mjs`：焦点与 ARIA 辅助测试。
- `tests/test_desktop_bridge.py`：桌面桥白名单与窗口状态测试。
- `tests/test_import_selection.py`：目录枚举、reparse 和授权测试。
- `tests/test_visual_fixture_contract.py`：视觉 fixture 不进入生产、无成人数据/假动作的契约。
- `tests/visual/fixture_server.py`：只供截图的静态服务器与固定 API 数据。
- `tests/visual/fixtures/*.json`：五页真实形状、虚构身份已移除的固定数据。
- `scripts/capture_ui.ps1`：用本机 Edge headless 生成三档五页截图。
- `scripts/compare_ui_screenshots.py`：尺寸、空白边界和已批准基线差异检查。
- `tests/visual/baselines/*.png`：人工批准后的 15 张 v2.2 回归基线。
- `docs/verification/2026-07-13-paldeck-v2.2-release.md`：正式发布证据。

### 主要修改文件

- `frontend/index.html`：三段式外壳、五页语义骨架、底栏、窗口控件、折叠区。
- `frontend/styles.css`：设计令牌、玻璃组件、三档响应式布局、三主题和可访问降级。
- `frontend/app.js`：页面编排、筛选、真实统计、导入队列、加载/错误状态。
- `frontend/render.js`：安全渲染本地 Mod、Workshop、Nexus、致谢和导入项。
- `backend/app.py`：绑定桌面桥和选择授权，保持现有 HTTP 鉴权与业务 API。
- `backend/credits.py`：补充真实 PalDeck 项目/反馈链接和群体致谢。
- `backend/version.py`：版本升级为 `2.2.0`。
- `backend/smoke_check.py`、`scripts/smoke_portable.ps1`：v2.2 Frozen 契约。
- `scripts/build_portable.ps1`、`README.md`：新版本和发布说明。
- `tests/test_frontend_contract.py`、`tests/test_api.py`、`tests/test_credits.py`、`tests/test_release_script_contract.py`、`tests/test_smoke_check.py`：对应回归契约。

## 执行约束

- 每个任务严格执行红—绿—重构；未看到预期失败前不改生产代码。
- 每个任务只有一个写代理；审查代理只读。
- 不把五张概念图、视觉 fixture、截图临时服务或浏览器缓存打进便携包。
- 不增加 Nexus 登录、收藏、下载、一键安装或成人内容开关。
- 不用 `innerHTML` 渲染后端、文件名或 Nexus 内容。
- 每个任务完成后运行 `git diff --check` 并提交；下一任务从干净工作区开始。

---

### 任务 1：建立纯 UI 模型与视觉截图夹具

**文件：**
- 创建：`frontend/ui-model.js`
- 创建：`tests/js/ui-model.test.mjs`
- 创建：`tests/visual/fixture_server.py`
- 创建：`tests/visual/fixtures/mods.json`
- 创建：`tests/visual/fixtures/import.json`
- 创建：`tests/visual/fixtures/nexus.json`
- 创建：`tests/visual/fixtures/settings.json`
- 创建：`tests/visual/fixtures/credits.json`
- 创建：`scripts/capture_ui.ps1`
- 创建：`scripts/compare_ui_screenshots.py`
- 创建：`tests/test_visual_fixture_contract.py`
- 修改：`package.json`

- [ ] **步骤 1：编写 UI 模型失败测试**

在 `tests/js/ui-model.test.mjs` 写明真实行为：

```js
import test from "node:test";
import assert from "node:assert/strict";
import { deriveModView, createImportQueue, reduceImportQueue } from "../../frontend/ui-model.js";

test("模组筛选和统计只使用真实状态", () => {
  const mods = [
    { id: "1", name: "Alpha", source: "local", status: "enabled", mod_type: "pak" },
    { workshop_id: "2", name: "Beta", source: "steam_workshop", enabled: false, valid: true },
    { id: "3", name: "Broken", source: "local", status: "conflict" },
  ];
  const result = deriveModView(mods, { query: "", source: "all", status: "all" });
  assert.deepEqual(result.stats, { total: 3, enabled: 1, disabled: 1, abnormal: 1 });
  assert.deepEqual(result.items.map(item => item.name), ["Alpha", "Beta", "Broken"]);
});

test("导入队列严格串行并在冲突处暂停", () => {
  let queue = createImportQueue([{ key: "a", name: "A.zip" }, { key: "b", name: "B.pak" }]);
  queue = reduceImportQueue(queue, { type: "start", key: "a" });
  queue = reduceImportQueue(queue, { type: "conflict", key: "a", token: "fixed-token" });
  assert.equal(queue.activeKey, "a");
  assert.equal(queue.items[1].state, "queued");
  assert.equal(queue.paused, true);
});
```

- [ ] **步骤 2：运行测试并确认红灯**

运行：

```bash
npm test -- tests/js/ui-model.test.mjs
```

预期：FAIL，提示无法找到 `frontend/ui-model.js`。

- [ ] **步骤 3：实现最小纯函数**

`frontend/ui-model.js` 至少导出：

```js
export function normalizedModState(mod) {
  if (mod.source === "steam_workshop") {
    if (mod.valid === false) return "abnormal";
    return mod.enabled === true ? "enabled" : "disabled";
  }
  return ["enabled", "disabled"].includes(mod.status) ? mod.status : "abnormal";
}

export function deriveModView(mods, filters) {
  const source = Array.isArray(mods) ? mods : [];
  const states = source.map(mod => ({ mod, state: normalizedModState(mod) }));
  const stats = {
    total: source.length,
    enabled: states.filter(item => item.state === "enabled").length,
    disabled: states.filter(item => item.state === "disabled").length,
    abnormal: states.filter(item => item.state === "abnormal").length,
  };
  const query = String(filters.query || "").trim().toLocaleLowerCase();
  const items = states.filter(({ mod, state }) => {
    const sourceMatch = filters.source === "all" || mod.source === filters.source;
    const statusMatch = filters.status === "all" || state === filters.status;
    const haystack = [mod.name, mod.mod_type, mod.nexus_id, mod.workshop_id, mod.author].join(" ").toLocaleLowerCase();
    return sourceMatch && statusMatch && (!query || haystack.includes(query));
  }).map(item => item.mod);
  return { items, stats };
}
```

导入 reducer 只接受固定事件 `start/conflict/succeed/fail/retry/cancel`，任何未知事件抛 `TypeError`，且同一时刻最多一个 `installing/conflict` 项。

- [ ] **步骤 4：建立测试专用 fixture server**

`tests/visual/fixture_server.py` 必须：

- 仅绑定 `127.0.0.1` 随机端口。
- 从 `frontend/` 提供真实静态文件。
- 从 `tests/visual/fixtures/` 返回固定 `/api/*` JSON。
- 在返回 `index.html` 时注入测试脚本，点击 `?view=mods|import|nexus|settings|credits` 对应导航；生产文件不读取 fixture 全局变量。
- Nexus fixture 只含 `adultContent: false`，且不含收藏、一键安装或下载 URL。

关键注入逻辑：

```python
injected = """
<script>
window.addEventListener('load', () => setTimeout(() => {
  const view = new URL(location.href).searchParams.get('view') || 'mods';
  document.querySelector(`[data-view="${view}"]`)?.click();
  document.documentElement.dataset.visualReady = 'true';
}, 250));
</script>
"""
html = html.replace("</body>", injected + "</body>")
```

- [ ] **步骤 5：编写截图和比较脚本契约测试**

`tests/test_visual_fixture_contract.py` 检查：

```python
def test_visual_fixtures_are_test_only_and_safe():
    build = (ROOT / "scripts/build_portable.ps1").read_text(encoding="utf-8-sig")
    assert "tests/visual" not in build
    for path in (ROOT / "tests/visual/fixtures").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert '"adultContent": true' not in text
        assert "一键安装" not in text and "加入收藏" not in text
```

`capture_ui.ps1` 按五页和 `1600x1000/1280x820/960x640` 调用本机 Edge headless：

```powershell
& $edge --headless=new --disable-gpu --hide-scrollbars --run-all-compositor-stages-before-draw `
  --virtual-time-budget=3000 "--window-size=$width,$height" "--screenshot=$output" "$baseUrl/?view=$view"
if ($LASTEXITCODE -ne 0) { throw "Edge screenshot failed: $view $size" }
```

`compare_ui_screenshots.py` 使用已锁定 Pillow，验证像素尺寸、非空内容边界和与批准基线的归一化差异；没有基线时明确返回“需要人工批准”，不得自动把当前截图当作通过。

- [ ] **步骤 6：运行任务测试并提交**

运行：

```bash
npm test
./.venv-build/Scripts/python.exe -m pytest tests/test_visual_fixture_contract.py -q
git diff --check
git add frontend/ui-model.js tests/js/ui-model.test.mjs tests/visual scripts/capture_ui.ps1 scripts/compare_ui_screenshots.py tests/test_visual_fixture_contract.py package.json
git commit -m "test: 建立 v2.2 UI 模型与视觉夹具"
```

预期：Node 和 pytest 全部 PASS，工作区干净。

---

### 任务 2：实现固定 SVG 图标和响应式三段式外壳

**文件：**
- 创建：`frontend/icons.js`
- 创建：`tests/js/icons.test.mjs`
- 修改：`frontend/index.html`
- 修改：`frontend/styles.css`
- 修改：`frontend/app.js`
- 修改：`tests/test_frontend_contract.py`

- [ ] **步骤 1：为固定图标和外壳写失败测试**

`tests/js/icons.test.mjs`：

```js
import test from "node:test";
import assert from "node:assert/strict";
import { icon, iconDefinition, ICON_NAMES } from "../../frontend/icons.js";

test("只创建固定本地 SVG 图标", () => {
  assert.ok(ICON_NAMES.includes("mods"));
  assert.equal(icon("unknown"), null);
  const definition = iconDefinition("mods");
  assert.equal(definition.viewBox, "0 0 24 24");
  assert.ok(definition.paths.length > 0);
  assert.equal(iconDefinition("unknown"), null);
});
```

在 `tests/test_frontend_contract.py` 增加：

```python
def test_v22_shell_has_sidebar_workspace_bottom_status_and_breakpoints():
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'class="app-shell"' in html
    assert 'class="sidebar glass-panel"' in html
    assert 'id="appStatusbar"' in html
    assert 'id="statusGamePath"' in html
    for width in (1440, 1100, 960):
        assert str(width) in css
    assert "overflow-x: hidden" in css
```

- [ ] **步骤 2：运行并确认红灯**

```bash
npm test -- tests/js/icons.test.mjs
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py -q -k "v22_shell"
```

预期：缺少 `icons.js`、底栏和新断点而失败。

- [ ] **步骤 3：实现安全图标工厂**

`frontend/icons.js` 固定图标表只保存受审计 path：

```js
const NS = "http://www.w3.org/2000/svg";
const PATHS = Object.freeze({
  mods: ["M4 6l8-4 8 4v12l-8 4-8-4z", "M4 6l8 4 8-4M12 10v12"],
  import: ["M12 3v12", "M7 10l5 5 5-5", "M4 19h16"],
  nexus: ["M12 3a9 9 0 1 0 9 9", "M12 7v5l4 2"],
  settings: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z"],
  credits: ["M12 21s-8-4.7-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 10c0 6.3-8 11-8 11z"],
});
export const ICON_NAMES = Object.freeze(Object.keys(PATHS));
export function iconDefinition(name) {
  if (!Object.hasOwn(PATHS, name)) return null;
  return Object.freeze({ viewBox: "0 0 24 24", paths: PATHS[name] });
}
export function icon(name, label = "", documentRef = globalThis.document) {
  const definition = iconDefinition(name);
  if (!definition || !documentRef) return null;
  const svg = documentRef.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", definition.viewBox);
  svg.setAttribute("aria-hidden", label ? "false" : "true");
  if (label) svg.setAttribute("aria-label", label);
  for (const d of definition.paths) {
    const path = documentRef.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
```

禁止导出接受任意 `d`、SVG 字符串或 URL 的函数。

- [ ] **步骤 4：重构 HTML 外壳**

保持五个 `view-*` ID 和已存在 `data-action`，新增：

- `<img src="/assets/app.png">` 品牌图。
- 带固定图标槽的五项导航。
- 主内容滚动容器。
- `#appStatusbar`：`#statusService`、`#statusGamePath`、配置入口。
- 侧栏背景入口和版本入口；不重复底栏的打开目录按钮。

`app.js` 中 `showPathInfo()` 同时更新设置输入和底栏：

```js
function updateShellStatus({ version, path, healthy }) {
  $("#statusService").textContent = healthy ? `运行中 · v${version}` : "服务异常";
  $("#statusGamePath").textContent = path || "未配置游戏目录";
}
```

- [ ] **步骤 5：实现三档 CSS**

`styles.css` 采用：

```css
.app-shell {
  display: grid;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr) var(--statusbar-height);
  overflow: hidden;
}
.sidebar { grid-row: 1 / -1; }
.workspace { min-width: 0; min-height: 0; overflow: hidden; }
.workspace-scroll { min-width: 0; min-height: 0; overflow: auto; overflow-x: hidden; }
.app-statusbar { grid-column: 2; }
@media (min-width: 1440px) { :root { --sidebar-width: 300px; --statusbar-height: 68px; } }
@media (min-width: 1100px) and (max-width: 1439px) { :root { --sidebar-width: 236px; --statusbar-height: 62px; } }
@media (min-width: 960px) and (max-width: 1099px) { :root { --sidebar-width: 204px; --statusbar-height: 58px; } }
```

长列表行不能单独使用 `backdrop-filter`；只允许 `.sidebar`、`.app-statusbar` 和页面级 `.glass-panel` 使用。

- [ ] **步骤 6：验证、截图和提交**

```bash
npm test
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py -q
node --check frontend/icons.js
node --check frontend/app.js
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1
./.venv-build/Scripts/python.exe scripts/compare_ui_screenshots.py --allow-missing-baseline
git diff --check
git add frontend tests/js/icons.test.mjs tests/test_frontend_contract.py
git commit -m "feat: 重构响应式应用外壳与图标"
```

预期：三档共 15 张截图尺寸正确；比较器明确标记“基线待批准”而非假 PASS。

---

### 任务 3：接入安全的无边框窗口与桌面桥

**文件：**
- 创建：`backend/desktop_bridge.py`
- 创建：`frontend/window-controls.js`
- 创建：`tests/test_desktop_bridge.py`
- 创建：`tests/js/window-controls.test.mjs`
- 修改：`backend/app.py`
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/styles.css`
- 修改：`tests/test_frontend_contract.py`

- [ ] **步骤 1：编写桌面桥失败测试**

`tests/test_desktop_bridge.py` 使用假窗口，不启动 WebView：

```python
class FakeWindow:
    def __init__(self): self.calls = []
    def minimize(self): self.calls.append("minimize")
    def maximize(self): self.calls.append("maximize")
    def restore(self): self.calls.append("restore")
    def destroy(self): self.calls.append("destroy")

def test_bridge_exposes_only_window_whitelist():
    bridge = DesktopBridge()
    window = FakeWindow()
    bridge.bind(window)
    assert bridge.minimize() == {"state": "minimized"}
    assert bridge.toggle_maximize() == {"state": "maximized"}
    assert bridge.toggle_maximize() == {"state": "normal"}
    assert bridge.close() == {"state": "closed"}
    assert window.calls == ["minimize", "maximize", "restore", "destroy"]
    assert not hasattr(bridge, "execute")
    assert not hasattr(bridge, "open_path")
```

`tests/js/window-controls.test.mjs` 验证 bridge 缺失时返回 `false` 且不抛错。

- [ ] **步骤 2：运行并确认红灯**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_desktop_bridge.py -q
npm test -- tests/js/window-controls.test.mjs
```

预期：模块不存在。

- [ ] **步骤 3：实现最小窗口白名单**

`backend/desktop_bridge.py`：

```python
class DesktopBridge:
    def __init__(self, *, custom_chrome: bool = True):
        self._window = None
        self._state = "normal"
        self._custom_chrome = custom_chrome

    def bind(self, window) -> None:
        self._window = window

    def _require_window(self):
        if self._window is None:
            raise RuntimeError("window is not ready")
        return self._window

    def minimize(self):
        self._require_window().minimize(); self._state = "minimized"
        return {"state": self._state}

    def toggle_maximize(self):
        window = self._require_window()
        if self._state == "maximized": window.restore(); self._state = "normal"
        else: window.maximize(); self._state = "maximized"
        return {"state": self._state}

    def close(self):
        self._require_window().destroy(); self._state = "closed"
        return {"state": self._state}

    def get_state(self):
        return {"state": self._state, "custom_chrome": self._custom_chrome}
```

该类不得接受命令名、URL、脚本或路径。

- [ ] **步骤 4：绑定 pywebview 并实现降级**

`backend/app.py` 创建窗口：

```python
use_native_frame = os.environ.get("PALDECK_NATIVE_FRAME", "").casefold() in {"1", "true", "yes"}
bridge = DesktopBridge(custom_chrome=not use_native_frame)
window = webview.create_window(
    "PalDeck", launch_url, js_api=bridge,
    width=1280, height=820, min_size=(960, 640),
    frameless=not use_native_frame, easy_drag=False,
    shadow=True, background_color="#EEF4FF", text_select=True,
    confirm_close=False, resizable=True,
)
bridge.bind(window)
webview.start(debug=False, private_mode=False)
```

只在 Windows pywebview 模式显示 `.window-chrome`；浏览器模式或 `PALDECK_NATIVE_FRAME=1` 隐藏自绘按钮并使用原生标题栏。把 `PALDECK_NATIVE_FRAME=1` 记录为兼容性降级入口。

- [ ] **步骤 5：实现前端控制和拖拽区**

`frontend/window-controls.js`：

```js
const hostWindow = globalThis.window;
let ready = Boolean(hostWindow?.pywebview?.api);
globalThis.document?.addEventListener("pywebviewready", () => { ready = true; });
export async function callWindowControl(name) {
  const api = hostWindow?.pywebview?.api;
  if (!ready || !api || !["minimize", "toggle_maximize", "close"].includes(name)) return false;
  await api[name]();
  return true;
}
```

HTML 窗口按钮使用中文 `aria-label`；顶部空白区添加 `.pywebview-drag-region`，按钮、输入、导航、滚动区不在拖拽节点内。`.window-chrome` 默认隐藏，收到 `pywebviewready` 后先调用 `get_state()`，只有 `custom_chrome === true` 才显示，因此浏览器模式和 `PALDECK_NATIVE_FRAME=1` 不会出现重复窗口按钮。

- [ ] **步骤 6：验证并提交**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_desktop_bridge.py tests/test_frontend_contract.py -q
npm test
node --check frontend/window-controls.js
git diff --check
git add backend/desktop_bridge.py backend/app.py frontend tests/test_desktop_bridge.py tests/js/window-controls.test.mjs tests/test_frontend_contract.py
git commit -m "feat: 添加安全的桌面窗口控制"
```

---

### 任务 4：重构“我的模组”真实列表

**文件：**
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 修改：`frontend/styles.css`
- 修改：`frontend/ui-model.js`
- 修改：`tests/js/ui-model.test.mjs`
- 修改：`tests/test_frontend_contract.py`

- [ ] **步骤 1：添加来源/状态筛选与状态映射失败测试**

测试本地 `enabled/disabled/modified/missing/conflict` 和 Workshop `enabled/valid/deployed/needs_restart/cleanup_pending`，并断言：

```js
assert.deepEqual(
  deriveModView(mods, { query: "", source: "steam_workshop", status: "abnormal" }).items.map(x => x.workshop_id),
  ["103"]
);
assert.equal(statusLabel({ source: "steam_workshop", enabled: true, needs_restart: true }), "已启用 · 下次启动生效");
```

Python 契约断言四个统计卡存在且没有“可更新”假统计。

- [ ] **步骤 2：运行红灯**

```bash
npm test -- tests/js/ui-model.test.mjs
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py -q -k "mod"
```

- [ ] **步骤 3：实现筛选、统计和列表几何**

- 搜索：名称、类型、Nexus ID、Workshop ID、作者。
- 来源：全部、本地、Steam Workshop。
- 状态：全部、已启用、已禁用、异常。
- 统计：总数、已启用、已禁用、异常；全部从 `state.mods` 派生。
- 本地行保留启停/打开/重扫/删除；Workshop 行保留启停/打开/Steam 页面。
- `can_toggle === false` 或异常时不错误启停。
- 长名称和路径使用 `title` 与可聚焦文本；状态同时显示文字和图标。

`filterMods()` 改为：

```js
const view = deriveModView(state.mods, {
  query: $("#modFilter").value,
  source: $("#modSourceFilter").value,
  status: $("#modStatusFilter").value,
});
renderModStats($("#modStats"), view.stats);
renderMods($("#modList"), view.items);
```

- [ ] **步骤 4：运行回归并截图**

```bash
npm test
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py tests/test_api.py tests/test_steam_workshop.py -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -Views mods
git diff --check
```

- [ ] **步骤 5：提交**

```bash
git add frontend tests/js/ui-model.test.mjs tests/test_frontend_contract.py
git commit -m "feat: 重构真实数据驱动的模组页面"
```

---

### 任务 5：实现安全文件夹选择和多项顺序导入

**文件：**
- 创建：`backend/import_selection.py`
- 创建：`tests/test_import_selection.py`
- 修改：`backend/desktop_bridge.py`
- 修改：`backend/app.py`
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 修改：`frontend/styles.css`
- 修改：`frontend/ui-model.js`
- 修改：`tests/js/ui-model.test.mjs`
- 修改：`tests/test_api.py`
- 修改：`tests/test_frontend_contract.py`

- [ ] **步骤 1：编写顶层安全枚举失败测试**

`tests/test_import_selection.py`：

```python
def test_lists_only_top_level_regular_zip_and_pak_in_stable_order(tmp_path):
    (tmp_path / "b.PAK").write_bytes(b"pak")
    (tmp_path / "A.zip").write_bytes(b"zip")
    (tmp_path / "note.txt").write_text("x")
    nested = tmp_path / "nested"; nested.mkdir(); (nested / "hidden.zip").write_bytes(b"x")
    assert [p.name for p in list_supported_top_level(tmp_path)] == ["A.zip", "b.PAK"]

def test_rejects_reparse_or_symlink_directory(tmp_path):
    link = tmp_path / "linked"
    make_directory_link_for_test(link)
    with pytest.raises(UnsafeSelectionError):
        list_supported_top_level(link)
```

Windows reparse 测试用 `os.symlink(target, link, target_is_directory=True)` 创建目录链接；若当前账户没有创建链接权限则 `pytest.skip("当前账户不能创建目录链接")`，但生产 `is_reparse_point()` 检查仍必须执行。

- [ ] **步骤 2：编写授权和 API 失败测试**

`SelectionRegistry` 测试 TTL、随机 token、未知/过期 token、冲突后保留、成功后消费；API 测试只接受 `selection_token`，不允许通过该字段提交路径：

```python
response = client.post("/api/mods/import", json={"selection_token": "unknown"}, headers=auth)
assert response.status_code == 410
assert response.get_json()["error_code"] == "selection_expired"
```

- [ ] **步骤 3：运行红灯**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_import_selection.py tests/test_api.py -q -k "selection or folder"
npm test -- tests/js/ui-model.test.mjs
```

- [ ] **步骤 4：实现安全枚举和授权**

`backend/import_selection.py`：

```python
def is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

def list_supported_top_level(folder: Path) -> list[Path]:
    root = folder.resolve(strict=True)
    if not root.is_dir() or is_reparse_point(folder):
        raise UnsafeSelectionError("所选目录不安全")
    found = []
    for entry in os.scandir(root):
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        path = Path(entry.path)
        if path.suffix.casefold() in {".zip", ".pak"}:
            found.append(path)
    return sorted(found, key=lambda path: (path.name.casefold(), path.name))
```

`SelectionRegistry.issue(paths)` 返回不可预测 token 和只读元数据；`resolve(token)` 只返回原生选择器授予的路径。过期自动清理，最大授权数和总文件数有限制。

- [ ] **步骤 5：桥接原生文件夹选择**

`DesktopBridge.choose_mod_folder()` 仅调用 `window.create_file_dialog(webview.FOLDER_DIALOG)`，把返回目录交给安全枚举和 registry；前端不能传入路径参数。返回：

```json
{"items":[{"selection_token":"…","name":"A.zip","size":1234,"kind":"zip"}]}
```

浏览器降级模式隐藏“选择文件夹”，仍保留多文件 `<input multiple>` 和拖放。

- [ ] **步骤 6：API 接受选择授权但保持原事务**

`/api/mods/import` 增加独立 `selection_token` 分支：

- 用 registry 解析路径。
- 冲突时不消费授权，前端用同 token + decision 重试。
- 成功、明确取消或 TTL 到期时消费授权。
- 不复制/删除用户原文件。
- 每个文件仍调用一次现有 `ModService.install()`。

- [ ] **步骤 7：实现前端队列**

- 文件 input 增加 `multiple`。
- 拖放收集全部普通 ZIP/PAK，保持 `FileList` 顺序。
- 文件夹项按后端稳定顺序加入队列。
- 同时只有一个 `uploading/installing/conflict` 项。
- 冲突弹窗只绑定当前项；`replace/keep_both/cancel` 完成后才推进下一项。
- 失败项保留错误与“重试”；成功项从后端返回真实名称和类型。

- [ ] **步骤 8：验证并提交**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_import_selection.py tests/test_api.py tests/test_mod_service_pak.py tests/test_mod_service_ue4ss.py -q
npm test
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -Views import
git diff --check
git add backend frontend tests
git commit -m "feat: 添加安全的多项导入队列"
```

---

### 任务 6：重构匿名只读 Nexus 页面

**文件：**
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 修改：`frontend/styles.css`
- 修改：`frontend/ui-model.js`
- 修改：`tests/js/ui-model.test.mjs`
- 修改：`tests/test_frontend_contract.py`
- 修改：`tests/test_nexus_catalog_contract.py`

- [ ] **步骤 1：添加只读和 fail-closed 失败测试**

断言：

```python
for forbidden in ("加入收藏", "一键安装", "Nexus 登录", "编辑精选"):
    assert forbidden not in html + app + render
for required in ("打开 N 网", "复制尾号", "成人内容已过滤", "匿名只读"):
    assert required in html + render
```

Node 测试 `deriveNexusStatus()` 覆盖 `live/fresh cache/stale cache/warning`，不生成假统计。

- [ ] **步骤 2：运行红灯**

```bash
npm test -- tests/js/ui-model.test.mjs
./.venv-build/Scripts/python.exe -m pytest tests/test_nexus_catalog_contract.py tests/test_frontend_contract.py -q
```

- [ ] **步骤 3：实现高保真目录布局**

- 顶部搜索与三种真实排序：下载、推荐、最新。
- 主区响应式卡片；右侧辅助栏只含 ID 直达、来源/缓存状态、匿名只读和成人过滤说明。
- 纯数字搜索继续走固定 Palworld Mod ID 查询；不增加客户端 URL。
- 卡片在读取 `name/summary/picture_url/author` 前先 `if (mod.adultContent !== false) continue;`，与后端严格 `False` 一致。
- 图片保持 HTTPS、lazy、`no-referrer` 和失败移除。

- [ ] **步骤 4：运行真实边界回归与截图**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_nexus_api.py tests/test_nexus_catalog_contract.py tests/test_frontend_contract.py -q
npm test
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -Views nexus
git diff --check
```

- [ ] **步骤 5：提交**

```bash
git add frontend tests/js/ui-model.test.mjs tests/test_frontend_contract.py tests/test_nexus_catalog_contract.py
git commit -m "feat: 重构匿名只读 Nexus 目录"
```

---

### 任务 7：重构设置与外观卡片网格

**文件：**
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 修改：`frontend/styles.css`
- 修改：`frontend/ui-model.js`
- 修改：`tests/js/ui-model.test.mjs`
- 修改：`tests/test_frontend_contract.py`
- 修改：`tests/test_appearance.py`

- [ ] **步骤 1：为七个设置分组和折叠状态写失败测试**

HTML 契约要求 `game/update/ue4ss/theme/background/effects/advanced` 七个语义 section；高级区使用真实 `<details>` 或同步 `aria-expanded` 的按钮。Node 测试折叠状态只接受已知 section ID。

- [ ] **步骤 2：运行红灯**

```bash
npm test -- tests/js/ui-model.test.mjs
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py tests/test_appearance.py -q
```

- [ ] **步骤 3：实现设置网格和轻量预览**

- 游戏目录、启动更新、UE4SS、主题、背景、动效、高级设置各自独立卡片。
- UE4SS 的内置、固定上游、本地 ZIP 和 Workshop 互斥提示完整保留。
- 外观预览只使用静态 DOM 示意和当前 CSS token，不嵌套 iframe、不调用 API。
- 背景上传/重置仍经过 `backgroundWriteQueue`；主题/遮罩/模糊仍经过 revision guard。
- `prefers-reduced-motion` 状态在动效卡中明确显示。

- [ ] **步骤 4：验证竞态、真实 API 与截图**

```bash
npm test
./.venv-build/Scripts/python.exe -m pytest tests/test_appearance.py tests/test_frontend_contract.py tests/test_ue4ss_provider.py tests/test_api.py -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -Views settings
git diff --check
```

- [ ] **步骤 5：提交**

```bash
git add frontend tests/js/ui-model.test.mjs tests/test_frontend_contract.py tests/test_appearance.py
git commit -m "feat: 重构设置与外观预览页面"
```

---

### 任务 8：重构可核实的开源致谢页面

**文件：**
- 修改：`backend/credits.py`
- 修改：`backend/app.py`
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 修改：`frontend/styles.css`
- 修改：`tests/test_credits.py`
- 修改：`tests/test_frontend_contract.py`

- [ ] **步骤 1：编写真实项目和可信链接失败测试**

```python
def test_credits_use_real_project_and_group_roles_only(client, auth_headers):
    items = client.get("/api/credits", headers=auth_headers).get_json()["data"]
    names = {item["name"] for item in items}
    assert {"PalDeck", "Okaetsu/RE-UE4SS", "UE4SS-RE/RE-UE4SS"} <= names
    assert not {"KingEnderBrine", "Joker409", "Laezel", "Rayden"} & names
```

测试 PalDeck 主页、Issues、LICENSE 都通过固定 ID 打开；未知 ID 和前端 URL 被拒绝。

- [ ] **步骤 2：运行红灯**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_credits.py tests/test_frontend_contract.py -q
```

- [ ] **步骤 3：实现真实致谢布局**

- 顶部使用 `/assets/app.png` 和 `/api/health` 真实版本。
- 核心项目显示真实作者/组织、用途、版本、许可证。
- 测试者、Mod 作者和社区支持者按群体致谢，不虚构账户。
- 完整依赖继续来自固定 catalog；不按概念图误列 Electron/Vue/Tailwind。
- 所有外链按钮只提交固定 ID 到 `/api/system/open-trusted-link`。

- [ ] **步骤 4：验证并提交**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_credits.py tests/test_api.py tests/test_frontend_contract.py -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -Views credits
git diff --check
git add backend/credits.py backend/app.py frontend tests/test_credits.py tests/test_frontend_contract.py
git commit -m "feat: 重构可核实的开源致谢页面"
```

---

### 任务 9：补齐可访问性、错误状态和视觉基线

**文件：**
- 创建：`frontend/a11y.js`
- 创建：`tests/js/a11y.test.mjs`
- 创建：`tests/visual/baselines/*.png`
- 修改：`frontend/index.html`
- 修改：`frontend/app.js`
- 修改：`frontend/styles.css`
- 修改：`tests/test_frontend_contract.py`
- 修改：`scripts/compare_ui_screenshots.py`

- [ ] **步骤 1：编写焦点、ARIA 和错误状态失败测试**

`tests/js/a11y.test.mjs`：

```js
import test from "node:test";
import assert from "node:assert/strict";
import { rememberFocus, restoreFocus, setExpanded } from "../../frontend/a11y.js";

test("折叠状态同时更新 aria-expanded 和 hidden", () => {
  const button = fakeButton(); const panel = fakePanel();
  setExpanded(button, panel, true);
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal(panel.hidden, false);
});
```

Python 契约检查五页都存在 loading/empty/error 容器或统一 renderer，所有仅图标按钮有中文 `aria-label`，导航保持唯一 `aria-current`。

- [ ] **步骤 2：运行红灯**

```bash
npm test -- tests/js/a11y.test.mjs
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py -q -k "aria or error or loading"
```

- [ ] **步骤 3：实现统一状态和焦点管理**

- `a11y.js` 只接受 DOM 节点，不接受 HTML 字符串。
- 打开模态框前记录触发元素；关闭后恢复焦点。
- 五页定义 loading、empty、success/partial、error；stale cache 同时显示数据与警告。
- Toast 只报告短期结果；需要用户修复的问题留在页面。
- `prefers-reduced-motion` 关闭樱花和大幅位移；状态不用颜色单独表达。

- [ ] **步骤 4：生成 15 张候选截图**

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -OutputDir .tmp/ui-v2.2-candidate
./.venv-build/Scripts/python.exe scripts/compare_ui_screenshots.py --candidate .tmp/ui-v2.2-candidate --report .tmp/ui-v2.2-report.json
```

人工对照五张概念图审查：外壳比例、侧栏、底栏、卡片、留白、长文本、深色主题、自定义背景。只有人工批准后，才把候选复制为 `tests/visual/baselines/`；不得由脚本自动批准。

- [ ] **步骤 5：运行视觉回归和完整前端测试**

```bash
./.venv-build/Scripts/python.exe scripts/compare_ui_screenshots.py --candidate .tmp/ui-v2.2-candidate --baseline tests/visual/baselines --report .tmp/ui-v2.2-report.json
npm test
./.venv-build/Scripts/python.exe -m pytest tests/test_frontend_contract.py tests/test_visual_fixture_contract.py -q
node --check frontend/a11y.js
git diff --check
```

预期：15 张尺寸正确且差异在批准阈值内；无横向滚动、裁切或死按钮。

- [ ] **步骤 6：提交**

```bash
git add frontend tests/js/a11y.test.mjs tests/test_frontend_contract.py tests/visual/baselines scripts/compare_ui_screenshots.py
git commit -m "test: 完成 v2.2 可访问性与视觉基线"
```

---

### 任务 10：完整回归、性能和独立审查

**文件：**
- 修改：`tests/test_frontend_contract.py`（仅在审查发现契约遗漏时）
- 修改：`scripts/capture_ui.ps1`（仅在真实 Windows 捕获问题时）
- 创建：`.tmp/ui-v2.2-review/`（不提交）

- [ ] **步骤 1：运行完整自动化验证**

```bash
npm test
./.venv-build/Scripts/python.exe -m pytest -q
for file in frontend/*.js; do node --check "$file"; done
./.venv-build/Scripts/python.exe -m compileall -q backend launcher.py scripts
git diff --check
```

预期：全部 PASS，无 warning 引起的失败。

- [ ] **步骤 2：运行真实 Windows 三档截图**

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/capture_ui.ps1 -OutputDir .tmp/ui-v2.2-review
./.venv-build/Scripts/python.exe scripts/compare_ui_screenshots.py --candidate .tmp/ui-v2.2-review --baseline tests/visual/baselines --report .tmp/ui-v2.2-review.json
```

在 100% 与 150% DPI 人工检查 `1280×820`，并在 `1600×1000` 与 `960×640` 检查五页。

- [ ] **步骤 3：做性能和安全 spot check**

- DevTools Performance 验证长列表滚动没有每行 `backdrop-filter`。
- 检查 Nexus 图片无 referrer、成人项为 0。
- 检查导入队列冲突不会推进下一项。
- 检查目录选择不递归、不跟随 reparse、不接受前端路径。
- 检查窗口 bridge 没有任意命令/路径/URL 方法。
- 检查无边框最大化不覆盖任务栏；`PALDECK_NATIVE_FRAME=1` 能降级。

- [ ] **步骤 4：请求两阶段独立审查**

先审规格符合性，再审代码质量。任何 Critical/Important 必须修复并重新运行本任务；Minor 记录是否修复及理由。

- [ ] **步骤 5：提交审查修复（如有）**

```bash
git add backend frontend tests scripts README.md package.json
git diff --cached --name-only
git commit -m "fix: 修正 v2.2 界面审查问题"
```

若无修改，不创建空提交。

---

### 任务 11：升级、可复现构建并发布 v2.2.0

**文件：**
- 修改：`backend/version.py`
- 修改：`backend/smoke_check.py`
- 修改：`scripts/build_portable.ps1`
- 修改：`scripts/smoke_portable.ps1`
- 修改：`tests/test_smoke_check.py`
- 修改：`tests/test_release_script_contract.py`
- 修改：`README.md`
- 创建：`docs/verification/2026-07-13-paldeck-v2.2-release.md`

- [ ] **步骤 1：先写版本和 smoke 红灯测试**

`tests/test_release_script_contract.py` 断言：

```python
assert APP_VERSION == "2.2.0"
for marker in ("app_statusbar", "responsive_shell", "desktop_bridge", "nexus_adult_filtered", "import_queue_empty"):
    assert marker in read_script("smoke_portable.ps1")
```

`tests/test_smoke_check.py` 要求 Frozen 报告继续包含五视图、三樱花、Workshop empty、Okaetsu 元数据，并新增 v2.2 外壳标记。

- [ ] **步骤 2：运行红灯**

```bash
./.venv-build/Scripts/python.exe -m pytest tests/test_release_script_contract.py tests/test_smoke_check.py -q
```

预期：当前版本 `2.1.1` 且缺少 v2.2 marker。

- [ ] **步骤 3：升级版本和发布契约**

- `backend/version.py` 改为 `2.2.0`。
- README 说明高保真响应式界面、匿名只读 Nexus、成人过滤、Workshop/UE4SS 保留和 `PALDECK_NATIVE_FRAME=1` 降级。
- Build 脚本继续只打包生产 `backend/frontend/assets/bundled_mods/third_party` 等，明确不打包 `tests/visual` 和概念图。
- Frozen smoke 检查三段式外壳、五页、底栏、窗口桥标记、导入空队列、Nexus 成人过滤和现有 v2.1 能力。

- [ ] **步骤 4：运行发布前完整验证**

```bash
npm test
./.venv-build/Scripts/python.exe -m pytest -q
for file in frontend/*.js; do node --check "$file"; done
./.venv-build/Scripts/python.exe -m compileall -q backend launcher.py scripts
git diff --check
git status --short
```

- [ ] **步骤 5：提交 packaged source**

```bash
git add backend frontend scripts tests README.md package.json
git commit -m "release: 准备 PalDeck v2.2.0"
git status --short
```

记录该提交为 `SOURCE_COMMIT`。

- [ ] **步骤 6：执行两次全新可复现构建**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1 *> .tmp\build-v2.2.0-1.log
Copy-Item dist\PalDeck-portable\PalDeck.exe .tmp\PalDeck-v2.2.0-build1.exe
Copy-Item dist\PalDeck-v2.2.0-windows-portable.zip .tmp\PalDeck-v2.2.0-build1.zip
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1 *> .tmp\build-v2.2.0-2.log
Get-FileHash .tmp\PalDeck-v2.2.0-build1.exe,dist\PalDeck-portable\PalDeck.exe -Algorithm SHA256
Get-FileHash .tmp\PalDeck-v2.2.0-build1.zip,dist\PalDeck-v2.2.0-windows-portable.zip -Algorithm SHA256
```

预期：两次 EXE 大小/摘要相同，两次 ZIP 大小/摘要相同；每次构建内 Python/Node 测试全部通过。

- [ ] **步骤 7：执行 Frozen smoke 与资产校验**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke_portable.ps1 *> .tmp\smoke-v2.2.0.log
```

随后验证：

- Frozen 报告全部项目 PASS。
- ZIP `testzip()` 返回 `None`。
- `.sha256` sidecar 与 ZIP 相符。
- 便携 README 的 source commit 等于 `SOURCE_COMMIT`。
- Okaetsu ZIP 摘要、许可证和第三方 NOTICE 相符。
- 无 PalDeck 进程或本次归属 `_MEI*` 残留。

- [ ] **步骤 8：写验证文档并请求最终 reviewer gate**

`docs/verification/2026-07-13-paldeck-v2.2-release.md` 记录：

- Source/Final commit。
- 两次 EXE/ZIP 大小和 SHA-256。
- Python/Node/语法/compileall 数量。
- Frozen smoke 项目和三档截图审查结果。
- Nexus 成人过滤、Workshop、UE4SS、ZIP/sidecar/许可证证据。
- 已知残余风险：未签名 EXE、不同 Windows/WebView2 字体栅格差异。

最终 reviewer 必须明确“无 Critical/Important，允许发布”。

- [ ] **步骤 9：提交验证文档**

```bash
git add docs/verification/2026-07-13-paldeck-v2.2-release.md
git commit -m "docs: 记录 PalDeck v2.2 发布验证"
git diff --name-only "$SOURCE_COMMIT"..HEAD
```

预期：`source..HEAD` 只有验证文档。

- [ ] **步骤 10：推送并创建正式 Release**

仅在 reviewer 允许后执行：

```bash
git push -u paldeck feature/v2.2-concept-ui
git push paldeck HEAD:main
gh release create v2.2.0 \
  dist/PalDeck-v2.2.0-windows-portable.zip \
  dist/PalDeck-v2.2.0-windows-portable.zip.sha256 \
  --repo ylty1516/PalDeck \
  --target "$(git rev-parse HEAD)" \
  --title "PalDeck v2.2.0" \
  --notes-file .tmp/release-v2.2.0.md \
  --latest
```

原仓库 `ylty1516/palworld-mod-manager` 的 `main` 不推送。

- [ ] **步骤 11：回下载校验并复制桌面文件**

```bash
rm -rf .tmp/github-v2.2.0-verify
gh release download v2.2.0 --repo ylty1516/PalDeck --dir .tmp/github-v2.2.0-verify
(cd .tmp/github-v2.2.0-verify && sha256sum -c PalDeck-v2.2.0-windows-portable.zip.sha256)
mkdir -p '/c/Users/yyx/Desktop/pal mod'
cp -f dist/PalDeck-v2.2.0-windows-portable.zip '/c/Users/yyx/Desktop/pal mod/'
```

确认 GitHub Release `isDraft=false`、`isPrerelease=false`、target commit 正确、回下载摘要与本地产物一致；桌面根目录不新增发布文件。
