/* Palworld Mod Manager — frontend */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  mods: [],
  configs: [],
  gamePath: null,
  nexusMode: "popular",
  updateInfo: null,
  localVersion: null,
};

const titles = {
  mods: ["我的模组", "管理已安装模组，一键启用 / 禁用"],
  import: ["导入安装", "直接拖入 .zip / .pak，自动解压并安装（无需手动解压）"],
  nexus: ["N 网热门", "实时连接 Nexus Mods，显示模组尾号与详情"],
  settings: ["游戏路径 / 更新 / UE4SS", "检测游戏目录、面板一键更新、安装 UE4SS"],
};

function toast(message, type = "info") {
  const host = $("#toastHost");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .2s";
    setTimeout(() => el.remove(), 220);
  }, 3200);
}

async function api(path, options = {}) {
  const opts = { ...options };
  opts.headers = { ...(options.headers || {}) };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body === "object") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(`服务器响应异常 (${res.status})`);
  }
  if (!data.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data.data !== undefined ? data.data : data;
}

function formatBytes(n) {
  if (!n || n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function formatNum(n) {
  if (n == null) return "0";
  return Number(n).toLocaleString("zh-CN");
}

function typeLabel(t) {
  return (
    {
      pak: "PAK",
      logicpak: "LogicMods",
      ue4ss: "UE4SS",
      workshop: "Workshop",
      loose: "Loose",
    }[t] || t
  );
}

function switchView(name) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  const [title, sub] = titles[name] || ["", ""];
  $("#viewTitle").textContent = title;
  $("#viewSubtitle").textContent = sub;

  if (name === "mods") loadMods();
  if (name === "nexus") loadNexus();
  if (name === "settings") loadSettings();
}

function updatePathChip(path) {
  const chip = $("#pathChip");
  if (path) {
    chip.textContent = path;
    chip.title = path;
    state.gamePath = path;
  } else {
    chip.textContent = "未检测游戏目录";
    chip.title = "未配置";
    state.gamePath = null;
  }
}

/* ---------- Mods ---------- */
async function loadMods() {
  const list = $("#modList");
  list.innerHTML = `<div class="empty-state">正在加载模组列表…</div>`;
  try {
    const mods = await api("/api/mods");
    state.mods = mods || [];
    renderMods();
    await loadConfigs();
  } catch (e) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}<br/><br/>请先在「游戏路径」中配置目录</div>`;
  }
}

async function loadConfigs() {
  const host = $("#configList");
  if (!host) return;
  try {
    const configs = await api("/api/mod-config");
    state.configs = configs || [];
    renderConfigs();
  } catch (e) {
    host.innerHTML = "";
  }
}

function renderConfigs() {
  const host = $("#configList");
  if (!host) return;
  const configs = state.configs || [];
  if (!configs.length) {
    host.innerHTML = `
      <div class="config-card">
        <h3>可调节参数模组</h3>
        <p class="cfg-desc">尚未安装带配置的模组。可点击上方「安装可配置背包扩容」一键安装（默认 100 格，可改）。</p>
      </div>`;
    return;
  }
  host.innerHTML = configs
    .map((c) => {
      const schema = c.schema || {};
      const fields = schema.fields || [];
      const values = c.values || {};
      const folder = c.mod_folder;
      const fieldHtml = fields
        .map((f) => {
          const key = f.key;
          const val = values[key] ?? f.default;
          const help = f.description ? `<p class="cfg-help">${escapeHtml(f.description)}</p>` : "";
          if (f.type === "bool" || f.type === "boolean") {
            return `<label class="config-field">
              <span class="cfg-label">${escapeHtml(f.label || key)}</span>
              <input type="checkbox" data-mod="${escapeAttr(folder)}" data-key="${escapeAttr(key)}" ${val ? "checked" : ""} />
              ${help}
            </label>`;
          }
          const min = f.min != null ? `min="${f.min}"` : "";
          const max = f.max != null ? `max="${f.max}"` : "";
          const step = f.step != null ? `step="${f.step}"` : f.type === "int" ? `step="1"` : "";
          return `<label class="config-field">
            <span class="cfg-label">${escapeHtml(f.label || key)}</span>
            <input type="number" data-mod="${escapeAttr(folder)}" data-key="${escapeAttr(key)}" value="${escapeAttr(val)}" ${min} ${max} ${step} />
            ${help}
          </label>`;
        })
        .join("");
      return `<article class="config-card" data-config-mod="${escapeAttr(folder)}">
        <h3>${escapeHtml(schema.display_name || folder)}</h3>
        <p class="cfg-desc">${escapeHtml(schema.description || "")} · 路径 <code>${escapeHtml(c.install_path || "")}</code></p>
        <div class="config-fields">${fieldHtml}</div>
        <div class="config-actions">
          <button class="btn btn-primary btn-sm btn-save-config" data-mod="${escapeAttr(folder)}" type="button">保存配置</button>
          <button class="btn btn-secondary btn-sm btn-reset-config" data-mod="${escapeAttr(folder)}" type="button">恢复默认</button>
        </div>
      </article>`;
    })
    .join("");

  host.querySelectorAll(".btn-save-config").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const mod = btn.dataset.mod;
      const card = host.querySelector(`[data-config-mod="${CSS.escape(mod)}"]`);
      if (!card) return;
      const values = {};
      card.querySelectorAll("input[data-key]").forEach((inp) => {
        const k = inp.dataset.key;
        if (inp.type === "checkbox") values[k] = inp.checked;
        else if (inp.type === "number") values[k] = inp.value === "" ? null : Number(inp.value);
        else values[k] = inp.value;
      });
      btn.disabled = true;
      try {
        const r = await api(`/api/mod-config/${encodeURIComponent(mod)}`, {
          method: "POST",
          body: { values },
        });
        toast(r.message || "配置已保存", "success");
        await loadConfigs();
      } catch (e) {
        toast(e.message, "error");
      } finally {
        btn.disabled = false;
      }
    });
  });

  host.querySelectorAll(".btn-reset-config").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const mod = btn.dataset.mod;
      const c = state.configs.find((x) => x.mod_folder === mod);
      if (!c) return;
      const defaults = {};
      (c.schema.fields || []).forEach((f) => {
        defaults[f.key] = f.default;
      });
      try {
        await api(`/api/mod-config/${encodeURIComponent(mod)}`, {
          method: "POST",
          body: { values: defaults },
        });
        toast("已恢复默认", "success");
        await loadConfigs();
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });
}

async function installBagExpand() {
  try {
    const r = await api("/api/mod-config/install-bundled", {
      method: "POST",
      body: { name: "ConfigurableBagExpand" },
    });
    toast(r.message || "背包扩容模组已安装", "success");
    await loadMods();
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderMods() {
  const filter = ($("#modFilter").value || "").trim().toLowerCase();
  let mods = state.mods;
  if (filter) {
    mods = mods.filter(
      (m) =>
        (m.name || "").toLowerCase().includes(filter) ||
        (m.mod_type || "").toLowerCase().includes(filter) ||
        String(m.nexus_id || "").includes(filter)
    );
  }
  $("#modStats").textContent = `共 ${state.mods.length} 个模组${filter ? ` · 筛选后 ${mods.length}` : ""} · 已启用 ${state.mods.filter((m) => m.enabled).length}`;

  const list = $("#modList");
  if (!mods.length) {
    list.innerHTML = `<div class="empty-state">暂无模组。<br/>去「导入安装」添加，或在「N 网热门」浏览后手动下载导入。</div>`;
    return;
  }

  list.innerHTML = mods
    .map((m) => {
      const nexus =
        m.nexus_id != null
          ? `<span class="badge nexus" title="Nexus Mods ID">N#${m.nexus_id}</span>`
          : "";
      return `
      <article class="mod-card ${m.enabled ? "" : "disabled-mod"}" data-id="${m.id}">
        <div class="mod-main">
          <div class="mod-title">
            ${escapeHtml(m.name)}
            <span class="badge ${escapeHtml(m.mod_type)}">${typeLabel(m.mod_type)}</span>
            ${nexus}
          </div>
          <div class="mod-meta">
            <span>${m.enabled ? "已启用" : "已禁用"}</span>
            <span>${formatBytes(m.size_bytes)}</span>
            <span>${escapeHtml(m.notes || m.source_name || "")}</span>
          </div>
          <div class="mod-path">${escapeHtml(m.install_path)}</div>
        </div>
        <div class="mod-actions">
          <label class="switch" title="${m.enabled ? "点击禁用" : "点击启用"}">
            <input type="checkbox" class="mod-toggle" data-id="${m.id}" ${m.enabled ? "checked" : ""} />
            <span class="slider"></span>
          </label>
          <button class="btn btn-secondary btn-xs btn-open" data-id="${m.id}" type="button">文件夹</button>
          <button class="btn btn-danger btn-xs btn-del" data-id="${m.id}" type="button">删除</button>
        </div>
      </article>`;
    })
    .join("");

  list.querySelectorAll(".mod-toggle").forEach((el) => {
    el.addEventListener("change", async () => {
      const id = el.dataset.id;
      const enabled = el.checked;
      el.disabled = true;
      try {
        const result = await api(`/api/mods/${id}/toggle`, {
          method: "POST",
          body: { enabled },
        });
        const updated = result.mod || result;
        const idx = state.mods.findIndex((x) => x.id === id);
        if (idx >= 0) state.mods[idx] = updated;
        toast(enabled ? `已启用：${updated.name}` : `已禁用：${updated.name}`, "success");
        renderMods();
      } catch (e) {
        el.checked = !enabled;
        toast(e.message, "error");
      } finally {
        el.disabled = false;
      }
    });
  });

  list.querySelectorAll(".btn-del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const mod = state.mods.find((m) => m.id === id);
      if (!confirm(`确定删除模组「${mod?.name || id}」？\n将从游戏目录移除文件，此操作不可撤销。`)) return;
      btn.disabled = true;
      try {
        await api(`/api/mods/${id}`, { method: "DELETE" });
        state.mods = state.mods.filter((m) => m.id !== id);
        toast("模组已删除", "success");
        renderMods();
      } catch (e) {
        toast(e.message, "error");
        btn.disabled = false;
      }
    });
  });

  list.querySelectorAll(".btn-open").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/mods/open-folder?id=${encodeURIComponent(btn.dataset.id)}`);
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });
}

/* ---------- Import ---------- */
async function importFile(file) {
  if (!file) return;
  if (!state.gamePath) {
    toast("请先配置游戏路径", "error");
    switchView("settings");
    return;
  }
  const lower = (file.name || "").toLowerCase();
  if (!lower.endsWith(".zip") && !lower.endsWith(".pak")) {
    toast("请使用 .zip 或 .pak（zip 无需先解压）", "error");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("type", $("#importType").value || "auto");
  const name = $("#importName").value.trim();
  if (name) form.append("name", name);
  const nid = $("#importNexusId").value.trim();
  if (nid) form.append("nexus_id", nid);

  const resultBox = $("#importResult");
  resultBox.classList.remove("hidden", "ok", "fail");
  resultBox.textContent = `正在${lower.endsWith(".zip") ? "解压并" : ""}安装 ${file.name} …`;

  try {
    const data = await api("/api/mods/import", { method: "POST", body: form });
    const mod = data.mod || data;
    const kind = data.kind || mod.mod_type;
    resultBox.classList.add("ok");
    if (kind === "ue4ss_framework" || mod.mod_type === "ue4ss_framework") {
      const u = data.ue4ss || {};
      resultBox.innerHTML = `UE4SS 框架已从 zip 自动解压安装<br/><code>${escapeHtml(u.win64 || mod.install_path || "")}</code><br/>${escapeHtml(u.message || "")}`;
      toast("UE4SS 安装成功", "success");
      loadUe4ssStatus();
    } else {
      resultBox.innerHTML = `安装成功：<strong>${escapeHtml(mod.name)}</strong><br/>类型 ${typeLabel(mod.mod_type)} · 路径<br/><code>${escapeHtml(mod.install_path)}</code>`;
      toast(`导入成功：${mod.name}`, "success");
      await loadMods();
    }
    $("#importName").value = "";
    $("#importNexusId").value = "";
  } catch (e) {
    resultBox.classList.add("fail");
    resultBox.textContent = `导入失败：${e.message}`;
    toast(e.message, "error");
  }
}

function setupDropzone() {
  const dz = $("#dropzone");
  const input = $("#fileInput");

  ["dragenter", "dragover"].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.remove("dragover");
    });
  });
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) importFile(file);
  });
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) importFile(file);
    input.value = "";
  });
}

/* ---------- Nexus ---------- */
async function loadNexus(mode) {
  if (mode) state.nexusMode = mode;
  const status = $("#nexusStatus");
  const grid = $("#nexusGrid");
  status.textContent = "正在连接 N 网…";
  grid.innerHTML = "";

  $$("[data-nexus]").forEach((b) => {
    b.classList.toggle("active-sort", b.dataset.nexus === state.nexusMode || (state.nexusMode === "popular" && b.dataset.nexus === "popular"));
  });
  // fix endorsements active
  $$("[data-nexus]").forEach((b) => {
    b.classList.toggle("active-sort", b.dataset.nexus === state.nexusMode);
  });

  try {
    let mods;
    if (state.nexusMode === "latest") {
      mods = await api("/api/nexus/latest?count=24");
    } else if (state.nexusMode === "endorsements") {
      mods = await api("/api/nexus/popular?sort=endorsements&count=24");
    } else if (state.nexusMode === "search") {
      const q = $("#nexusQuery").value.trim();
      mods = await api(`/api/nexus/search?q=${encodeURIComponent(q)}&count=24`);
    } else {
      mods = await api("/api/nexus/popular?sort=downloads&count=24");
    }
    status.textContent = `已从 N 网获取 ${mods.length} 个模组 · 尾号即 nexusmods.com/palworld/mods/<尾号>`;
    renderNexus(mods);
  } catch (e) {
    status.textContent = `连接失败：${e.message}`;
    grid.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
  }
}

function renderNexus(mods) {
  const grid = $("#nexusGrid");
  if (!mods.length) {
    grid.innerHTML = `<div class="empty-state">没有找到相关模组</div>`;
    return;
  }
  grid.innerHTML = mods
    .map((m) => {
      const img = m.picture_url
        ? `style="background-image:url('${escapeAttr(m.picture_url)}')"`
        : "";
      return `
      <article class="nexus-card">
        <div class="nexus-thumb" ${img}>
          <span class="nexus-id-badge">尾号 #${m.mod_id ?? m.nexus_id}</span>
        </div>
        <div class="nexus-body">
          <div class="nexus-name">${escapeHtml(m.name)}</div>
          <div class="nexus-summary">${escapeHtml(m.summary || "暂无简介")}</div>
          <div class="nexus-meta">
            <span>⬇ ${formatNum(m.downloads)}</span>
            <span>👍 ${formatNum(m.endorsements)}</span>
            <span>by ${escapeHtml(m.author || "?")}</span>
            <span>v${escapeHtml(m.version || "-")}</span>
          </div>
          <div class="nexus-actions">
            <button class="btn btn-primary btn-xs btn-nexus-open" data-url="${escapeAttr(m.url)}" type="button">打开 N 网</button>
            <button class="btn btn-secondary btn-xs btn-nexus-copy" data-id="${m.mod_id ?? m.nexus_id}" data-name="${escapeAttr(m.name)}" type="button">复制尾号</button>
          </div>
        </div>
      </article>`;
    })
    .join("");

  grid.querySelectorAll(".btn-nexus-open").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.url) window.open(btn.dataset.url, "_blank", "noopener");
    });
  });
  grid.querySelectorAll(".btn-nexus-copy").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const name = btn.dataset.name;
      try {
        await navigator.clipboard.writeText(String(id));
        toast(`已复制尾号 #${id}（${name}）`, "success");
        // Prefill import form for convenience
        $("#importNexusId").value = id;
        $("#importName").value = name;
      } catch {
        toast(`尾号：${id}`, "info");
      }
    });
  });
}

/* ---------- Settings ---------- */
async function loadSettings() {
  try {
    const status = await api("/api/game/status");
    if (status.configured && status.path) {
      $("#gamePathInput").value = status.path;
      updatePathChip(status.path);
      showPathInfo(status);
    }
    await loadUe4ssStatus();
    // refresh version chip; don't force network every time if already checked
    if (!state.updateInfo) {
      try {
        const health = await api("/api/health");
        state.localVersion = health.version;
        const chip = $("#versionChip");
        if (chip) chip.textContent = `v${health.version || "?"}`;
      } catch (_) {}
    }
  } catch (e) {
    /* ignore */
  }
}

function renderUpdateStatus(info, errMsg) {
  const el = $("#updateStatus");
  const applyBtn = $("#btnUpdateApply");
  const chip = $("#versionChip");
  if (!el) return;
  if (errMsg) {
    el.innerHTML = `<div><strong>检查失败：</strong>${escapeHtml(errMsg)}</div>`;
    if (applyBtn) applyBtn.disabled = true;
    return;
  }
  if (!info) {
    el.textContent = "尚未检查";
    if (applyBtn) applyBtn.disabled = true;
    return;
  }
  state.updateInfo = info;
  state.localVersion = info.local_version;
  if (chip) {
    chip.textContent = info.update_available
      ? `v${info.local_version} → v${info.remote_version}`
      : `v${info.local_version}`;
    chip.classList.toggle("has-update", !!info.update_available);
  }
  const asset = info.asset;
  el.innerHTML = `
    <div><strong>当前版本：</strong>v${escapeHtml(info.local_version || "?")}</div>
    <div><strong>GitHub 最新：</strong>v${escapeHtml(info.remote_version || "?")}
      ${info.tag_name ? `（${escapeHtml(info.tag_name)}）` : ""}</div>
    <div><strong>仓库：</strong>${escapeHtml(info.github || "")}</div>
    <div><strong>状态：</strong>${
      info.update_available
        ? `<span style="color:#b45309;font-weight:700">发现新版本，可一键更新</span>`
        : `<span style="color:#047857;font-weight:600">已是最新</span>`
    }</div>
    ${
      asset
        ? `<div><strong>更新包：</strong>${escapeHtml(asset.name || "")}（${formatBytes(asset.size || 0)}）</div>`
        : `<div style="color:#b91c1c">Release 中未找到 .exe / zip 资源</div>`
    }
    ${info.release_url ? `<div><a href="${escapeAttr(info.release_url)}" target="_blank" rel="noopener">打开 Release 页面</a></div>` : ""}
    ${
      !info.is_frozen
        ? `<div style="margin-top:6px;color:#64748b">开发模式运行：可检查版本，完整一键替换需使用打包后的 EXE。</div>`
        : ""
    }
  `;
  if (applyBtn) {
    applyBtn.disabled = !(info.update_available && info.asset && info.asset.browser_download_url);
  }
}

async function checkUpdate(silent) {
  const el = $("#updateStatus");
  if (el && !silent) el.textContent = "正在连接 GitHub…";
  try {
    const info = await api("/api/update/check");
    renderUpdateStatus(info);
    if (!silent) {
      if (info.update_available) toast(`发现新版本 v${info.remote_version}`, "success");
      else toast(`已是最新 v${info.local_version}`, "info");
    }
    return info;
  } catch (e) {
    renderUpdateStatus(null, e.message);
    if (!silent) toast(e.message, "error");
    return null;
  }
}

async function applyUpdate() {
  const box = $("#updateResult");
  const btn = $("#btnUpdateApply");
  if (box) {
    box.classList.remove("hidden", "ok", "fail");
    box.textContent = "正在下载更新，请稍候…";
  }
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/update/apply", { method: "POST", body: {} });
    if (box) {
      box.classList.add("ok");
      box.innerHTML = `${escapeHtml(r.message || "更新已开始")}<br/>目标版本 v${escapeHtml(r.version || "")}`;
    }
    toast(r.message || "更新中…", "success");
    if (r.should_exit) {
      // UI will close with process
      setTimeout(() => {
        if (box) box.textContent = "正在重启…";
      }, 500);
    }
  } catch (e) {
    if (box) {
      box.classList.add("fail");
      box.textContent = `更新失败：${e.message}`;
    }
    toast(e.message, "error");
    if (btn) btn.disabled = false;
  }
}

function showPathInfo(info) {
  const box = $("#pathInfo");
  if (!info || !info.valid) {
    box.classList.add("hidden");
    return;
  }
  const paths = info.mod_paths || {};
  box.classList.remove("hidden");
  box.innerHTML = `
    <div><strong>状态：</strong>有效游戏目录 ${info.has_ue4ss ? "· 已检测到 UE4SS" : "· 未安装 UE4SS（PAK 模组仍可用）"}</div>
    <div><strong>~mods：</strong><code>${escapeHtml(paths.tilde_mods || "")}</code></div>
    <div><strong>LogicMods：</strong><code>${escapeHtml(paths.logic_mods || "")}</code></div>
    <div><strong>UE4SS Mods：</strong><code>${escapeHtml(paths.ue4ss_mods || "")}</code></div>
    <div><strong>Workshop：</strong><code>${escapeHtml(paths.workshop || "")}</code></div>
  `;
}

async function loadUe4ssStatus() {
  const el = $("#ue4ssStatus");
  if (!el) return;
  if (!state.gamePath) {
    el.textContent = "请先设置游戏路径";
    return;
  }
  try {
    const st = await api("/api/ue4ss/status");
    const bp = st.bp_mod_loader;
    const bpText = bp === true ? "已开启" : bp === false ? "未开启" : "未知";
    el.innerHTML = st.installed
      ? `<div><strong>UE4SS：</strong>已安装</div>
         <div><strong>Win64：</strong><code>${escapeHtml(st.win64 || "")}</code></div>
         <div><strong>BPModLoaderMod：</strong>${bpText}</div>`
      : `<div><strong>UE4SS：</strong>未安装</div>
         <div>脚本 / LogicMods 需要安装。可点「在线安装」或导入官方 zip。</div>
         <div><strong>目标：</strong><code>${escapeHtml(st.win64 || "")}</code></div>`;
  } catch (e) {
    el.textContent = e.message;
  }
}

async function installUe4ssOnline() {
  const box = $("#ue4ssResult");
  const btn = $("#btnUe4ssOnline");
  box.classList.remove("hidden", "ok", "fail");
  box.textContent = "正在从 GitHub 下载并解压安装 UE4SS，请稍候…";
  btn.disabled = true;
  try {
    const r = await api("/api/ue4ss/install-latest", { method: "POST", body: {} });
    box.classList.add("ok");
    box.innerHTML = `安装成功：${escapeHtml(r.download_name || "UE4SS")}<br/>
      文件数 ${r.files_copied || "?"} · ${escapeHtml(r.message || "")}<br/>
      <code>${escapeHtml(r.win64 || "")}</code>`;
    toast("UE4SS 安装完成", "success");
    await loadUe4ssStatus();
    const status = await api("/api/game/status");
    showPathInfo(status);
  } catch (e) {
    box.classList.add("fail");
    box.textContent = `安装失败：${e.message}`;
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

async function installUe4ssZip(file) {
  if (!file) return;
  const box = $("#ue4ssResult");
  box.classList.remove("hidden", "ok", "fail");
  box.textContent = `正在解压安装 ${file.name} …`;
  const form = new FormData();
  form.append("file", file);
  try {
    const r = await api("/api/ue4ss/install-zip", { method: "POST", body: form });
    box.classList.add("ok");
    box.innerHTML = `已从 zip 安装<br/><code>${escapeHtml(r.win64 || "")}</code><br/>${escapeHtml(r.message || "")}`;
    toast("UE4SS 安装完成", "success");
    await loadUe4ssStatus();
  } catch (e) {
    box.classList.add("fail");
    box.textContent = `安装失败：${e.message}`;
    toast(e.message, "error");
  }
}

async function autoDetect() {
  const list = $("#detectList");
  list.innerHTML = `<div class="empty-state">正在扫描 Steam 库…</div>`;
  try {
    const data = await api("/api/game/detect");
    const installs = data.installs || [];
    if (data.current) {
      $("#gamePathInput").value = data.current;
      updatePathChip(data.current);
    }
    if (!installs.length) {
      list.innerHTML = `<div class="empty-state">未自动找到游戏。请手动填写安装路径（含 Palworld.exe 的目录）。</div>`;
      return;
    }
    list.innerHTML = installs
      .map(
        (inst, i) => `
      <div class="detect-item">
        <span>${escapeHtml(inst.path)}${inst.has_ue4ss ? " · UE4SS" : ""}</span>
        <button class="btn btn-primary btn-xs btn-use-path" data-path="${escapeAttr(inst.path)}" type="button">使用此路径</button>
      </div>`
      )
      .join("");
    list.querySelectorAll(".btn-use-path").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("#gamePathInput").value = btn.dataset.path;
        savePath();
      });
    });
    // Auto-select first if nothing configured
    if (!data.current && installs[0]) {
      $("#gamePathInput").value = installs[0].path;
    }
  } catch (e) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
    toast(e.message, "error");
  }
}

async function savePath() {
  const path = $("#gamePathInput").value.trim();
  if (!path) {
    toast("请输入路径", "error");
    return;
  }
  try {
    const result = await api("/api/game/set", { method: "POST", body: { path } });
    updatePathChip(result.game_path || path);
    toast("游戏路径已保存，模组目录已就绪", "success");
    const status = await api("/api/game/status");
    showPathInfo(status);
    await loadMods();
  } catch (e) {
    toast(e.message, "error");
  }
}

/* ---------- Utils ---------- */
function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

/* ---------- Boot ---------- */
function bindUI() {
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  $("#btnRefreshMods").addEventListener("click", () => loadMods());
  const btnBag = $("#btnInstallBagExpand");
  if (btnBag) btnBag.addEventListener("click", installBagExpand);
  const btnCfg = $("#btnRefreshConfigs");
  if (btnCfg) btnCfg.addEventListener("click", loadConfigs);
  $("#btnOpenModsFolder").addEventListener("click", async () => {
    try {
      await api("/api/mods/open-folder");
    } catch (e) {
      toast(e.message, "error");
    }
  });
  $("#btnRescan").addEventListener("click", async () => {
    try {
      await api("/api/mods/resync", { method: "POST" });
      await loadMods();
      toast("已重新扫描游戏目录", "success");
    } catch (e) {
      toast(e.message, "error");
    }
  });
  $("#modFilter").addEventListener("input", () => renderMods());

  setupDropzone();

  $("#btnNexusSearch").addEventListener("click", () => {
    state.nexusMode = "search";
    loadNexus("search");
  });
  $("#nexusQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      state.nexusMode = "search";
      loadNexus("search");
    }
  });
  $$("[data-nexus]").forEach((btn) => {
    btn.addEventListener("click", () => loadNexus(btn.dataset.nexus));
  });

  $("#btnAutoDetect").addEventListener("click", autoDetect);
  $("#btnSavePath").addEventListener("click", savePath);
  $("#btnEnsureFolders").addEventListener("click", async () => {
    try {
      const r = await api("/api/game/ensure-folders", { method: "POST" });
      toast(r.created?.length ? `已创建 ${r.created.length} 个目录` : "模组目录已存在", "success");
      const status = await api("/api/game/status");
      showPathInfo(status);
    } catch (e) {
      toast(e.message, "error");
    }
  });

  const btnUe4 = $("#btnUe4ssOnline");
  if (btnUe4) btnUe4.addEventListener("click", installUe4ssOnline);
  const btnUe4R = $("#btnUe4ssRefresh");
  if (btnUe4R) btnUe4R.addEventListener("click", loadUe4ssStatus);

  const btnUp1 = $("#btnCheckUpdate");
  if (btnUp1) btnUp1.addEventListener("click", () => {
    switchView("settings");
    checkUpdate(false);
  });
  const btnUp2 = $("#btnUpdateCheck");
  if (btnUp2) btnUp2.addEventListener("click", () => checkUpdate(false));
  const btnUp3 = $("#btnUpdateApply");
  if (btnUp3) btnUp3.addEventListener("click", applyUpdate);
  const ue4zip = $("#ue4ssZipInput");
  if (ue4zip) {
    ue4zip.addEventListener("change", () => {
      const f = ue4zip.files?.[0];
      if (f) installUe4ssZip(f);
      ue4zip.value = "";
    });
  }
}

async function init() {
  bindUI();
  try {
    try {
      const health = await api("/api/health");
      state.localVersion = health.version;
      const chip = $("#versionChip");
      if (chip) chip.textContent = `v${health.version || "?"}`;
    } catch (_) {}

    const status = await api("/api/game/status");
    if (status.configured && status.path) {
      updatePathChip(status.path);
      await loadMods();
    } else {
      // Auto detect on first launch
      const data = await api("/api/game/detect");
      if (data.installs?.length) {
        const path = data.installs[0].path;
        await api("/api/game/set", { method: "POST", body: { path } });
        updatePathChip(path);
        toast(`已自动检测到游戏：${path}`, "success");
        await loadMods();
      } else {
        $("#modList").innerHTML = `<div class="empty-state">未找到幻兽帕鲁安装目录，请到「游戏路径」手动设置。</div>`;
        switchView("settings");
        autoDetect();
      }
    }
    // Background version check (non-blocking)
    checkUpdate(true).catch(() => {});
  } catch (e) {
    toast(e.message, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);
