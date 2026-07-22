let previousFocus = null;

export function rememberFocus(root = globalThis.document) {
  const candidate = root?.activeElement;
  previousFocus = candidate && typeof candidate.focus === "function" ? candidate : null;
  return previousFocus !== null;
}

export function restoreFocus() {
  const candidate = previousFocus;
  previousFocus = null;
  if (!candidate || candidate.isConnected === false || typeof candidate.focus !== "function") return false;
  candidate.focus();
  return true;
}

export function setExpanded(button, panel, expanded) {
  const value = expanded === true;
  button.setAttribute("aria-expanded", String(value));
  panel.hidden = !value;
}
