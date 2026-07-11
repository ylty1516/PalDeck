const text = (value) => document.createTextNode(String(value ?? ""));

export function validatedNexusUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  const host = url.hostname.toLocaleLowerCase();
  const trustedHost = host === "nexusmods.com" || host.endsWith(".nexusmods.com");
  if (url.protocol !== "https:" || !trustedHost || !/^\/palworld\/mods\/\d+\/?$/.test(url.pathname)) return null;
  return url;
}

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content);
  return node;
}

function actionButton(label, action, className = "btn") {
  const button = el("button", className, label);
  button.type = "button";
  button.dataset.dynamicAction = action;
  return button;
}

export function renderMessage(container, message, className = "empty-state") {
  container.replaceChildren(el("div", className, message));
}

const MOD_STATUS = Object.freeze({
  enabled: { label: "已启用", integrity: "完整性正常" },
  disabled: { label: "已禁用", integrity: "完整性正常" },
  modified: { label: "文件已修改", integrity: "完整性异常" },
  missing: { label: "文件缺失", integrity: "完整性异常" },
  conflict: { label: "文件冲突", integrity: "完整性异常" },
});

export function renderMods(container, mods) {
  if (!mods.length) return renderMessage(container, "暂无模组，请从“导入安装”添加。", "empty-state glass-panel");
  const fragment = document.createDocumentFragment();
  for (const mod of mods) {
    const status = MOD_STATUS[mod.status || mod.audit?.status] ? (mod.status || mod.audit.status) : (mod.enabled ? "enabled" : "disabled");
    const statusInfo = MOD_STATUS[status];
    const toggleAllowed = status === "enabled" || status === "disabled";
    const card = el("article", `mod-card glass-panel status-${status}${status === "disabled" ? " disabled-mod" : ""}`);
    card.dataset.id = String(mod.id);
    const main = el("div", "mod-main");
    const title = el("h3", "mod-title", mod.name || "未命名模组");
    const badge = el("span", "badge", mod.mod_type || "unknown");
    title.append(" ", badge);
    main.append(title);
    const meta = el("p", "mod-meta");
    meta.append(text(statusInfo.label), text(` · ${formatBytes(mod.size_bytes)}`));
    if (mod.nexus_id != null) meta.append(text(` · N#${mod.nexus_id}`));
    const fileCount = Array.isArray(mod.manifest_files) ? mod.manifest_files.length : (Array.isArray(mod.files) ? mod.files.length : 0);
    const integrity = el("p", `mod-integrity ${toggleAllowed ? "ok" : "warning"}`, `${statusInfo.integrity} · ${fileCount} 个受管文件`);
    main.append(meta, integrity, el("p", "mod-path", mod.install_path || ""));
    if (!toggleAllowed) main.append(el("p", "repair-hint", "请修复文件或重扫；异常状态不可启停。"));
    const actions = el("div", "button-row");
    const toggle = actionButton(status === "enabled" ? "禁用" : "启用", "toggleMod", "btn");
    toggle.dataset.id = String(mod.id);
    toggle.dataset.enabled = String(status !== "enabled");
    toggle.disabled = !toggleAllowed;
    const open = actionButton("文件夹", "openModFolder", "btn");
    open.dataset.id = String(mod.id);
    const remove = actionButton("删除", "deleteMod", "btn danger");
    remove.dataset.id = String(mod.id);
    actions.append(toggle, open);
    if (!toggleAllowed) {
      const rescan = actionButton("重扫", "rescanMods", "btn");
      rescan.dataset.id = String(mod.id);
      actions.append(rescan);
    }
    actions.append(remove);
    card.append(main, actions);
    fragment.append(card);
  }
  container.replaceChildren(fragment);
}

export function renderNexus(container, mods) {
  if (!mods.length) return renderMessage(container, "没有找到相关模组。", "empty-state glass-panel");
  const fragment = document.createDocumentFragment();
  for (const mod of mods) {
    const card = el("article", "nexus-card glass-panel");
    const image = el("div", "nexus-image");
    if (typeof mod.picture_url === "string" && /^https?:\/\//i.test(mod.picture_url)) {
      image.style.backgroundImage = `url(${JSON.stringify(mod.picture_url)})`;
    }
    image.append(el("span", "badge", `#${mod.mod_id ?? mod.nexus_id ?? "-"}`));
    const body = el("div", "nexus-body");
    body.append(el("h3", "mod-title", mod.name || "未命名"), el("p", "nexus-summary", mod.summary || "暂无简介"));
    const meta = el("p", "mod-meta", `下载 ${Number(mod.downloads || 0).toLocaleString("zh-CN")} · 作者 ${mod.author || "?"}`);
    body.append(meta);
    const actions = el("div", "button-row");
    const id = String(mod.mod_id ?? mod.nexus_id ?? "");
    const open = actionButton("打开 N 网", "openNexus", "btn");
    open.dataset.url = typeof mod.url === "string" ? mod.url : `https://www.nexusmods.com/palworld/mods/${encodeURIComponent(id)}`;
    const copy = actionButton("复制尾号", "copyNexusId", "btn");
    copy.dataset.id = id;
    copy.append(text(` #${id}`));
    actions.append(open, copy);
    body.append(actions);
    card.append(image, body);
    fragment.append(card);
  }
  container.replaceChildren(fragment);
}

export function renderDetectedGames(container, installs) {
  if (!installs.length) return renderMessage(container, "未自动找到游戏，请手动填写路径。", "empty-state");
  const fragment = document.createDocumentFragment();
  for (const install of installs) {
    const row = el("div", "detect-row");
    row.append(el("span", "mod-path", install.path));
    const use = actionButton("使用此路径", "useGamePath", "btn");
    use.dataset.path = String(install.path || "");
    row.append(use);
    fragment.append(row);
  }
  container.replaceChildren(fragment);
}

export function renderConflict(container, details) {
  const list = el("ul", "stack");
  const files = Array.isArray(details?.files)
    ? details.files
    : (Array.isArray(details?.conflicts) ? details.conflicts : []);
  for (const file of files.slice(0, 20)) list.append(el("li", "mod-path", file?.path || file));
  if (!files.length) list.append(el("li", "muted", "目标位置已有文件。"));
  container.replaceChildren(el("p", "muted", "请处理以下冲突文件后重试："), list);
}

export function formatBytes(value) {
  let size = Number(value) || 0;
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}
