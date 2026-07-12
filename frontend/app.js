import { ApiError, request } from "./api.js";
import { createEffects } from "./effects.js";
import {
  actionableErrorMessage, dynamicActionKey, nextModsGeneration,
  pendingUploadTokenAfterError, resetModFileSelectionState,
} from "./interaction-policy.js";
import { renderConflict, renderDetectedGames, renderMessage, renderMods, renderNexus, revealAdultCard, validatedNexusUrl } from "./render.js";

const $ = (selector, root = document) => root.querySelector(selector);
const state = {
  mods: [], nexus: [], gamePath: "", selectedModFile: null, pendingUploadToken: null,
  pendingDeleteId: null, pendingDeleteForce: false, pendingWorkshopDependency: null, updateInfo: null,
  ue4ssUpdateAvailable: false,
  modsRequestSequence: 0, modsRequestGeneration: 0, modsRequestController: null,
  nexusRequestSequence: 0, nexusRequestController: null, nexusMode: "downloads",
  appearance: { theme: "aurora-glass", mask: 0.35, blur: 0, position: "center", petals: "medium", petal_style: "natural", background: "default" },
};
const effects = createEffects();
const inFlightDynamicActions = new Set();
let workshopWriteQueue = Promise.resolve();
const VIEW_COPY = Object.freeze({
  mods: ["我的模组", "管理已安装的模组"], import: ["导入安装", "自动识别并安全安装 ZIP / PAK"],
  nexus: ["N网热门", "浏览 Nexus Mods 热门与最新内容"], settings: ["设置与外观", "游戏工具、更新与三套主题"],
});
const UE4SS_WRITE_ACTIONS = new Set([
  "installUe4ss", "installUe4ssUpdate", "selectUe4ssZip",
]);
const LOCAL_INPUT_ACTIONS = new Set([
  "filterMods", "changeImportType", "editImportName", "editImportNexusId",
  "editNexusQuery", "editGamePath", "changeMask", "changeBlur", "changePetals",
]);

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

async function run(trigger, operation, { disable = true, global = false, busyText = "处理中…" } = {}) {
  const canDisable = disable && trigger && (trigger.tagName === "BUTTON" || trigger.type === "submit");
  if (canDisable && trigger.disabled) return undefined;
  if (canDisable) { trigger.disabled = true; trigger.classList.add("loading"); trigger.setAttribute("aria-busy", "true"); }
  if (global) setBusy(true, busyText);
  try { return await operation(); }
  catch (error) {
    toast(actionableErrorMessage(error), "error");
    throw error;
  } finally {
    if (canDisable) { trigger.disabled = false; trigger.classList.remove("loading"); trigger.removeAttribute("aria-busy"); }
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
  if (name === "nexus") await loadNexus(state.nexusMode, true);
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
  document.querySelectorAll("[data-petal-style]").forEach((button) => {
    const selected = button.dataset.petalStyle === state.appearance.petal_style;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  effects.update({ level: state.appearance.petals, style: state.appearance.petal_style });
}

function refreshBackground() {
  const url = `/api/appearance/background/current?v=${Date.now()}`;
  document.documentElement.style.setProperty("--background-url", `url("${url}")`);
}

function invalidateModsRequests() {
  state.modsRequestGeneration = nextModsGeneration(state.modsRequestGeneration);
  if (state.modsRequestController) state.modsRequestController.abort();
  state.modsRequestController = null;
  return state.modsRequestGeneration;
}

function beginModsWrite() {
  return invalidateModsRequests();
}

async function loadMods() {
  const sequence = ++state.modsRequestSequence;
  const generation = invalidateModsRequests();
  const controller = new AbortController();
  state.modsRequestController = controller;
  renderMessage($("#modList"), "正在加载模组…", "empty-state glass-panel");
  try {
    const mods = await request("/api/mods", { signal: controller.signal });
    if (sequence !== state.modsRequestSequence || generation !== state.modsRequestGeneration) return false;
    state.mods = mods;
    $("#modConflictNotice").hidden = true;
    filterMods();
    return true;
  } catch (error) {
    if (controller.signal.aborted || generation !== state.modsRequestGeneration) return false;
    throw error;
  } finally {
    if (state.modsRequestController === controller) state.modsRequestController = null;
  }
}

function filterMods() {
  const query = $("#modFilter").value.trim().toLocaleLowerCase();
  const filtered = state.mods.filter((mod) => !query || [
    mod.name, mod.mod_type, mod.nexus_id, mod.status, mod.author,
    mod.install_types, mod.package_name, mod.workshop_id, mod.dependencies,
  ].some((value) => String(value ?? "").toLocaleLowerCase().includes(query)));
  const enabled = state.mods.filter((mod) => (mod.status || mod.audit?.status) === "enabled").length;
  const abnormal = state.mods.filter((mod) => !["enabled", "disabled"].includes(mod.status || mod.audit?.status)).length;
  $("#modStats").textContent = `共 ${state.mods.length} 个 · 显示 ${filtered.length} 个 · 已启用 ${enabled} 个 · 异常 ${abnormal} 个`;
  renderMods($("#modList"), filtered);
}

function isSupportedModFile(file) {
  return Boolean(file && /\.(zip|pak)$/i.test(file.name || ""));
}

function resetModFileSelection() {
  const reset = resetModFileSelectionState(state);
  state.pendingUploadToken = reset.pendingUploadToken;
  state.selectedModFile = reset.selectedModFile;
  $("#modFileInput").value = "";
  $("#selectedModFile").textContent = "尚未选择文件";
  $("#importResult").textContent = "";
}

function selectModFile(file) {
  resetModFileSelection();
  if (file && !isSupportedModFile(file)) throw new ApiError("仅支持 ZIP 或 PAK 文件");
  state.selectedModFile = file || null;
  $("#selectedModFile").textContent = file ? `已选择：${file.name}` : "尚未选择文件";
  $("#importResult").textContent = file ? `识别结果：${/\.pak$/i.test(file.name) ? "PAK 模组" : "ZIP 压缩包（安装时自动识别）"}` : "";
}

async function executeFileOperation(operation) {
  try {
    await operation();
    return true;
  } catch (error) {
    toast(actionableErrorMessage(error), "error");
    return false;
  }
}

async function executeModFileSelection(file) {
  return executeFileOperation(() => selectModFile(file));
}

async function importSelected(decision = "cancel") {
  const retryToken = state.pendingUploadToken;
  let options;
  if (retryToken) {
    options = { method: "POST", body: { upload_token: retryToken, decision } };
  } else {
    if (!state.selectedModFile) throw new ApiError("请先选择 ZIP 或 PAK 文件");
    const form = new FormData();
    form.append("file", state.selectedModFile);
    form.append("type", $("#importType").value);
    if ($("#importName").value.trim()) form.append("name", $("#importName").value.trim());
    if ($("#importNexusId").value) form.append("nexus_id", $("#importNexusId").value);
    options = { method: "POST", body: form, timeout: 120000 };
  }
  beginModsWrite();
  try {
    $("#importResult").textContent = retryToken ? "正在识别并安装：应用冲突处理…" : `正在识别并安装：正在上传 ${state.selectedModFile.name}`;
    const result = await request("/api/mods/import", options);
    resetModFileSelection();
    $("#importResult").textContent = `安装成功：${result.name || result.mod?.name || "模组"} · 类型 ${result.mod_type || result.kind || "已识别"}`;
    toast("模组安装成功", "success");
    await loadMods();
  } catch (error) {
    const nextToken = pendingUploadTokenAfterError(retryToken, error);
    if (error instanceof ApiError && error.status === 409 && error.code === "mod_conflict" && nextToken) {
      state.pendingUploadToken = nextToken;
      $("#importResult").textContent = "识别完成：发现冲突，请选择处理方式";
      renderConflict($("#conflictDetails"), error.details);
      openModal($("#conflictModal"));
      return null;
    }
    if (retryToken) state.pendingUploadToken = null;
    if (error instanceof ApiError && error.status === 410 && error.code === "upload_expired") {
      resetModFileSelection();
      $("#importResult").textContent = "暂存文件已过期，请重新选择文件";
      if ($("#conflictModal").open) $("#conflictModal").close();
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
  if (!token) { resetModFileSelection(); $("#conflictModal").close(); return; }
  beginModsWrite();
  try {
    await request("/api/mods/import", { method: "POST", body: { upload_token: token, decision: "cancel" } });
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 410 && error.code === "upload_expired")) throw error;
  } finally {
    resetModFileSelection();
    $("#conflictModal").close();
  }
}

async function deletePendingMod() {
  const id = state.pendingDeleteId;
  if (!id) return;
  $("#deleteModal").close();
  const suffix = state.pendingDeleteForce ? "?force_modified=true" : "";
  beginModsWrite();
  try {
    await request(`/api/mods/${encodeURIComponent(id)}${suffix}`, { method: "DELETE" });
    state.pendingDeleteId = null;
    state.pendingDeleteForce = false;
    await loadMods();
    toast("模组已删除", "success");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && error.code === "modified_files" && !state.pendingDeleteForce) {
      state.pendingDeleteForce = true;
      $("#deleteMessage").textContent = "检测到已修改文件。再次确认将强制删除这些文件，此操作不可撤销。";
      renderConflict($("#deleteDetails"), error.details);
      openModal($("#deleteModal"));
      return;
    }
    throw error;
  }
}

function replaceWorkshopMods(authoritative) {
  const localMods = state.mods.filter((mod) => mod.source !== "steam_workshop");
  const workshopMods = authoritative.map((mod) => ({ ...mod }));
  state.mods = [...localMods, ...workshopMods];
  filterMods();
}

function queueWorkshopWrite(operation) {
  const next = workshopWriteQueue.then(operation, operation);
  workshopWriteQueue = next.catch(() => undefined);
  return next;
}

function cancelWorkshopDependency() {
  state.pendingWorkshopDependency = null;
  $("#workshopDependencyDetails").replaceChildren();
  if ($("#workshopDependencyModal").open) $("#workshopDependencyModal").close();
}

function renderWorkshopDependents(ids) {
  const list = document.createElement("ul");
  list.className = "stack";
  for (const id of ids) {
    const item = document.createElement("li");
    item.textContent = `Workshop ${id}`;
    list.append(item);
  }
  $("#workshopDependencyDetails").replaceChildren(list);
}

async function toggleWorkshop(id, enabled, confirmDependents = false) {
  beginModsWrite();
  try {
    const authoritative = await request(`/api/workshop/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`, {
      method: "POST", body: confirmDependents ? { confirm_dependents: true } : {},
    });
    state.pendingWorkshopDependency = null;
    replaceWorkshopMods(Array.isArray(authoritative.mods) ? authoritative.mods : []);
    if (Array.isArray(authoritative.cleanup_pending) && authoritative.cleanup_pending.length) {
      toast("检测到无法安全自动清理的 Workshop 事务文件；已隔离保留，请勿手动删除游戏文件。", "warning");
    } else {
      toast(enabled ? "Workshop 模组已启用" : "Workshop 模组已禁用", "success");
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && error.code === "workshop_dependency_conflict") {
      state.pendingWorkshopDependency = { id, enabled };
      renderWorkshopDependents(Array.isArray(error.details?.dependents) ? error.details.dependents : []);
      openModal($("#workshopDependencyModal"));
      return;
    }
    await loadMods();
    throw error;
  }
}

function openValidatedNexus(target) {
  const url = validatedNexusUrl(target.dataset.url);
  if (!url) throw new ApiError("仅允许打开 Nexus Mods 的帕鲁模组详情页");
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

async function loadNexus(mode = "downloads", force = false) {
  state.nexusRequestController?.abort();
  const controller = new AbortController();
  state.nexusRequestController = controller;
  const sequence = ++state.nexusRequestSequence;
  $("#nexusStatus").textContent = "正在连接 Nexus Mods…";
  const query = $("#nexusQuery").value.trim();
  state.nexusMode = mode;
  const forceQuery = force ? "&force=1" : "";
  const path = mode === "search"
    ? `/api/nexus/search?q=${encodeURIComponent(query)}&count=24${forceQuery}`
    : `/api/nexus/popular?sort=${encodeURIComponent(mode)}&count=24${forceQuery}`;
  try {
    const result = await request(path, { signal: controller.signal });
    if (sequence !== state.nexusRequestSequence) return false;
    state.nexus = Array.isArray(result.items) ? result.items : [];
    const source = result.source === "live" ? "实时" : (result.stale ? "过期缓存" : "缓存");
    const fetched = result.fetched_at ? new Date(result.fetched_at).toLocaleString("zh-CN") : "未知时间";
    const warning = result.warning ? ` · 警告：${result.warning}` : "";
    $("#nexusStatus").textContent = `${source} · ${fetched} · ${state.nexus.length} 个模组${warning}`;
    renderNexus($("#nexusGrid"), state.nexus);
    return true;
  } catch (error) {
    if (error.code === "request_cancelled") return false;
    throw error;
  } finally {
    if (state.nexusRequestController === controller) state.nexusRequestController = null;
  }
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
  const asset = status.bundled?.asset;
  $("#ue4ssUpdatedAt").textContent = asset?.updated_at ? `内置更新：${asset.updated_at}` : "内置资源不可用";
  $("#ue4ssDigest").textContent = asset?.sha256 ? `SHA-256：${asset.sha256.slice(0, 10)}` : "";
}

async function loadSettings() {
  const status = await request("/api/game/status");
  if (status.configured) showPathInfo(status);
  await loadUe4ssStatus();
}

async function installWithUe4ssConfirmation(operation) {
  try {
    return await operation(false);
  } catch (error) {
    if (error.code === "ue4ss_conflict" && window.confirm("检测到已有 UE4SS 安装。是否确认替换？")) {
      return operation(true);
    }
    throw error;
  }
}

async function installFixedUe4ss(endpoint) {
  const result = await installWithUe4ssConfirmation((confirmReplace) => request(endpoint, {
    method: "POST", body: confirmReplace ? { confirm_replace: true } : {}, timeout: 120000,
  }));
  $("#ue4ssResult").textContent = result.message || "UE4SS 安装完成";
  if (endpoint === "/api/ue4ss/install-upstream") {
    state.ue4ssUpdateAvailable = false;
    $("#installUe4ssUpdate").hidden = true;
  }
  await loadUe4ssStatus();
}

async function installZip(file) {
  if (!file) return;
  const result = await installWithUe4ssConfirmation((confirmReplace) => {
    const form = new FormData();
    form.append("file", file);
    if (confirmReplace) form.append("confirm_replace", "true");
    return request("/api/ue4ss/install-zip", { method: "POST", body: form, timeout: 120000 });
  });
  $("#ue4ssResult").textContent = result.message || "UE4SS 安装完成";
  await loadUe4ssStatus();
}

async function checkUe4ssUpstream() {
  const result = await request("/api/ue4ss/check-upstream", { method: "POST", body: {}, timeout: 30000 });
  state.ue4ssUpdateAvailable = result.update_available === true;
  $("#installUe4ssUpdate").hidden = !state.ue4ssUpdateAvailable;
  $("#ue4ssResult").textContent = state.ue4ssUpdateAvailable
    ? `发现新版 ${result.asset.updated_at} · ${result.asset.sha256.slice(0, 10)}`
    : "GitHub 版本与内置版本相同";
}

function chooseTheme(event) { applyAppearance({ theme: event.currentTarget.dataset.themeValue }); }
function choosePosition(event) { applyAppearance({ position: event.currentTarget.dataset.position }); }
function choosePetalStyle(event) { applyAppearance({ petal_style: event.currentTarget.dataset.petalStyle }); }
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
  selectModFile: async (event) => executeModFileSelection(event.currentTarget.files?.[0] || null),
  changeImportType: noop,
  editImportName: noop,
  editImportNexusId: noop,
  importMod: async () => importSelected(),
  editNexusQuery: noop,
  searchNexus: async () => loadNexus("search", true),
  downloadsNexus: async () => loadNexus("downloads", true),
  endorsementsNexus: async () => loadNexus("endorsements", true),
  refreshNexus: async () => loadNexus(state.nexusMode, true),
  latestNexus: async () => loadNexus("latest", true),
  editGamePath: noop,
  autoDetectGame: async () => { const data = await request("/api/game/detect"); renderDetectedGames($("#detectList"), data.installs || []); },
  saveGamePath: async () => { const path = $("#gamePathInput").value.trim(); const result = await request("/api/game/set", { method: "POST", body: { path } }); showPathInfo({ ...result, path: result.game_path || path, valid: true }); toast("游戏路径已保存", "success"); },
  repairFolders: async () => { await request("/api/game/ensure-folders", { method: "POST", body: {} }); toast("模组目录已修复", "success"); },
  installUe4ss: async () => installFixedUe4ss("/api/ue4ss/install-bundled"),
  checkUe4ss: async () => checkUe4ssUpstream(),
  installUe4ssUpdate: async () => installFixedUe4ss("/api/ue4ss/install-upstream"),
  chooseUe4ssZip: () => $("#ue4ssZipInput").click(),
  selectUe4ssZip: async (event) => run(event.currentTarget, () => executeFileOperation(() => installZip(event.currentTarget.files?.[0])), { disable: false, global: true, busyText: "正在安装 UE4SS…" }),
  checkUpdate: async () => { state.updateInfo = await request("/api/update/check", { timeout: 30000 }); $("#updateStatus").textContent = state.updateInfo.update_available ? `发现新版本 ${state.updateInfo.remote_version}` : `已是最新版 ${state.updateInfo.local_version}`; },
  applyUpdate: async () => { const result = await request("/api/update/apply", { method: "POST", body: {}, timeout: 120000 }); toast(result.message || "更新已准备，将自动重启", "success"); },
  themeAurora: chooseTheme,
  themeIvory: chooseTheme,
  themeStarlit: chooseTheme,
  chooseBackground: () => $("#backgroundInput").click(),
  resetBackground: async () => { const saved = await request("/api/appearance/background", { method: "DELETE" }); applyAppearance(saved); refreshBackground(); toast("已恢复默认背景", "success"); },
  selectBackground: async (event) => executeFileOperation(async () => { const file = event.currentTarget.files?.[0]; if (!file) return; const form = new FormData(); form.append("file", file); const saved = await request("/api/appearance/background", { method: "POST", body: form, timeout: 60000 }); applyAppearance(saved); refreshBackground(); toast("背景已更新", "success"); }),
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
  petalStyleNatural: choosePetalStyle,
  petalStyleWatercolor: choosePetalStyle,
  petalStyleMinimal: choosePetalStyle,
  saveAppearance: async () => {
    const { theme, mask, blur, position, petals, petal_style } = state.appearance;
    try {
      const saved = await request("/api/appearance", { method: "POST", body: { theme, mask, blur, position, petals, petal_style } });
      applyAppearance(saved);
      toast("外观已保存", "success");
    } catch (error) {
      try {
        const recovered = await request("/api/appearance");
        applyAppearance(recovered);
      } finally {
        throw error;
      }
    }
  },
  cancelConflict: async () => cancelImportConflict(),
  replaceConflict: async () => { $("#conflictModal").close(); await importSelected("replace"); },
  keepBothConflict: async () => { $("#conflictModal").close(); await importSelected("keep_both"); },
  cancelDelete: () => { state.pendingDeleteId = null; state.pendingDeleteForce = false; $("#deleteDetails").replaceChildren(); $("#deleteModal").close(); },
  approveDelete: async () => deletePendingMod(),
  cancelWorkshopDependency: () => cancelWorkshopDependency(),
  approveWorkshopDependency: async () => {
    const pending = state.pendingWorkshopDependency;
    if (!pending) return;
    $("#workshopDependencyModal").close();
    await queueWorkshopWrite(() => toggleWorkshop(pending.id, pending.enabled, true));
  },
});

async function dispatchStatic(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !ACTION_HANDLERS[target.dataset.action]) return;
  const expected = ["INPUT", "SELECT"].includes(target.tagName) ? ["input", "change"] : ["click"];
  if (!expected.includes(event.type)) return;
  if (target.tagName === "INPUT" && ["file", "range"].includes(target.type) && event.type !== "change" && target.type !== "range") return;
  const actionEvent = { currentTarget: target, target: event.target, type: event.type };
  if (LOCAL_INPUT_ACTIONS.has(target.dataset.action)) {
    ACTION_HANDLERS[target.dataset.action](actionEvent);
    return;
  }
  if (target.tagName === "INPUT" || target.tagName === "SELECT") {
    try { await ACTION_HANDLERS[target.dataset.action](actionEvent); }
    catch (error) { toast(actionableErrorMessage(error), "error"); }
    return;
  }
  try { await run(target, () => ACTION_HANDLERS[target.dataset.action](actionEvent), { global: target.dataset.action === "importMod" || UE4SS_WRITE_ACTIONS.has(target.dataset.action) || target.dataset.action === "applyUpdate", busyText: "请稍候，正在处理…" }); }
  catch { /* errors are surfaced by run or a conflict modal */ }
}

async function toggleMod(target, id) {
  beginModsWrite();
  try {
    await request(`/api/mods/${encodeURIComponent(id)}/toggle`, {
      method: "POST", body: { enabled: target.dataset.enabled === "true" },
    });
    await loadMods();
  } catch (error) {
    await loadMods();
    if (error instanceof ApiError && error.status === 409 && error.code === "mod_conflict") {
      renderConflict($("#modConflictNotice"), error.details);
      $("#modConflictNotice").hidden = false;
      toast("切换失败：请先处理冲突文件后重试", "error");
      return;
    }
    throw error;
  }
}

async function handleDynamicAction(event) {
  const target = event.target.closest("[data-dynamic-action]");
  if (!target) return;
  if (target.disabled) return;
  const id = target.dataset.id;
  const actionKey = dynamicActionKey(target.dataset.dynamicAction, id);
  if (inFlightDynamicActions.has(actionKey)) return;
  inFlightDynamicActions.add(actionKey);
  target.disabled = true;
  try {
    switch (target.dataset.dynamicAction) {
      case "toggleMod": await run(target, () => toggleMod(target, id), { disable: false }); break;
      case "toggleWorkshop": await run(target, () => queueWorkshopWrite(() => toggleWorkshop(id, target.dataset.enabled === "true")), { disable: false }); break;
      case "openModFolder": await run(target, () => request(`/api/mods/open-folder?id=${encodeURIComponent(id)}`), { disable: false }); break;
      case "openWorkshopFolder": await run(target, () => request(`/api/workshop/${encodeURIComponent(id)}/open-folder`), { disable: false }); break;
      case "openSteamWorkshop": await run(target, () => request(`/api/workshop/${encodeURIComponent(id)}/open-page`, { method: "POST", body: {} }), { disable: false }); break;
      case "rescanMods": beginModsWrite(); await request("/api/mods/resync", { method: "POST", body: {} }); await loadMods(); toast("重扫完成", "success"); break;
      case "deleteMod": state.pendingDeleteId = id; state.pendingDeleteForce = false; $("#deleteDetails").replaceChildren(); $("#deleteMessage").textContent = `确定删除“${state.mods.find((mod) => String(mod.id) === id)?.name || id}”吗？`; openModal($("#deleteModal")); break;
      case "useGamePath": $("#gamePathInput").value = target.dataset.path || ""; toast("已填入检测到的路径", "success"); break;
      case "openNexus": await run(target, () => openValidatedNexus(target), { disable: false }); break;
      case "copyNexusId": await run(target, () => copyNexusId(target), { disable: false }); break;
      case "revealAdult": revealAdultCard(target.closest(".nexus-card"), target); break;
      default: return;
    }
  } catch (error) { if (!["toggleMod", "openModFolder", "openNexus", "copyNexusId"].includes(target.dataset.dynamicAction)) toast(actionableErrorMessage(error), "error"); }
  finally {
    inFlightDynamicActions.delete(actionKey);
    target.disabled = false;
  }
}

function setupDropzone() {
  const zone = $("#dropzone");
  for (const name of ["dragenter", "dragover", "dragleave", "drop"]) zone.addEventListener(name, async (event) => {
    event.preventDefault();
    zone.classList.toggle("dragging", name === "dragenter" || name === "dragover");
    if (name === "drop") await executeModFileSelection(event.dataTransfer?.files?.[0] || null);
  });
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
  $("#workshopDependencyModal").addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelWorkshopDependency();
  });
  setupDropzone();
  try {
    const [health, appearance, game] = await Promise.all([request("/api/health"), request("/api/appearance"), request("/api/game/status")]);
    $("#healthStatus").textContent = `服务正常 · v${health.version || "-"}`;
    applyAppearance(appearance); refreshBackground();
    if (game.configured) showPathInfo(game);
    await loadMods();
  } catch (error) { $("#healthStatus").textContent = "服务连接失败"; toast(actionableErrorMessage(error), "error"); }
}

init();
