const text = (value) => document.createTextNode(String(value ?? ""));

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

export function renderMods(container, mods) {
  if (!mods.length) return renderMessage(container, "暂无模组，请从“导入安装”添加。", "empty-state glass-panel");
  const fragment = document.createDocumentFragment();
  for (const mod of mods) {
    const card = el("article", `mod-card glass-panel${mod.enabled ? "" : " disabled-mod"}`);
    card.dataset.id = String(mod.id);
    const main = el("div", "mod-main");
    const title = el("h3", "mod-title", mod.name || "未命名模组");
    const badge = el("span", "badge", mod.mod_type || "unknown");
    title.append(" ", badge);
    main.append(title);
    const meta = el("p", "mod-meta");
    meta.append(text(mod.enabled ? "已启用" : "已禁用"), text(` · ${formatBytes(mod.size_bytes)}`));
    if (mod.nexus_id != null) meta.append(text(` · N#${mod.nexus_id}`));
    main.append(meta, el("p", "mod-path", mod.install_path || ""));
    const actions = el("div", "button-row");
    const toggle = actionButton(mod.enabled ? "禁用" : "启用", "toggleMod", "btn");
    toggle.dataset.id = String(mod.id);
    toggle.dataset.enabled = String(!mod.enabled);
    const open = actionButton("文件夹", "openModFolder", "btn");
    open.dataset.id = String(mod.id);
    const remove = actionButton("删除", "deleteMod", "btn danger");
    remove.dataset.id = String(mod.id);
    actions.append(toggle, open, remove);
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
  const conflicts = Array.isArray(details?.conflicts) ? details.conflicts : [];
  for (const conflict of conflicts.slice(0, 20)) list.append(el("li", "mod-path", conflict.path || conflict));
  if (!conflicts.length) list.append(el("li", "muted", "目标位置已有文件。"));
  container.replaceChildren(list);
}

export function formatBytes(value) {
  let size = Number(value) || 0;
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}
