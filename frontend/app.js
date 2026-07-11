import { ApiError, request } from "./api.js";
import { createEffects } from "./effects.js";
import { renderConflict, renderDetectedGames, renderMessage, renderMods, renderNexus } from "./render.js";

const $ = (selector, root = document) => root.querySelector(selector);
const state = {
  mods: [], nexus: [], gamePath: "", selectedModFile: null, pendingUploadToken: null,
  pendingDeleteId: null, pendingDeleteForce: false, updateInfo: null,
  appearance: { theme: "aurora-glass", mask: 0.35, blur: 0, position: "center", petals: "medium", background: "default" },
};
const effects = createEffects();
const VIEW_COPY = Object.freeze({
  mods: ["我的模组", "管理已安装的模组"], import: ["导入安装", "自动识别并安全安装 ZIP / PAK"],
  nexus: ["N网热门", "浏览 Nexus Mods 热门与最新内容"], settings: ["设置与外观", "游戏工具、更新与三套主题"],
});
function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = String(message || "操作完成");
  $("#toastHost").append(item);
  setTimeout(() => item.remove(), 3600);
}

function setBusy(active, message = "处理中…") {
  const overlay = $("#busyOverlay");
  overlay.hidden = !active;
  $("#busyText").textContent = message;
  document.documentElement.classList.toggle("is-busy", active);
}

async function run(trigger, operation, { global = false, busyText = "处理中…" } = {}) {
  if (trigger?.disabled) return undefined;
  if (trigger) { trigger.disabled = true; trigger.classList.add("loading"); trigger.setAttribute("aria-busy", "true"); }
  if (global) setBusy(true, busyText);
  try { return await operation(); }
  catch (error) {
    if (!(error instanceof ApiError && error.status === 409)) toast(error.message || "操作失败", "error");
    throw error;
  } finally {
    if (trigger) { trigger.disabled = false; trigger.classList.remove("loading"); trigger.removeAttribute("aria-busy"); }
    if (global) setBusy(false);
  }
}

async function switchView(name) {
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  const copy = VIEW_COPY[name];
  $("#viewTitle").textContent = copy[0];
  $("#viewSubtitle").textContent = copy[1];
  if (name === "mods") await loadMods();
  if (name === "nexus" && !state.nexus.length) await loadNexus("popular");
  if (name === "settings") await loadSettings();
}

function applyAppearance(settings) {
  state.appearance = { ...state.appearance, ...settings };
  document.documentElement.dataset.theme = state.appearance.theme;
  document.documentElement.style.setProperty("--background-mask", String(state.appearance.mask));
  document.documentElement.style.setProperty("--background-blur", `${state.appearance.blur}px`);
  document.documentElement.style.setProperty("--background-position", state.appearance.position.replace("-", " "));
  $("#maskRange").value = Math.round(state.appearance.mask * 100);
  $("#blurRange").value = state.appearance.blur;
  $("#maskValue").textContent = `${Math.round(state.appearance.mask * 100)}%`;
  $("#blurValue").textContent = `${state.appearance.blur}px`;
  $("#petalLevel").value = state.appearance.petals;
  document.querySelectorAll("[data-theme-value]").forEach((button) => {
    const selected = button.dataset.themeValue === state.appearance.theme;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  document.querySelectorAll("[data-position]").forEach((button) => {
    const selected = button.dataset.position === state.appearance.position;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  effects.update(state.appearance.petals);
}

function refreshBackground() {
  const url = `/api/appearance/background/current?v=${Date.now()}`;
  document.documentElement.style.setProperty("--background-url", `url("${url}")`);
}

async function loadMods() {
  renderMessage($("#modList"), "正在加载模组…", "empty-state glass-panel");
  state.mods = await request("/api/mods");
  filterMods();
}

function filterMods() {
  const query = $("#modFilter").value.trim().toLocaleLowerCase();
  const filtered = state.mods.filter((mod) => !query || [mod.name, mod.mod_type, mod.nexus_id].some((value) => String(value ?? "").toLocaleLowerCase().includes(query)));
  $("#modStats").textContent = `共 ${state.mods.length} 个 · 已启用 ${state.mods.filter((mod) => mod.enabled).length} 个`;
  renderMods($("#modList"), filtered);
}

async function importSelected(decision = "cancel") {
  let options;
  if (state.pendingUploadToken) {
    options = { method: "POST", body: { upload_token: state.pendingUploadToken, decision } };
  } else {
    if (!state.selectedModFile) throw new ApiError("请先选择 ZIP 或 PAK 文件");
    const form = new FormData();
    form.append("file", state.selectedModFile);
    form.append("type", $("#importType").value);
    if ($("#importName").value.trim()) form.append("name", $("#importName").value.trim());
    if ($("#importNexusId").value) form.append("nexus_id", $("#importNexusId").value);
    options = { method: "POST", body: form, timeout: 120000 };
  }
  try {
    const result = await request("/api/mods/import", options);
    state.pendingUploadToken = null;
    state.selectedModFile = null;
    $("#selectedModFile").textContent = "尚未选择文件";
    $("#importResult").textContent = `安装成功：${result.name || result.mod?.name || "模组"}`;
    toast("模组安装成功", "success");
    await loadMods();
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      state.pendingUploadToken = error.details?.upload_token || null;
      renderConflict($("#conflictDetails"), error.details);
      openModal($("#conflictModal"));
    }
    throw error;
  }
}

function openModal(dialog) {
  dialog.showModal();
  dialog.querySelector("[autofocus]")?.focus();
}

async function cancelImportConflict() {
  const token = state.pendingUploadToken;
  state.pendingUploadToken = null;
  $("#conflictModal").close();
  if (!token) return;
  try {
    await request("/api/mods/import", { method: "POST", body: { upload_token: token, decision: "cancel" } });
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 409)) throw error;
  }
}

async function deletePendingMod() {
  const id = state.pendingDeleteId;
  if (!id) return;
  $("#deleteModal").close();
  const suffix = state.pendingDeleteForce ? "?force_modified=true" : "";
  try {
    await request(`/api/mods/${encodeURIComponent(id)}${suffix}`, { method: "DELETE" });
    state.pendingDeleteId = null;
    state.pendingDeleteForce = false;
    await loadMods();
    toast("模组已删除", "success");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && !state.pendingDeleteForce) {
      state.pendingDeleteForce = true;
      $("#deleteMessage").textContent = "检测到已修改文件。再次确认将强制删除这些文件，此操作不可撤销。";
      openModal($("#deleteModal"));
      return;
    }
    throw error;
  }
}

function openValidatedNexus(target) {
  let url;
  try { url = new URL(target.dataset.url); } catch { throw new ApiError("N 网地址无效"); }
  if (url.protocol !== "https:") throw new ApiError("仅允许打开安全的 HTTPS 地址");
  window.open(url, "_blank", "noopener");
}

async function copyNexusId(target) {
  const id = target.dataset.id || "";
  try {
    await navigator.clipboard.writeText(id);
    toast(`已复制尾号 #${id}`, "success");
  } catch {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(target);
    selection.removeAllRanges();
    selection.addRange(range);
    toast("无法访问剪贴板，已选中尾号，请按 Ctrl+C", "info");
  }
}

async function loadNexus(mode = "popular") {
  $("#nexusStatus").textContent = "正在连接 Nexus Mods…";
  const query = $("#nexusQuery").value.trim();
  const path = mode === "search" ? `/api/nexus/search?q=${encodeURIComponent(query)}&count=24` : mode === "latest" ? "/api/nexus/latest?count=24" : "/api/nexus/popular?sort=downloads&count=24";
  state.nexus = await request(path);
  $("#nexusStatus").textContent = `已加载 ${state.nexus.length} 个模组`;
  renderNexus($("#nexusGrid"), state.nexus);
}

function showPathInfo(status) {
  state.gamePath = status.path || state.gamePath;
  $("#pathChip").textContent = state.gamePath || "未配置游戏目录";
  $("#gamePathInput").value = state.gamePath;
  $("#pathInfo").textContent = status.valid ? `目录有效${status.has_ue4ss ? " · UE4SS 已安装" : " · UE4SS 未安装"}` : "目录尚未配置";
}

async function loadUe4ssStatus() {
  if (!state.gamePath) { $("#ue4ssStatus").textContent = "请先配置游戏目录"; return; }
  const status = await request("/api/ue4ss/status");
  $("#ue4ssStatus").textContent = status.installed ? "UE4SS 已安装" : "UE4SS 未安装";
}

async function loadSettings() {
  const status = await request("/api/game/status");
  if (status.configured) showPathInfo(status);
  await loadUe4ssStatus();
}

async function installZip(file) {
  if (!file) return;
  const form = new FormData(); form.append("file", file);
  const result = await request("/api/ue4ss/install-zip", { method: "POST", body: form, timeout: 120000 });
  $("#ue4ssResult").textContent = result.message || "UE4SS 安装完成";
  await loadUe4ssStatus();
}

function chooseTheme(event) { applyAppearance({ theme: event.currentTarget.dataset.themeValue }); }
function choosePosition(event) { applyAppearance({ position: event.currentTarget.dataset.position }); }
function noop() { /* Text and select controls keep their native editable behavior. */ }

export const ACTION_HANDLERS = Object.freeze({
  showMods: async () => switchView("mods"),
  showImport: async () => switchView("import"),
  showNexus: async () => switchView("nexus"),
  showSettings: async () => switchView("settings"),
  restartAdmin: async () => { await request("/api/system/restart-admin", { method: "POST", body: {} }); toast("正在请求管理员权限", "success"); },
  refreshMods: async () => loadMods(),
  openModsFolder: async () => request("/api/mods/open-folder"),
  filterMods: () => filterMods(),
  chooseModFile: () => $("#modFileInput").click(),
  selectModFile: (event) => { state.selectedModFile = event.currentTarget.files?.[0] || null; $("#selectedModFile").textContent = state.selectedModFile?.name || "尚未选择文件"; },
  changeImportType: noop,
  editImportName: noop,
  editImportNexusId: noop,
  importMod: async () => importSelected(),
  editNexusQuery: noop,
  searchNexus: async () => loadNexus("search"),
  refreshNexus: async () => loadNexus("popular"),
  latestNexus: async () => loadNexus("latest"),
  editGamePath: noop,
  autoDetectGame: async () => { const data = await request("/api/game/detect"); renderDetectedGames($("#detectList"), data.installs || []); },
  saveGamePath: async () => { const path = $("#gamePathInput").value.trim(); const result = await request("/api/game/set", { method: "POST", body: { path } }); showPathInfo({ ...result, path: result.game_path || path, valid: true }); toast("游戏路径已保存", "success"); },
  repairFolders: async () => { await request("/api/game/ensure-folders", { method: "POST", body: {} }); toast("模组目录已修复", "success"); },
  installUe4ss: async () => { const result = await request("/api/ue4ss/install-latest", { method: "POST", body: {}, timeout: 120000 }); $("#ue4ssResult").textContent = result.message || "安装完成"; await loadUe4ssStatus(); },
  refreshUe4ss: async () => loadUe4ssStatus(),
  chooseUe4ssZip: () => $("#ue4ssZipInput").click(),
  selectUe4ssZip: async (event) => installZip(event.currentTarget.files?.[0]),
  checkUpdate: async () => { state.updateInfo = await request("/api/update/check", { timeout: 30000 }); $("#updateStatus").textContent = state.updateInfo.update_available ? `发现新版本 ${state.updateInfo.remote_version}` : `已是最新版 ${state.updateInfo.local_version}`; },
  applyUpdate: async () => { const result = await request("/api/update/apply", { method: "POST", body: { url: state.updateInfo?.asset?.browser_download_url }, timeout: 120000 }); toast(result.message || "更新已准备，将自动重启", "success"); },
  themeAurora: chooseTheme,
  themeIvory: chooseTheme,
  themeStarlit: chooseTheme,
  chooseBackground: () => $("#backgroundInput").click(),
  resetBackground: async () => { const saved = await request("/api/appearance/background", { method: "DELETE" }); applyAppearance(saved); refreshBackground(); toast("已恢复默认背景", "success"); },
  selectBackground: async (event) => { const file = event.currentTarget.files?.[0]; if (!file) return; const form = new FormData(); form.append("file", file); const saved = await request("/api/appearance/background", { method: "POST", body: form, timeout: 60000 }); applyAppearance(saved); refreshBackground(); toast("背景已更新", "success"); },
  changeMask: (event) => applyAppearance({ mask: Number(event.currentTarget.value) / 100 }),
  changeBlur: (event) => applyAppearance({ blur: Number(event.currentTarget.value) }),
  positionTopLeft: choosePosition,
  positionTopCenter: choosePosition,
  positionTopRight: choosePosition,
  positionCenterLeft: choosePosition,
  positionCenter: choosePosition,
  positionCenterRight: choosePosition,
  positionBottomLeft: choosePosition,
  positionBottomCenter: choosePosition,
  positionBottomRight: choosePosition,
  changePetals: (event) => applyAppearance({ petals: event.currentTarget.value }),
  saveAppearance: async () => { const { theme, mask, blur, position, petals } = state.appearance; const saved = await request("/api/appearance", { method: "POST", body: { theme, mask, blur, position, petals } }); applyAppearance(saved); toast("外观已保存", "success"); },
  cancelConflict: async () => cancelImportConflict(),
  replaceConflict: async () => { $("#conflictModal").close(); await importSelected("replace"); },
  keepBothConflict: async () => { $("#conflictModal").close(); await importSelected("keep_both"); },
  cancelDelete: () => { state.pendingDeleteId = null; state.pendingDeleteForce = false; $("#deleteModal").close(); },
  approveDelete: async () => deletePendingMod(),
});

async function dispatchStatic(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !ACTION_HANDLERS[target.dataset.action]) return;
  const expected = ["INPUT", "SELECT"].includes(target.tagName) ? ["input", "change"] : ["click"];
  if (!expected.includes(event.type)) return;
  if (target.tagName === "INPUT" && ["file", "range"].includes(target.type) && event.type !== "change" && target.type !== "range") return;
  const actionEvent = { currentTarget: target, target: event.target, type: event.type };
  try { await run(target, () => ACTION_HANDLERS[target.dataset.action](actionEvent), { global: target.dataset.action === "importMod" || target.dataset.action === "installUe4ss" || target.dataset.action === "applyUpdate", busyText: "请稍候，正在处理…" }); }
  catch { /* errors are surfaced by run or a conflict modal */ }
}

async function handleDynamicAction(event) {
  const target = event.target.closest("[data-dynamic-action]");
  if (!target) return;
  const id = target.dataset.id;
  try {
    switch (target.dataset.dynamicAction) {
      case "toggleMod": await run(target, async () => { await request(`/api/mods/${encodeURIComponent(id)}/toggle`, { method: "POST", body: { enabled: target.dataset.enabled === "true" } }); await loadMods(); }); break;
      case "openModFolder": await run(target, () => request(`/api/mods/open-folder?id=${encodeURIComponent(id)}`)); break;
      case "deleteMod": state.pendingDeleteId = id; state.pendingDeleteForce = false; $("#deleteMessage").textContent = `确定删除“${state.mods.find((mod) => String(mod.id) === id)?.name || id}”吗？`; openModal($("#deleteModal")); break;
      case "useGamePath": $("#gamePathInput").value = target.dataset.path || ""; toast("已填入检测到的路径", "success"); break;
      case "openNexus": await run(target, () => openValidatedNexus(target)); break;
      case "copyNexusId": await run(target, () => copyNexusId(target)); break;
      default: return;
    }
  } catch { /* run has already displayed the error */ }
}

function setupDropzone() {
  const zone = $("#dropzone");
  for (const name of ["dragenter", "dragover", "dragleave", "drop"]) zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.toggle("dragging", name === "dragenter" || name === "dragover"); if (name === "drop") { state.selectedModFile = event.dataTransfer?.files?.[0] || null; $("#selectedModFile").textContent = state.selectedModFile?.name || "尚未选择文件"; } });
}

async function init() {
  document.addEventListener("click", dispatchStatic);
  document.addEventListener("input", dispatchStatic);
  document.addEventListener("change", dispatchStatic);
  $("#modList").addEventListener("click", handleDynamicAction);
  $("#modList").addEventListener("change", handleDynamicAction);
  $("#detectList").addEventListener("click", handleDynamicAction);
  $("#nexusGrid").addEventListener("click", handleDynamicAction);
  $("#conflictModal").addEventListener("cancel", async (event) => {
    event.preventDefault();
    try { await run(null, cancelImportConflict); } catch { /* run displayed the error */ }
  });
  $("#deleteModal").addEventListener("cancel", (event) => {
    event.preventDefault();
    state.pendingDeleteId = null;
    state.pendingDeleteForce = false;
    $("#deleteModal").close();
  });
  setupDropzone();
  try {
    const [health, appearance, game] = await Promise.all([request("/api/health"), request("/api/appearance"), request("/api/game/status")]);
    $("#healthStatus").textContent = `服务正常 · v${health.version || "-"}`;
    applyAppearance(appearance); refreshBackground();
    if (game.configured) showPathInfo(game);
    await loadMods();
  } catch (error) { $("#healthStatus").textContent = "服务连接失败"; toast(error.message, "error"); }
}

init();
