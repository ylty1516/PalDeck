const ALLOWED_CONTROLS = new Set(["minimize", "toggle_maximize", "close"]);

export async function callWindowControl(name, host = globalThis.window) {
  if (!ALLOWED_CONTROLS.has(name)) return false;
  const operation = host?.pywebview?.api?.[name];
  if (typeof operation !== "function") return false;
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
  const chrome = root?.querySelector?.(".window-chrome");
  const getState = host?.pywebview?.api?.get_state;
  if (!chrome || typeof getState !== "function") return false;
  const state = await getState.call(host.pywebview.api);
  const enabled = state?.custom_chrome === true;
  chrome.hidden = !enabled;
  root.documentElement?.classList?.toggle("has-custom-chrome", enabled);
  return enabled;
}

export function initializeWindowControls(root = globalThis.document, host = globalThis.window) {
  const initialize = () => setupWindowControls(root, host).catch(() => false);
  if (host?.pywebview?.api) return initialize();
  root?.addEventListener?.("pywebviewready", initialize, { once: true });
  return Promise.resolve(false);
}
