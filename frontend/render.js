const text = (value) => document.createTextNode(String(value ?? ""));

export function validatedNexusUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  const trustedHost = url.hostname.toLocaleLowerCase() === "www.nexusmods.com";
  const hasExtraUrlParts = Boolean(url.username || url.password || url.port || url.search || url.hash);
  if (url.protocol !== "https:" || !trustedHost || hasExtraUrlParts || !/^\/palworld\/mods\/\d+$/.test(url.pathname)) return null;
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
  if (!mods.length) return renderMessage(container, "没有符合当前条件的模组。", "empty-state glass-panel");
  const fragment = document.createDocumentFragment();
  for (const mod of mods) {
    const workshop = mod.source === "steam_workshop";
    const rawStatus = workshop
      ? (mod.valid === false ? "conflict" : (mod.enabled === true ? "enabled" : "disabled"))
      : (mod.status || mod.audit?.status || (mod.enabled === true ? "enabled" : "disabled"));
    const status = MOD_STATUS[rawStatus] ? rawStatus : "conflict";
    const statusInfo = MOD_STATUS[status];
    const toggleAllowed = workshop ? mod.can_toggle !== false && mod.valid !== false : ["enabled", "disabled"].includes(status);
    const id = String(workshop ? mod.workshop_id : mod.id);
    const name = mod.name || mod.mod_name || (workshop ? `Workshop ${id}` : "未命名模组");
    const card = el("article", `mod-row glass-panel${workshop ? " source-workshop" : ""} status-${status}${status === "disabled" ? " disabled-mod" : ""}`);
    card.dataset.id = id;

    const identity = el("div", "mod-identity");
    const avatar = el("span", `mod-avatar ${workshop ? "workshop" : "local"}`, workshop ? "W" : (name.trim()[0] || "M").toUpperCase());
    const identityText = el("div", "mod-identity-text");
    const title = el("h3", "mod-title", name);
    title.title = name;
    const identityMeta = workshop
      ? `作者：${mod.author || "未提供"} · Workshop ID ${id}`
      : `作者：${mod.author || "未提供"}${mod.nexus_id != null ? ` · N#${mod.nexus_id}` : ""}`;
    identityText.append(title, el("p", "mod-meta", identityMeta));
    identity.append(avatar, identityText);

    const source = el("div", "mod-source-cell");
    const types = workshop
      ? (Array.isArray(mod.install_types) && mod.install_types.length ? mod.install_types.join(" / ") : "未知")
      : (mod.mod_type || "unknown");
    const typeDetails = workshop ? types : `${types} · ${formatBytes(mod.size_bytes)}`;
    source.append(el("span", `badge${workshop ? " source-badge" : ""}`, workshop ? "Steam Workshop" : "本地模组"), el("small", "mod-type", typeDetails));

    const version = el("div", "mod-version-cell", mod.version || "未提供");
    const state = el("div", "mod-state-cell");
    state.append(el("strong", `state-label state-${status}`, statusInfo.label));
    if (workshop) {
      const dependencies = Array.isArray(mod.dependencies) && mod.dependencies.length ? mod.dependencies.join(", ") : "无";
      state.append(
        el("small", "workshop-state", `全局开关：${mod.global_enabled ? "开启" : "关闭"} · ${mod.deployed ? "已部署" : "未部署"}${mod.needs_restart ? " · 下次启动生效" : ""}`),
        el("small", "workshop-details", `依赖：${dependencies}${Array.isArray(mod.cleanup_pending) && mod.cleanup_pending.length ? " · 清理待完成" : ""}`),
      );
    } else {
      const fileCount = Array.isArray(mod.manifest_files) ? mod.manifest_files.length : (Array.isArray(mod.files) ? mod.files.length : 0);
      state.append(el("small", toggleAllowed ? "ok" : "warning", `${statusInfo.integrity} · ${fileCount} 个受管文件`));
      if (!toggleAllowed) state.append(el("small", "repair-hint", "请修复文件或重扫；异常状态不可启停。"));
    }

    const actions = el("div", "button-row mod-actions");
    if (workshop) {
      const toggle = actionButton(mod.enabled ? "禁用" : "启用", "toggleWorkshop", "btn compact");
      toggle.dataset.id = id;
      toggle.dataset.enabled = String(!mod.enabled);
      toggle.disabled = !toggleAllowed;
      const open = actionButton("文件夹", "openWorkshopFolder", "btn compact");
      open.dataset.id = id;
      const steam = actionButton("Steam 页面", "openSteamWorkshop", "btn compact");
      steam.dataset.id = id;
      actions.append(toggle, open, steam);
    } else {
      const toggle = actionButton(status === "enabled" ? "禁用" : "启用", "toggleMod", "btn compact");
      toggle.dataset.id = id;
      toggle.dataset.enabled = String(status !== "enabled");
      toggle.disabled = !toggleAllowed;
      const open = actionButton("文件夹", "openModFolder", "btn compact");
      open.dataset.id = id;
      const remove = actionButton("删除", "deleteMod", "btn compact danger");
      remove.dataset.id = id;
      actions.append(toggle, open);
      if (!toggleAllowed) {
        const rescan = actionButton("重扫", "rescanMods", "btn compact");
        rescan.dataset.id = id;
        actions.append(rescan);
      }
      actions.append(remove);
    }

    const path = el("p", "mod-path mod-row-path", mod.source_dir || mod.install_path || "");
    path.title = path.textContent;
    card.append(identity, source, version, state, actions, path);
    fragment.append(card);
  }
  container.replaceChildren(fragment);
}

export function renderNexus(container, mods) {
  if (!mods.length) return renderMessage(container, "没有找到相关模组。", "empty-state glass-panel");
  const fragment = document.createDocumentFragment();
  for (const mod of mods) {
    if (mod.adultContent !== false) continue;
    const card = el("article", "nexus-card glass-panel");
    const image = el("div", "nexus-image");
    if (typeof mod.picture_url === "string" && /^https:\/\//i.test(mod.picture_url)) {
      const picture = el("img", "nexus-picture");
      picture.src = mod.picture_url;
      picture.alt = "";
      picture.loading = "lazy";
      picture.referrerPolicy = "no-referrer";
      picture.addEventListener("error", () => picture.remove(), { once: true });
      image.append(picture);
    }
    image.append(el("span", "badge", `#${mod.nexus_id ?? "-"}`));
    const body = el("div", "nexus-body");
    const name = mod.name || "未命名";
    const summary = mod.summary || "暂无简介";
    const stats = `作者 ${mod.author || "未知"} · 版本 ${mod.version || "未知"} · 下载 ${Number(mod.downloads || 0).toLocaleString("zh-CN")} · 推荐 ${Number(mod.endorsements || 0).toLocaleString("zh-CN")}`;
    body.append(
      el("h3", "mod-title", name),
      el("p", "nexus-summary", summary),
      el("p", "mod-meta", stats),
    );
    const actions = el("div", "button-row");
    const id = String(mod.nexus_id ?? "");
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
