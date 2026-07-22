const ALLOWED_CONTROLS = new Set(["minimize", "toggle_maximize", "close"]);

function requestedChrome(host) {
  try {
    const value = new URLSearchParams(host?.location?.search || "").get("chrome");
    return value === "1" ? true : (value === "0" ? false : null);
  } catch {
    return null;
  }
}

function applyChrome(root, enabled) {
  const chrome = root?.querySelector?.(".window-chrome");
  if (!chrome) return false;
  chrome.hidden = !enabled;
  root.documentElement?.classList?.toggle("has-custom-chrome", enabled);
  return true;
}

async function waitForOperation(name, host) {
  const mayWait = requestedChrome(host) === true;
  for (let attempt = 0; attempt < (mayWait ? 100 : 1); attempt += 1) {
    const operation = host?.pywebview?.api?.[name];
    if (typeof operation === "function") return operation;
    if (mayWait) await new Promise((resolve) => setTimeout(resolve, 20));
  }
  return null;
}

export async function callWindowControl(name, host = globalThis.window) {
  if (!ALLOWED_CONTROLS.has(name)) return false;
  const operation = await waitForOperation(name, host);
  if (!operation) return false;
  await operation.call(host.pywebview.api);
  return true;
}

export async function chooseModFolder(host = globalThis.window) {
  const operation = host?.pywebview?.api?.choose_mod_folder;
  if (typeof operation !== "function") return null;
  const result = await operation.call(host.pywebview.api);
  return result && Array.isArray(result.items) ? result.items : null;
}

export async function setupWindowControls(root = globalThis.document, host = globalThis.window) {
  const getState = host?.pywebview?.api?.get_state;
  if (typeof getState !== "function") return false;
  const state = await getState.call(host.pywebview.api);
  return applyChrome(root, state?.custom_chrome === true);
}

export function initializeWindowControls(root = globalThis.document, host = globalThis.window) {
  const requested = requestedChrome(host);
  if (requested !== null) applyChrome(root, requested);
  const initialize = () => setupWindowControls(root, host).catch(() => requested === true);
  if (host?.pywebview?.api) return initialize();
  root?.addEventListener?.("pywebviewready", initialize, { once: true });
  return Promise.resolve(requested === true);
}
