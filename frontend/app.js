import { rememberFocus, restoreFocus } from "./a11y.js";
import { ApiError, request } from "./api.js";
import { createEffects } from "./effects.js";
import { hydrateIcons } from "./icons.js";
import {
  actionableErrorMessage, createRevisionGuard, createSerialQueue, dynamicActionKey, nextModsGeneration,
  pendingUploadTokenAfterError, resetModFileSelectionState,
} from "./interaction-policy.js";
import { renderConflict, renderDetectedGames, renderMessage, renderMods, renderNexus, renderTrash, validatedNexusUrl } from "./render.js";
import { callWindowControl, chooseModFolder as chooseNativeModFolder, initializeWindowControls } from "./window-controls.js";
import { createImportQueue, deriveModView, reduceImportQueue } from "./ui-model.js";
import { setupFileDropzone } from './dropzone.js';

const $ = (selector, root = document) => root.querySelector(selector);
const state = {
  mods: [], nexus: [], credits: [], gamePath: "", selectedModFile: null, selectedSelectionToken: null,
  importQueue: [], activeImportId: null, pendingUploadToken: null,
  pendingDeleteId: null, pendingDeleteForce: false, pendingWorkshopDependency: null, updateInfo: null,
  trash: { items: [], invalid_records: [] }, ue4ssUpdateAvailable: false,
  modHealth: null, recovery: null, recoverySeenSessionId: null,
  expandedModValueId: null, modValueCapability: null, modValueLoading: false, modValueError: "",
  modsRequestSequence: 0, modsRequestGeneration: 0, modsRequestController: null,
  nexusRequestSequence: 0, nexusRequestController: null, nexusMode: "downloads",
  appearance: { theme: "aurora-glass", mask: 0.35, blur: 0, position: "center", petals: "medium", petal_style: "natural", background: "default" },
};
const effects = createEffects();
const appearanceRevisions = createRevisionGuard();
const backgroundWriteQueue = createSerialQueue();
const inFlightDynamicActions = new Set();
let workshopWriteQueue = Promise.resolve();
let recoveryPollTimer = null;
const VIEW_COPY = Object.freeze({
  mods: ["我的模组", "管理已安装的模组"], import: ["导入安装", "自动识别并安全安装 ZIP / PAK"],
  nexus: ["N网热门", "浏览 Nexus Mods 热门与最新内容"], settings: ["设置与外观", "游戏工具、更新与三套主题"],
  credits: ["开源致谢", "感谢开放源代码项目与社区资料"],
});
const UE4SS_WRITE_ACTIONS = new Set([
  "installUe4ss", "installUe4ssUpdate", "selectUe4ssZip", "repairUe4ss", "uninstallUe4ss", "repairModHealth", "rollbackRecovery",
]);
const LOCAL_INPUT_ACTIONS = new Set([
  "filterMods", "filterModSource", "filterModStatus", "changeImportType", "editImportName", "editImportNexusId",
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
  document.querySelectorAll(".nav-item").forEach((item) => {
    const current = item.dataset.view === name;
    item.classList.toggle("active", current);
    if (current) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const copy = VIEW_COPY[name];
  $("#viewTitle").textContent = copy[0];
  $("#viewSubtitle").textContent = copy[1];
  if (name === "mods") await loadMods();
  if (name === "nexus") await loadNexus(state.nexusMode, true);
  if (name === "settings") await loadSettings();
  if (name === "credits") await loadCredits();
}

function creditText(label, value) {
  const line = document.createElement("p");
  const heading = document.createElement("strong");
  heading.textContent = `${label}：`;
  line.append(heading, document.createTextNode(String(value || "未标注")));
  return line;
}

function openTrustedId(id) {
  return request("/api/system/open-trusted-link", { method: "POST", body: { id } });
}

function trustedLinkButton(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  button.dataset.dynamicAction = "openTrustedLink";
  button.dataset.id = item.id;
  button.setAttribute("aria-label", `在系统浏览器打开 ${item.name} 来源页面`);
  button.textContent = "打开项目来源";
  return button;
}

function renderCredits(items) {
  const core = document.createDocumentFragment();
  const dependencies = document.createDocumentFragment();
  for (const item of items) {
    if (item.core) {
      const card = document.createElement("article");
      card.className = "credit-card glass-panel";
      const title = document.createElement("h2");
      title.textContent = item.name;
      const purpose = document.createElement("p");
      purpose.className = "credit-purpose";
      purpose.textContent = item.purpose;
      card.append(title, purpose, creditText("作者/组织", item.author), creditText("许可证", item.license), creditText("版本/来源", item.version), trustedLinkButton(item));
      core.append(card);
    }
    if (item.direct_dependency) {
      const row = document.createElement("article");
      row.className = "credit-dependency";
      const title = document.createElement("h3");
      title.textContent = `${item.name} ${item.version}`;
      row.append(title, creditText("用途", item.purpose), creditText("作者/组织", item.author), creditText("许可证", item.license), creditText("许可说明", item.license_text), trustedLinkButton(item));
      dependencies.append(row);
    }
  }
  $("#creditsCore").replaceChildren(core);
  $("#creditsDependencies").replaceChildren(dependencies);
}

async function loadCredits() {
  const items = await request("/api/credits");
  state.credits = Array.isArray(items) ? items : [];
  renderCredits(state.credits);
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

function previewAppearance(settings) {
  appearanceRevisions.bump();
  applyAppearance(settings);
}

function refreshBackground() {
  const url = `/api/appearance/background/current?v=${Date.now()}`;
  document.documentElement.style.setProperty("--background-url", `url("${url}")`);
}

function completeBackgroundWrite(revision, saved, message) {
  appearanceRevisions.apply(revision, () => applyAppearance(saved));
  refreshBackground();
  toast(message, "success");
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
    const mods = await request("/api/mods", { signal: controller.signal, timeout: 120000 });
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

function renderModStats(stats) {
  $("#statTotal").textContent = String(stats.total);
  $("#statEnabled").textContent = String(stats.enabled);
  $("#statDisabled").textContent = String(stats.disabled);
  $("#statAbnormal").textContent = String(stats.abnormal);
}

function filterMods() {
  const view = deriveModView(state.mods, {
    query: $("#modFilter").value,
    source: $("#modSourceFilter").value,
    status: $("#modStatusFilter").value,
  });
  renderModStats(view.stats);
  $("#modResultCount").textContent = `${view.items.length} 项`;
  renderMods($("#modList"), view.items, {
    expandedId: state.expandedModValueId,
    capability: state.modValueCapability,
    loading: state.modValueLoading,
    error: state.modValueError,
  });
}

async function loadTrash({ open = false } = {}) {
  const payload = await request("/api/trash");
  state.trash = payload && typeof payload === "object" ? payload : { items: [], invalid_records: [] };
  const count = (state.trash.items?.length || 0) + (state.trash.invalid_records?.length || 0);
  $("#trashCount").textContent = String(count);
  renderTrash($("#trashList"), state.trash);
  if (open && !$("#trashModal").open) openModal($("#trashModal"));
  return state.trash;
}

async function restoreTrash(id) {
  $("#trashResult").textContent = "正在验证负载并恢复文件…";
  try {
    await request(`/api/trash/${encodeURIComponent(id)}/restore`, { method: "POST", body: {}, timeout: 120000 });
    $("#trashResult").textContent = "恢复完成";
    await Promise.all([loadTrash(), loadMods()]);
    toast("模组已从回收站恢复", "success");
  } catch (error) {
    $("#trashResult").textContent = `恢复失败：${actionableErrorMessage(error)}`;
    throw error;
  }
}

async function purgeTrash(id) {
  if (!window.confirm("此记录的文件将彻底删除且无法恢复。是否继续？")) return;
  $("#trashResult").textContent = "正在彻底删除回收负载…";
  await request(`/api/trash/${encodeURIComponent(id)}`, { method: "DELETE", timeout: 120000 });
  $("#trashResult").textContent = "记录已彻底删除";
  await loadTrash();
  toast("回收记录已彻底删除", "success");
}

async function unmanageMod(id) {
  const mod = state.mods.find((item) => String(item.id) === String(id));
  const name = mod?.name || id;
  if (!window.confirm(`取消管理“${name}”？游戏文件会保留，后续刷新不再接管；可在设置中重新发现。`)) return;
  beginModsWrite();
  await request(`/api/mods/${encodeURIComponent(id)}/unmanage`, { method: "POST", body: {} });
  await Promise.all([loadMods(), loadIgnoredMods()]);
  toast("已取消管理并保留游戏文件", "success");
}

function closeModValues() {
  state.expandedModValueId = null;
  state.modValueCapability = null;
  state.modValueLoading = false;
  state.modValueError = "";
  filterMods();
}

async function toggleModValues(id) {
  if (state.expandedModValueId === id) {
    closeModValues();
    return;
  }
  state.expandedModValueId = id;
  state.modValueCapability = null;
  state.modValueLoading = true;
  state.modValueError = "";
  filterMods();
  try {
    const capability = await request(`/api/mods/${encodeURIComponent(id)}/values`);
    if (state.expandedModValueId !== id) return;
    state.modValueCapability = capability;
    state.modValueLoading = false;
    filterMods();
    document.querySelector(`.mod-row[data-id="${CSS.escape(id)}"] .mod-value-input`)?.focus();
  } catch (error) {
    if (state.expandedModValueId === id) {
      state.modValueLoading = false;
      state.modValueError = actionableErrorMessage(error);
      filterMods();
    }
    throw error;
  }
}

async function saveModValues(id, target) {
  const card = target.closest(".mod-row");
  const editor = card?.querySelector(".mod-value-editor");
  if (!editor || state.expandedModValueId !== id || !state.modValueCapability) {
    throw new ApiError("数值编辑器状态已失效，请重新展开");
  }
  const values = {};
  for (const input of editor.querySelectorAll(".mod-value-input")) {
    if (!input.dataset.key || input.value.trim() === "") throw new ApiError("请填写所有数值字段");
    const value = Number(input.value);
    if (!Number.isFinite(value)) throw new ApiError("数值必须是有限数字");
    values[input.dataset.key] = value;
  }
  const resultNode = editor.querySelector(".mod-value-result");
  resultNode.textContent = "正在验证并原子保存配置…";
  try {
    const saved = await request(`/api/mods/${encodeURIComponent(id)}/values`, {
      method: "POST",
      body: { revision: state.modValueCapability.revision, values },
      timeout: 120000,
    });
    state.modValueCapability = saved;
    state.modValueError = "";
    await loadMods();
    toast("Mod 数值已保存并更新清单", "success");
  } catch (error) {
    state.modValueError = error instanceof ApiError && error.code === "mod_values_stale"
      ? "配置已被其他程序修改，请收起后重新加载当前值。"
      : actionableErrorMessage(error);
    filterMods();
    throw error;
  }
}

function isSupportedModFile(file) {
  return Boolean(file && /\.(zip|pak)$/i.test(file.name || ""));
}

function renderImportQueue() {
  const container = $("#importQueue");
  if (!state.importQueue.length) return renderMessage(container, "尚未选择文件", "muted");
  const fragment = document.createDocumentFragment();
  for (const item of state.importQueue) {
    const row = document.createElement("div");
    row.className = `import-queue-row state-${item.status}`;
    const name = document.createElement("strong");
    name.textContent = item.name;
    const status = document.createElement("span");
    status.textContent = ({ queued: "待安装", installing: "安装中", conflict: "等待冲突处理", succeeded: "安装成功", failed: "安装失败", cancelled: "已取消" })[item.status] || item.status;
    row.append(name, status);
    if (item.status === "failed") {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn compact";
      retry.dataset.importRetry = item.id;
      retry.textContent = "重试";
      row.append(retry);
    }
    fragment.append(row);
  }
  container.replaceChildren(fragment);
}

function resetModFileSelection() {
  const reset = resetModFileSelectionState(state);
  state.pendingUploadToken = reset.pendingUploadToken;
  state.selectedModFile = reset.selectedModFile;
  state.selectedSelectionToken = null;
  state.activeImportId = null;
  state.importQueue = [];
  $("#modFileInput").value = "";
  $("#selectedModFile").textContent = "尚未选择文件";
  $("#importResult").textContent = "";
  renderImportQueue();
}

function selectModFiles(files) {
  const values = Array.from(files || []);
  if (!values.length) return resetModFileSelection();
  if (values.some((file) => !isSupportedModFile(file))) throw new ApiError("仅支持 ZIP 或 PAK 文件");
  state.pendingUploadToken = null;
  state.importQueue = createImportQueue(values.map((file) => ({ name: file.name, size: file.size, file })));
  state.selectedModFile = null;
  state.selectedSelectionToken = null;
  $("#selectedModFile").textContent = `已选择 ${values.length} 个文件`;
  $("#importResult").textContent = "队列已就绪，将按顺序安全安装。";
  renderImportQueue();
}

function selectModFile(file) { return selectModFiles(file ? [file] : []); }

async function selectNativeFolder() {
  const items = await chooseNativeModFolder();
  if (items === null) throw new ApiError("当前窗口不支持原生文件夹选择，请使用“选择文件”");
  if (!items.length) { toast("所选文件夹没有顶层 ZIP 或 PAK 文件", "info"); return; }
  state.pendingUploadToken = null;
  state.importQueue = createImportQueue(items.map((item) => ({ ...item, name: item.name })));
  state.selectedModFile = null;
  state.selectedSelectionToken = null;
  $("#selectedModFile").textContent = `文件夹中发现 ${items.length} 个可安装文件`;
  $("#importResult").textContent = "队列已就绪，将按稳定文件名顺序安装。";
  renderImportQueue();
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

async function executeModFileSelection(files) {
  return executeFileOperation(() => selectModFiles(files));
}

async function importSelected(decision = "cancel") {
  const retryToken = state.pendingUploadToken;
  const selectionToken = state.selectedSelectionToken;
  let options;
  if (retryToken) {
    options = { method: "POST", body: { upload_token: retryToken, decision }, timeout: 120000 };
  } else if (selectionToken) {
    options = { method: "POST", body: {
      selection_token: selectionToken, decision, type: $("#importType").value,
      name: $("#importName").value.trim() || undefined,
      nexus_id: $("#importNexusId").value ? Number($("#importNexusId").value) : undefined,
    }, timeout: 120000 };
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
    const sourceName = state.selectedModFile?.name || state.importQueue.find((item) => item.id === state.activeImportId)?.name || "本地文件";
    $("#importResult").textContent = retryToken || ["replace", "keep_both"].includes(decision)
      ? "正在识别并安装：应用冲突处理…"
      : (state.selectedModFile ? `正在识别并安装：正在上传 ${state.selectedModFile.name}` : `正在识别并安装：${sourceName}`);
    const result = await request("/api/mods/import", options);
    state.pendingUploadToken = null;
    $("#importResult").textContent = `安装成功：${result.name || result.mod?.name || "模组"} · 类型 ${result.mod_type || result.kind || "已识别"}`;
    toast("模组安装成功", "success");
    await loadMods();
    return result;
  } catch (error) {
    const nextToken = pendingUploadTokenAfterError(retryToken, error);
    if (error instanceof ApiError && error.status === 409 && error.code === "mod_conflict" && (nextToken || selectionToken)) {
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

function transitionActiveImport(type, extra = {}) {
  if (!state.activeImportId) return;
  state.importQueue = reduceImportQueue(state.importQueue, { type, id: state.activeImportId, ...extra });
  renderImportQueue();
}

async function processImportQueue() {
  while (true) {
    const next = state.importQueue.find((item) => item.status === "queued");
    if (!next) {
      state.activeImportId = null;
      state.selectedModFile = null;
      state.selectedSelectionToken = null;
      $("#modFileInput").value = "";
      return;
    }
    state.activeImportId = next.id;
    state.selectedModFile = next.file || null;
    state.selectedSelectionToken = next.selection_token || null;
    transitionActiveImport("start");
    try {
      const result = await importSelected();
      if (result === null) {
        transitionActiveImport("conflict", { conflict: true });
        return;
      }
      transitionActiveImport("succeed", { result });
    } catch (error) {
      transitionActiveImport("fail", { error: actionableErrorMessage(error) });
      throw error;
    }
  }
}

async function resolveImportConflict(decision) {
  const modal = $("#conflictModal");
  if (modal.open) modal.close();
  $("#importResult").textContent = decision === "replace" ? "正在替换冲突文件…" : "正在保留两份并安装…";
  try {
    const result = await importSelected(decision);
    if (result === null) return;
    transitionActiveImport("succeed", { result });
    state.pendingUploadToken = null;
    state.selectedModFile = null;
    state.selectedSelectionToken = null;
    $("#importResult").textContent = `冲突处理完成：${result.name || result.mod?.name || "模组"} 已安装`;
    toast(decision === "replace" ? "已替换原文件并完成安装" : "已保留两者并完成安装", "success");
    await processImportQueue();
  } catch (error) {
    const feedback = document.createElement("p");
    feedback.className = "conflict-inline-error";
    feedback.textContent = `处理失败：${actionableErrorMessage(error)}。请确认游戏已退出后重试。`;
    $("#conflictDetails").prepend(feedback);
    $("#importResult").textContent = feedback.textContent;
    if (!modal.open) openModal(modal);
    throw error;
  }
}

function openModal(dialog) {
  rememberFocus();
  dialog.showModal();
  dialog.querySelector("[autofocus]")?.focus();
}

async function cancelImportConflict() {
  const uploadToken = state.pendingUploadToken;
  const selectionToken = state.selectedSelectionToken;
  beginModsWrite();
  try {
    if (uploadToken) await request("/api/mods/import", { method: "POST", body: { upload_token: uploadToken, decision: "cancel" } });
    else if (selectionToken) await request("/api/mods/import", { method: "POST", body: { selection_token: selectionToken, cancel: true } });
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 410 && ["upload_expired", "selection_expired"].includes(error.code))) throw error;
  } finally {
    transitionActiveImport("cancel");
    state.pendingUploadToken = null;
    state.selectedModFile = null;
    state.selectedSelectionToken = null;
    $("#conflictModal").close();
  }
  await processImportQueue();
}

async function deletePendingMod() {
  const id = state.pendingDeleteId;
  if (!id) return;
  $("#deleteModal").close();
  const suffix = state.pendingDeleteForce ? "?force_modified=true" : "";
  beginModsWrite();
  try {
    const removed = await request(`/api/mods/${encodeURIComponent(id)}${suffix}`, { method: "DELETE" });
    state.pendingDeleteId = null;
    state.pendingDeleteForce = false;
    await Promise.all([loadMods(), loadTrash()]);
    const expiry = removed.expires_at ? new Date(removed.expires_at).toLocaleDateString("zh-CN") : "30 天后";
    toast(`模组已移入回收站，保留至 ${expiry}`, "success");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && error.code === "modified_files" && !state.pendingDeleteForce) {
      state.pendingDeleteForce = true;
      $("#deleteMessage").textContent = "检测到已修改文件。再次确认会将实际修改内容一并移入 PalDeck 回收站并保留 30 天。";
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
      toast("Workshop 设置已保存，将在下次启动生效；检测到无法安全自动清理的 Workshop 事务文件，已隔离保留，请勿手动删除游戏文件。", "warning");
    } else {
      toast(enabled ? "Workshop 模组已启用，将在下次启动生效" : "Workshop 模组已禁用，将在下次启动生效", "success");
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

function updateShellStatus({ version, path, healthy } = {}) {
  if (version) {
    $("#sidebarVersion").textContent = `v${version}`;
    $("#healthStatus").textContent = `${healthy === false ? "服务异常" : "运行中"} · v${version}`;
  } else if (healthy === false) {
    $("#healthStatus").textContent = "服务连接失败";
  }
  if (path !== undefined) $("#pathChip").textContent = path || "未配置游戏目录";
}

function showPathInfo(status) {
  state.gamePath = status.path || state.gamePath;
  updateShellStatus({ path: state.gamePath });
  $("#gamePathInput").value = state.gamePath;
  $("#pathInfo").textContent = status.valid ? `目录有效${status.has_ue4ss ? " · UE4SS 已安装" : " · UE4SS 未安装"}` : "目录尚未配置";
}

async function loadUe4ssStatus() {
  if (!state.gamePath) { $("#ue4ssStatus").textContent = "请先配置游戏目录"; return null; }
  const status = await request("/api/ue4ss/status");
  const integrityLabels = {
    not_installed: "未安装", unmanaged: "外部安装", healthy: "正常",
    missing: "文件缺失", modified: "文件已修改", conflict: "路径冲突",
  };
  $("#ue4ssStatus").textContent = status.installed
    ? (status.managed ? "UE4SS 已由 PalDeck 管理" : "检测到外部 UE4SS")
    : "UE4SS 未安装";
  $("#ue4ssIntegrity").textContent = integrityLabels[status.integrity] || "未知";
  $("#ue4ssOwnedFiles").textContent = String(status.owned_files || 0);
  $("#ue4ssAbnormalFiles").textContent = String((status.missing_files || 0) + (status.modified_files || 0) + (status.conflict_files || 0));
  $("#installUe4ssButton").hidden = status.installed === true;
  $("#repairUe4ssButton").hidden = status.repair_available !== true;
  $("#uninstallUe4ssButton").hidden = status.uninstall_available !== true;
  const asset = status.bundled?.asset;
  $("#ue4ssUpdatedAt").textContent = asset?.updated_at ? `内置更新：${asset.updated_at}` : "内置资源不可用";
  $("#ue4ssDigest").textContent = asset?.sha256 ? `SHA-256：${asset.sha256.slice(0, 10)}` : "";
  return status;
}

async function loadIgnoredMods() {
  if (!state.gamePath) { $("#ignoredModsCount").textContent = "0"; return { count: 0, items: [] }; }
  const ignored = await request("/api/mods/ignored");
  $("#ignoredModsCount").textContent = String(ignored.count || 0);
  return ignored;
}

async function resetIgnoredMods() {
  const count = Number($("#ignoredModsCount").textContent) || 0;
  if (!count) { toast("当前没有已忽略的外部模组", "info"); return; }
  if (!window.confirm(`重新发现 ${count} 个已忽略 Mod？PalDeck 将立即安全重扫游戏目录。`)) return;
  const result = await request("/api/mods/ignored/reset", { method: "POST", body: {}, timeout: 120000 });
  state.mods = Array.isArray(result.mods) ? result.mods : [];
  filterMods();
  $("#ignoredModsCount").textContent = "0";
  toast(`已重新发现 ${result.rediscovered || 0} 个外部模组`, "success");
}

async function repairUe4ss() {
  const output = $("#ue4ssResult");
  const invoke = (confirmReplace) => request("/api/ue4ss/repair", {
    method: "POST", body: confirmReplace ? { confirm_replace: true } : {}, timeout: 120000,
  });
  output.textContent = "正在检查 UE4SS 完整性…";
  try {
    let result;
    try { result = await invoke(false); }
    catch (error) {
      if (!(error instanceof ApiError) || error.code !== "ue4ss_repair_conflict") throw error;
      const abnormal = (error.details?.modified?.length || 0) + (error.details?.conflicts?.length || 0);
      if (!window.confirm(`检测到 ${abnormal || "若干"} 个需要替换的 UE4SS 文件。修复会保留用户 Mod 和配置，是否继续？`)) return;
      output.textContent = "正在备份并修复 UE4SS 核心文件…";
      result = await invoke(true);
    }
    output.textContent = result.unchanged ? "UE4SS 完整性正常，无需修改" : "UE4SS 修复完成，用户 Mod 与配置已保留";
    const refreshes = await Promise.allSettled([loadUe4ssStatus(), loadMods()]);
    if (refreshes.some((item) => item.status === "rejected")) {
      output.textContent = "UE4SS 修复已完成，但状态刷新失败；请稍后手动刷新";
      toast("UE4SS 已修复，状态刷新稍后重试", "warning");
      return;
    }
    toast("UE4SS 检查与修复完成", "success");
  } catch (error) {
    output.textContent = `UE4SS 修复失败：${actionableErrorMessage(error)}`;
    throw error;
  }
}

async function uninstallUe4ss() {
  const output = $("#ue4ssResult");
  if (!window.confirm("安全卸载只会将 PalDeck 识别的 UE4SS 核心文件移入回收站；用户 Mod 和配置会保留。是否继续？")) return;
  const invoke = (confirmModified) => request("/api/ue4ss/uninstall", {
    method: "POST", body: confirmModified ? { confirm_modified: true } : {}, timeout: 120000,
  });
  output.textContent = "正在审计并回收 UE4SS 核心文件…";
  try {
    let result;
    try { result = await invoke(false); }
    catch (error) {
      if (!(error instanceof ApiError) || error.code !== "ue4ss_modified_files") throw error;
      if (!window.confirm(`检测到 ${error.details?.file_count || "若干"} 个已修改核心文件。确认后实际内容也会进入回收站，是否继续？`)) return;
      result = await invoke(true);
    }
    output.textContent = "UE4SS 已安全卸载，可在回收站恢复";
    await Promise.all([loadUe4ssStatus(), loadTrash(), loadMods()]);
    toast(`UE4SS 已移入回收站（${String(result.trash_id || "").slice(0, 8)}）`, "success");
  } catch (error) {
    output.textContent = `UE4SS 卸载失败：${actionableErrorMessage(error)}`;
    throw error;
  }
}

function renderModHealth(report) {
  state.modHealth = report && typeof report === "object" ? report : null;
  const summary = state.modHealth?.summary || {};
  $("#modHealthHealthy").textContent = String(summary.healthy_mods || 0);
  $("#modHealthAbnormal").textContent = String(summary.abnormal_mods || 0);
  $("#modHealthInvalid").textContent = String(summary.invalid_manifests || 0);
  $("#modHealthSafe").textContent = String(summary.safe_actions || 0);
  $("#modHealthState").textContent = state.modHealth?.status === "healthy" ? "状态正常" : "需要处理";

  const fragment = document.createDocumentFragment();
  const abnormal = Array.isArray(state.modHealth?.mods)
    ? state.modHealth.mods.filter((item) => !["enabled", "disabled"].includes(item.status))
    : [];
  for (const item of abnormal.slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "mod-health-item warning";
    row.textContent = `${item.name || item.id} · ${item.status} · 安全修复 ${item.safe_actions || 0} · 需确认 ${item.confirmation_actions || 0} · 阻塞 ${item.blocked || 0}`;
    fragment.append(row);
  }
  for (const item of (state.modHealth?.invalid_manifests || []).slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "mod-health-item warning";
    row.textContent = `清单 ${item.id} · ${item.reason}${item.backup_available ? " · 可从备份恢复" : " · 无可用备份"}`;
    fragment.append(row);
  }
  if (!fragment.childNodes.length) {
    const row = document.createElement("div");
    row.className = "mod-health-item";
    row.textContent = "所有受管 Mod 和清单均通过检查。";
    fragment.append(row);
  }
  $("#modHealthList").replaceChildren(fragment);
}

async function loadModHealth() {
  $("#modHealthResult").textContent = "正在逐文件检查完整性…";
  const report = await request("/api/mods/health", { timeout: 120000 });
  renderModHealth(report);
  $("#modHealthResult").textContent = report.status === "healthy"
    ? "检查完成：当前没有异常。"
    : "检查完成：请查看异常项目；无损项可自动修复。";
  return report;
}

async function repairModHealth() {
  $("#modHealthResult").textContent = "正在创建健康副本并执行无损修复…";
  beginModsWrite();
  const result = await request("/api/mods/repair-safe", {
    method: "POST", body: {}, timeout: 120000,
  });
  renderModHealth(result.report);
  await loadMods();
  const repaired = Array.isArray(result.repaired) ? result.repaired.length : 0;
  const restored = Array.isArray(result.restored_manifests) ? result.restored_manifests.length : 0;
  $("#modHealthResult").textContent = `安全修复完成：处理 ${repaired} 个 Mod，恢复 ${restored} 个损坏清单；用户修改未被覆盖。`;
  toast("Mod 健康中心安全修复完成", result.ok === false ? "warning" : "success");
}

function renderRecoveryStatus(status) {
  state.recovery = status && typeof status === "object" ? status : null;
  const pending = state.recovery?.pending_assessment;
  const plan = state.recovery?.recovery_plan;
  const stableButton = $("#recoveryMarkStable");
  const faultButton = $("#recoveryMarkFault");
  const rollbackButton = $("#recoveryRollback");
  stableButton.hidden = !pending || pending.outcome !== "pending";
  faultButton.hidden = !pending || pending.outcome !== "pending";
  rollbackButton.hidden = !plan?.available;

  let badge = "等待监测";
  let message = "等待下一次 Palworld 游戏会话。";
  if (state.recovery?.running) {
    badge = "游戏运行中";
    message = "正在记录本次会话；运行期间不会修改任何 Mod。";
  } else if (pending?.outcome === "pending") {
    badge = "等待确认";
    message = "检测到游戏已经退出。请确认本次运行是否正常。";
  } else if (pending?.outcome === "fault") {
    badge = "故障待恢复";
    message = plan?.action_count
      ? `找到 ${plan.action_count} 个相对上次稳定状态新启用的 Mod，可在确认后禁用。`
      : "没有找到可安全自动回滚的新启用 Mod，请结合日志逐项排查。";
  } else if (pending?.outcome === "rolled_back") {
    badge = "已执行回滚";
    message = "可疑的新启用 Mod 已禁用；请重新启动游戏验证。";
  }
  $("#recoveryState").textContent = badge;
  $("#recoveryResult").textContent = message;

  const fragment = document.createDocumentFragment();
  const items = plan?.actions?.length ? plan.actions : (pending?.changes || []);
  for (const item of items.slice(0, 8)) {
    const row = document.createElement("div");
    row.className = "mod-health-item warning";
    const source = item.source === "workshop" ? "Workshop" : "本地";
    const change = "to_enabled" in item
      ? "将回滚为禁用"
      : (item.after_enabled ? "本次会话前已启用" : "本次会话前已禁用");
    row.textContent = `${item.name || item.id} · ${source} · ${change}`;
    fragment.append(row);
  }
  for (const item of (plan?.blocked || []).slice(0, 4)) {
    const row = document.createElement("div");
    row.className = "mod-health-item warning";
    row.textContent = `${item.name || item.id} · 当前文件状态异常，不能自动回滚`;
    fragment.append(row);
  }
  if (!fragment.childNodes.length && pending) {
    const row = document.createElement("div");
    row.className = "mod-health-item";
    row.textContent = "本次会话与上次稳定状态之间没有检测到 Mod 启停变化。";
    fragment.append(row);
  }
  $("#recoveryList").replaceChildren(fragment);
}

async function loadRecoveryStatus({ notify = false } = {}) {
  const status = await request("/api/recovery/status", { timeout: 30000 });
  const pendingId = status?.pending_assessment?.id || null;
  if (notify && pendingId && pendingId !== state.recoverySeenSessionId
      && status.pending_assessment.outcome === "pending") {
    toast("检测到 Palworld 已退出，请在设置中的“游戏故障恢复”确认本次结果", "info");
  }
  if (pendingId) state.recoverySeenSessionId = pendingId;
  renderRecoveryStatus(status);
  return status;
}

async function assessRecovery(outcome) {
  const status = await request("/api/recovery/assess", {
    method: "POST", body: { outcome }, timeout: 30000,
  });
  renderRecoveryStatus(status);
  toast(outcome === "stable" ? "已记录为稳定状态" : "已生成故障恢复计划", outcome === "stable" ? "success" : "warning");
}

async function rollbackRecovery() {
  const plan = state.recovery?.recovery_plan;
  if (!plan?.available) {
    toast("当前没有可安全回滚的新启用 Mod", "warning");
    return;
  }
  if (!window.confirm(`将禁用 ${plan.action_count} 个相对上次稳定状态新启用的 Mod。不会删除文件，是否继续？`)) return;
  beginModsWrite();
  const result = await request("/api/recovery/rollback", {
    method: "POST",
    body: { revision: plan.revision, confirm: true },
    timeout: 120000,
  });
  renderRecoveryStatus(result.status);
  await loadMods();
  toast(`已回滚 ${result.executed_count} 个新启用 Mod，请重新启动游戏验证`, "success");
}

function startRecoveryMonitor() {
  if (recoveryPollTimer !== null) clearTimeout(recoveryPollTimer);
  const poll = async () => {
    try { await loadRecoveryStatus({ notify: true }); }
    catch { /* Monitoring is advisory; regular API errors remain independently visible. */ }
    recoveryPollTimer = setTimeout(poll, 5000);
  };
  poll();
}

function showModRepairPlan(plan) {
  const notice = $("#modConflictNotice");
  const title = document.createElement("strong");
  title.textContent = `${plan.name || "Mod"} · 修复诊断`;
  const summary = document.createElement("p");
  summary.textContent = `安全操作 ${plan.safe_actions || 0}，需确认操作 ${plan.confirmation_actions || 0}，无法自动处理 ${plan.blocked?.length || 0}。`;
  const details = document.createElement("div");
  for (const item of (plan.blocked || []).slice(0, 6)) {
    const line = document.createElement("small");
    line.className = "mod-file-health";
    const reason = item.reason === "trusted_source_missing" ? "缺少可信修复源" : "路径不安全或冲突";
    line.textContent = `${item.relative_path || "受管路径"}：${reason}`;
    details.append(line);
  }
  notice.replaceChildren(title, summary, details);
  notice.hidden = false;
}

async function repairMod(id) {
  const plan = await request(`/api/mods/${encodeURIComponent(id)}/repair-plan`, { timeout: 120000 });
  showModRepairPlan(plan);
  if (!plan.repairable) {
    toast("当前没有可自动执行的修复，请重新选择原始安装包或检查路径", "warning");
    return;
  }
  const confirmReplace = Number(plan.confirmation_actions || 0) > 0;
  if (confirmReplace && !window.confirm(`检测到 ${plan.confirmation_actions} 个被修改或冲突的文件。PalDeck 会先隔离原文件，再恢复健康副本。是否继续？`)) return;
  beginModsWrite();
  try {
    const result = await request(`/api/mods/${encodeURIComponent(id)}/repair`, {
      method: "POST",
      body: { revision: plan.revision, ...(confirmReplace ? { confirm_replace: true } : {}) },
      timeout: 120000,
    });
    await loadMods();
    if (result.complete) {
      $("#modConflictNotice").hidden = true;
      toast("Mod 已修复并通过完整性校验", "success");
    } else {
      toast("已完成可执行项目，仍有文件需要原始安装包", "warning");
    }
  } catch (error) {
    if (error instanceof ApiError && error.code === "mod_repair_stale" && error.details) {
      showModRepairPlan(error.details);
    }
    throw error;
  }
}

async function loadSettings() {
  const status = await request("/api/game/status");
  if (status.configured) showPathInfo(status);
  if (status.configured) await Promise.all([loadUe4ssStatus(), loadIgnoredMods(), loadModHealth(), loadRecoveryStatus()]);
}

async function installWithUe4ssConfirmation(operation) {
  try {
    return await operation(false);
  } catch (error) {
    const replaceable = error.code === "ue4ss_conflict" && error.details?.markers;
    if (replaceable && window.confirm("检测到已有 UE4SS 安装。是否确认替换？")) {
      return operation(true);
    }
    throw error;
  }
}

async function installFixedUe4ss(endpoint) {
  const output = $("#ue4ssResult");
  output.textContent = endpoint === "/api/ue4ss/install-bundled"
    ? "正在安装内置 UE4SS…" : "正在安装 UE4SS 更新…";
  try {
    const result = await installWithUe4ssConfirmation((confirmReplace) => request(endpoint, {
      method: "POST", body: confirmReplace ? { confirm_replace: true } : {}, timeout: 120000,
    }));
    output.textContent = result.message || "UE4SS 安装完成";
    if (endpoint === "/api/ue4ss/install-upstream") {
      state.ue4ssUpdateAvailable = false;
      $("#installUe4ssUpdate").hidden = true;
    }
    await loadUe4ssStatus();
    toast("UE4SS 安装完成", "success");
  } catch (error) {
    output.textContent = `UE4SS 安装失败：${actionableErrorMessage(error)}`;
    throw error;
  }
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

async function checkApplicationUpdate() {
  state.updateInfo = await request("/api/update/check", { timeout: 30000 });
  $("#updateStatus").textContent = state.updateInfo.update_available
    ? `发现新版本 ${state.updateInfo.remote_version}`
    : `已是最新版 ${state.updateInfo.local_version}`;
  return state.updateInfo;
}

async function checkUe4ssUpstream() {
  const result = await request("/api/ue4ss/check-upstream", { method: "POST", body: {}, timeout: 30000 });
  state.ue4ssUpdateAvailable = result.update_available === true;
  $("#installUe4ssUpdate").hidden = !state.ue4ssUpdateAvailable;
  $("#ue4ssResult").textContent = state.ue4ssUpdateAvailable
    ? `发现新版 ${result.asset.updated_at} · ${result.asset.sha256.slice(0, 10)}`
    : "GitHub 版本与内置版本相同";
}

function chooseTheme(event) { previewAppearance({ theme: event.currentTarget.dataset.themeValue }); }
function choosePosition(event) { previewAppearance({ position: event.currentTarget.dataset.position }); }
function choosePetalStyle(event) { previewAppearance({ petal_style: event.currentTarget.dataset.petalStyle }); }
function noop() { /* Text and select controls keep their native editable behavior. */ }

export const ACTION_HANDLERS = Object.freeze({
  showMods: async () => switchView("mods"),
  showImport: async () => switchView("import"),
  showNexus: async () => switchView("nexus"),
  showSettings: async () => switchView("settings"),
  showCredits: async () => switchView("credits"),
  openPalDeckHome: async () => openTrustedId("paldeck-home"),
  openPalDeckIssues: async () => openTrustedId("paldeck-issues"),
  openPalDeckLicense: async () => openTrustedId("paldeck-license"),
  showAppearance: async () => { await switchView("settings"); $(".appearance-panel")?.scrollIntoView({ block: "start" }); },
  showSettingsStatus: async () => switchView("settings"),
  openGameFolder: async () => request("/api/mods/open-folder"),
  checkUpdateSidebar: async () => checkApplicationUpdate(),
  windowMinimize: async () => callWindowControl("minimize"),
  windowMaximize: async () => callWindowControl("toggle_maximize"),
  windowClose: async () => callWindowControl("close"),
  restartAdmin: async () => { await request("/api/system/restart-admin", { method: "POST", body: {} }); toast("正在请求管理员权限", "success"); },
  showTrash: async () => loadTrash({ open: true }),
  closeTrash: () => $("#trashModal").close(),
  refreshMods: async () => {
    beginModsWrite();
    await request("/api/mods/resync", { method: "POST", body: {}, timeout: 120000 });
    await loadMods();
    toast("已重新扫描游戏模组目录", "success");
  },
  openModsFolder: async () => request("/api/mods/open-folder"),
  filterMods: () => filterMods(),
  filterModSource: () => filterMods(),
  filterModStatus: () => filterMods(),
  chooseModFile: () => $("#modFileInput").click(),
  chooseModFolder: async () => selectNativeFolder(),
  selectModFile: async (event) => executeModFileSelection(event.currentTarget.files || []),
  changeImportType: noop,
  editImportName: noop,
  editImportNexusId: noop,
  importMod: async () => processImportQueue(),
  editNexusQuery: noop,
  searchNexus: async () => loadNexus("search", true),
  focusNexusSearch: () => $("#nexusQuery").focus(),
  downloadsNexus: async () => loadNexus("downloads", true),
  endorsementsNexus: async () => loadNexus("endorsements", true),
  refreshNexus: async () => loadNexus(state.nexusMode, true),
  latestNexus: async () => loadNexus("latest", true),
  editGamePath: noop,
  autoDetectGame: async () => { const data = await request("/api/game/detect"); renderDetectedGames($("#detectList"), data.installs || []); },
  saveGamePath: async () => { const path = $("#gamePathInput").value.trim(); const result = await request("/api/game/set", { method: "POST", body: { path } }); showPathInfo({ ...result, path: result.game_path || path, valid: true }); state.recoverySeenSessionId = null; startRecoveryMonitor(); toast("游戏路径已保存", "success"); },
  repairFolders: async () => { await request("/api/game/ensure-folders", { method: "POST", body: {} }); toast("模组目录已修复", "success"); },
  checkModHealth: async () => loadModHealth(),
  repairModHealth: async () => repairModHealth(),
  markRecoveryStable: async () => assessRecovery("stable"),
  markRecoveryFault: async () => assessRecovery("fault"),
  rollbackRecovery: async () => rollbackRecovery(),
  installUe4ss: async () => installFixedUe4ss("/api/ue4ss/install-bundled"),
  repairUe4ss: async () => repairUe4ss(),
  uninstallUe4ss: async () => uninstallUe4ss(),
  checkUe4ss: async () => checkUe4ssUpstream(),
  installUe4ssUpdate: async () => installFixedUe4ss("/api/ue4ss/install-upstream"),
  chooseUe4ssZip: () => $("#ue4ssZipInput").click(),
  selectUe4ssZip: async (event) => run(event.currentTarget, () => executeFileOperation(() => installZip(event.currentTarget.files?.[0])), { disable: false, global: true, busyText: "正在安装 UE4SS…" }),
  checkUpdate: async () => checkApplicationUpdate(),
  applyUpdate: async () => { const result = await request("/api/update/apply", { method: "POST", body: {}, timeout: 120000 }); toast(result.message || "更新已准备，将自动重启", "success"); },
  themeAurora: chooseTheme,
  themeIvory: chooseTheme,
  themeStarlit: chooseTheme,
  chooseBackground: () => $("#backgroundInput").click(),
  resetBackground: async () => {
    const revision = appearanceRevisions.bump();
    return backgroundWriteQueue.enqueue(async () => {
      const saved = await request("/api/appearance/background", { method: "DELETE" });
      completeBackgroundWrite(revision, saved, "已恢复默认背景");
    });
  },
  selectBackground: async (event) => executeFileOperation(async () => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const revision = appearanceRevisions.bump();
    return backgroundWriteQueue.enqueue(async () => {
      const saved = await request("/api/appearance/background", { method: "POST", body: form, timeout: 60000 });
      completeBackgroundWrite(revision, saved, "背景已更新");
    });
  }),
  changeMask: (event) => previewAppearance({ mask: Number(event.currentTarget.value) / 100 }),
  changeBlur: (event) => previewAppearance({ blur: Number(event.currentTarget.value) }),
  positionTopLeft: choosePosition,
  positionTopCenter: choosePosition,
  positionTopRight: choosePosition,
  positionCenterLeft: choosePosition,
  positionCenter: choosePosition,
  positionCenterRight: choosePosition,
  positionBottomLeft: choosePosition,
  positionBottomCenter: choosePosition,
  positionBottomRight: choosePosition,
  changePetals: (event) => previewAppearance({ petals: event.currentTarget.value }),
  petalStyleNatural: choosePetalStyle,
  petalStyleWatercolor: choosePetalStyle,
  petalStyleMinimal: choosePetalStyle,
  resetIgnoredMods: async () => resetIgnoredMods(),
  saveAppearance: async () => {
    const revision = appearanceRevisions.capture();
    const { theme, mask, blur, position, petals, petal_style } = state.appearance;
    try {
      const saved = await request("/api/appearance", { method: "POST", body: { theme, mask, blur, position, petals, petal_style } });
      if (appearanceRevisions.apply(revision, () => applyAppearance(saved))) toast("外观已保存", "success");
    } catch (error) {
      if (appearanceRevisions.capture() === revision) {
        const recovered = await request("/api/appearance");
        appearanceRevisions.apply(revision, () => applyAppearance(recovered));
      }
      throw error;
    }
  },
  cancelConflict: async () => cancelImportConflict(),
  replaceConflict: async () => resolveImportConflict("replace"),
  keepBothConflict: async () => resolveImportConflict("keep_both"),
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
      case "repairMod": await run(target, () => repairMod(id), { disable: false, global: true, busyText: "正在诊断并修复 Mod…" }); break;
      case "deleteMod": state.pendingDeleteId = id; state.pendingDeleteForce = false; $("#deleteDetails").replaceChildren(); $("#deleteMessage").textContent = `“${state.mods.find((mod) => String(mod.id) === id)?.name || id}”的受管文件将移入 PalDeck 回收站并保留 30 天。`; openModal($("#deleteModal")); break;
      case "unmanageMod": await unmanageMod(id); break;
      case "restoreTrash": await restoreTrash(id); break;
      case "purgeTrash": await purgeTrash(id); break;
      case "toggleModValues": await toggleModValues(id); break;
      case "saveModValues": await saveModValues(id, target); break;
      case "cancelModValues": closeModValues(); break;
      case "useGamePath": $("#gamePathInput").value = target.dataset.path || ""; toast("已填入检测到的路径", "success"); break;
      case "openNexus": await run(target, () => openValidatedNexus(target), { disable: false }); break;
      case "copyNexusId": await run(target, () => copyNexusId(target), { disable: false }); break;
      case "openTrustedLink": await run(target, () => request("/api/system/open-trusted-link", { method: "POST", body: { id } }), { disable: false }); break;
      default: return;
    }
  } catch (error) { if (!["toggleMod", "openModFolder", "openNexus", "copyNexusId", "openTrustedLink"].includes(target.dataset.dynamicAction)) toast(actionableErrorMessage(error), "error"); }
  finally {
    inFlightDynamicActions.delete(actionKey);
    target.disabled = false;
  }
}

function setupImportQueue() {
  $("#importQueue").addEventListener("click", async (event) => {
    const retry = event.target.closest("[data-import-retry]");
    if (!retry) return;
    state.importQueue = reduceImportQueue(state.importQueue, { type: "retry", id: retry.dataset.importRetry });
    renderImportQueue();
    try { await run(retry, processImportQueue); } catch { /* run displayed the error */ }
  });
}

function setupDropzone() {
  const zone = $("#dropzone");
  setupFileDropzone(zone, executeModFileSelection, {
    onEmpty: () => toast('没有读取到可拖放的文件，请改用“选择文件”', 'error'),
  });
}

async function init() {
  hydrateIcons();
  initializeWindowControls();
  document.addEventListener("click", dispatchStatic);
  document.addEventListener("input", dispatchStatic);
  document.addEventListener("change", dispatchStatic);
  $("#modList").addEventListener("click", handleDynamicAction);
  $("#modList").addEventListener("change", handleDynamicAction);
  $("#trashList").addEventListener("click", handleDynamicAction);
  $("#detectList").addEventListener("click", handleDynamicAction);
  $("#nexusGrid").addEventListener("click", handleDynamicAction);
  $("#creditsCore").addEventListener("click", handleDynamicAction);
  $("#creditsDependencies").addEventListener("click", handleDynamicAction);
  for (const dialog of [$("#conflictModal"), $("#deleteModal"), $("#trashModal"), $("#workshopDependencyModal")]) {
    dialog.addEventListener("close", restoreFocus);
  }
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
  setupImportQueue();
  setupDropzone();
  try {
    const [health, appearance, game] = await Promise.all([request("/api/health"), request("/api/appearance"), request("/api/game/status")]);
    updateShellStatus({ version: health.version || "-", path: game.game_path || game.path || "", healthy: true });
    $("#creditsVersion").textContent = `v${health.version || "-"}`;
    applyAppearance(appearance); refreshBackground();
    if (game.configured) showPathInfo(game);
    await Promise.all([loadMods(), game.configured ? loadTrash() : Promise.resolve()]);
    if (game.configured) startRecoveryMonitor();
  } catch (error) { updateShellStatus({ healthy: false }); toast(actionableErrorMessage(error), "error"); }
}

init();
