/* Palworld Mod Manager — frontend */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  mods: [],
  gamePath: null,
  nexusMode: "popular",
};

const titles = {
  mods: ["我的模组", "管理已安装模组，一键启用 / 禁用"],
  import: ["导入安装", "拖入 zip/pak，自动识别类型并安装到正确目录"],
  nexus: ["N 网热门", "实时连接 Nexus Mods，显示模组尾号与详情"],
  settings: ["游戏路径", "自动检测幻兽帕鲁安装目录并创建模组文件夹"],
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
  } catch (e) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}<br/><br/>请先在「游戏路径」中配置目录</div>`;
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
  const form = new FormData();
  form.append("file", file);
  form.append("type", $("#importType").value || "auto");
  const name = $("#importName").value.trim();
  if (name) form.append("name", name);
  const nid = $("#importNexusId").value.trim();
  if (nid) form.append("nexus_id", nid);

  const resultBox = $("#importResult");
  resultBox.classList.remove("hidden", "ok", "fail");
  resultBox.textContent = `正在导入 ${file.name} …`;

  try {
    const data = await api("/api/mods/import", { method: "POST", body: form });
    const mod = data.mod || data;
    resultBox.classList.add("ok");
    resultBox.innerHTML = `安装成功：<strong>${escapeHtml(mod.name)}</strong><br/>类型 ${typeLabel(mod.mod_type)} · 路径<br/><code>${escapeHtml(mod.install_path)}</code>`;
    toast(`导入成功：${mod.name}`, "success");
    $("#importName").value = "";
    $("#importNexusId").value = "";
    await loadMods();
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
  } catch (e) {
    /* ignore */
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
}

async function init() {
  bindUI();
  try {
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
  } catch (e) {
    toast(e.message, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);
