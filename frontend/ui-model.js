const ACTIVE_IMPORT_STATES = new Set(["installing", "conflict"]);
const KNOWN_MOD_STATES = new Set(["enabled", "disabled", "modified", "missing", "conflict"]);
const ACTION_TYPES = new Set(["start", "conflict", "succeed", "fail", "retry", "cancel"]);

export function normalizedModState(mod) {
  if (!mod || typeof mod !== "object") return "disabled";
  if (mod.source === "steam_workshop") {
    if (mod.valid === false) return "conflict";
    return mod.enabled === true ? "enabled" : "disabled";
  }
  const stated = mod.status ?? mod.audit?.status;
  if (KNOWN_MOD_STATES.has(stated)) return stated;
  return mod.enabled === true ? "enabled" : "disabled";
}

function containsQuery(mod, query) {
  if (!query) return true;
  const fields = [
    mod.name, mod.mod_name, mod.mod_type, mod.install_types,
    mod.nexus_id, mod.workshop_id, mod.author,
  ];
  return fields.some((value) => String(value ?? "").toLocaleLowerCase().includes(query));
}

export function deriveModView(mods, { query = "", source = "", status = "" } = {}) {
  const sourceItems = Array.isArray(mods) ? mods : [];
  const states = sourceItems.map(normalizedModState);
  const stats = {
    total: sourceItems.length,
    enabled: states.filter((value) => value === "enabled").length,
    disabled: states.filter((value) => value === "disabled").length,
    abnormal: states.filter((value) => !["enabled", "disabled"].includes(value)).length,
  };
  const normalizedQuery = String(query).trim().toLocaleLowerCase();
  const items = sourceItems.filter((mod) => {
    const modState = normalizedModState(mod);
    const statusMatches = !status || (status === "abnormal" ? !["enabled", "disabled"].includes(modState) : modState === status);
    return (!source || mod.source === source) && statusMatches && containsQuery(mod, normalizedQuery);
  });
  return { items, stats };
}

export function createImportQueue(entries = []) {
  if (!Array.isArray(entries)) throw new TypeError("Import queue entries must be an array");
  return entries.map((entry, index) => ({
    ...entry,
    id: entry?.id ?? String(index + 1),
    status: "queued",
  }));
}

function transitionAllowed(status, type) {
  const allowed = {
    queued: new Set(["start", "cancel"]),
    installing: new Set(["conflict", "succeed", "fail", "cancel"]),
    conflict: new Set(["succeed", "fail", "cancel"]),
    failed: new Set(["retry", "cancel"]),
    cancelled: new Set(["retry"]),
    succeeded: new Set(),
  };
  return allowed[status]?.has(type) === true;
}

export function reduceImportQueue(queue, action) {
  if (!action || !ACTION_TYPES.has(action.type)) throw new TypeError(`Unknown import queue action: ${action?.type}`);
  if (!Array.isArray(queue)) throw new TypeError("Import queue must be an array");
  const index = queue.findIndex(({ id }) => String(id) === String(action.id));
  if (index < 0) throw new TypeError(`Unknown import queue item: ${action.id}`);
  const current = queue[index];
  if (!transitionAllowed(current.status, action.type)) {
    throw new TypeError(`Invalid import queue transition: ${current.status} -> ${action.type}`);
  }
  if (action.type === "start" && queue.some(({ status }) => ACTIVE_IMPORT_STATES.has(status))) {
    const paused = queue.some(({ status }) => status === "conflict");
    throw new TypeError(paused ? "Import queue paused by conflict" : "Import queue already has an active item");
  }
  const statusByAction = {
    start: "installing", conflict: "conflict", succeed: "succeeded",
    fail: "failed", retry: "queued", cancel: "cancelled",
  };
  const updated = { ...current, status: statusByAction[action.type] };
  if (action.type === "conflict") updated.conflict = action.conflict ?? null;
  if (action.type === "succeed") updated.result = action.result ?? null;
  if (action.type === "fail") updated.error = action.error ?? null;
  if (action.type === "retry") {
    delete updated.error;
    delete updated.conflict;
  }
  return queue.map((item, itemIndex) => itemIndex === index ? updated : item);
}
