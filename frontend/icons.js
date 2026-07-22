const NS = "http://www.w3.org/2000/svg";
const freeze = (paths) => Object.freeze(paths);
const PATHS = Object.freeze({
  mods: freeze(["M4 6l8-4 8 4v12l-8 4-8-4z", "M4 6l8 4 8-4", "M12 10v12"]),
  import: freeze(["M12 3v12", "M7 10l5 5 5-5", "M4 19h16"]),
  nexus: freeze(["M12 3a9 9 0 1 0 9 9", "M12 7v5l4 2"]),
  settings: freeze(["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z", "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"]),
  credits: freeze(["M12 21s-8-4.7-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 10c0 6.3-8 11-8 11z"]),
  folder: freeze(["M3 6h7l2 2h9v11H3z"]),
  refresh: freeze(["M20 7v5h-5", "M4 17v-5h5", "M6.1 8A7 7 0 0 1 18 6l2 1", "M17.9 16A7 7 0 0 1 6 18l-2-1"]),
  search: freeze(["M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14z", "M16 16l5 5"]),
  background: freeze(["M4 5h16v14H4z", "M4 15l4-4 4 4 3-3 5 5"]),
  status: freeze(["M12 3a9 9 0 1 0 9 9", "M8 12l3 3 5-6"]),
  close: freeze(["M6 6l12 12", "M18 6L6 18"]),
  minimize: freeze(["M5 12h14"]),
  maximize: freeze(["M5 5h14v14H5z"]),
});

export const ICON_NAMES = Object.freeze(Object.keys(PATHS));

export function iconDefinition(name) {
  if (!Object.hasOwn(PATHS, name)) return null;
  return Object.freeze({ viewBox: "0 0 24 24", paths: PATHS[name] });
}

export function icon(name, label = "", documentRef = globalThis.document) {
  const definition = iconDefinition(name);
  if (!definition || !documentRef) return null;
  const svg = documentRef.createElementNS(NS, "svg");
  svg.classList.add("ui-icon");
  svg.setAttribute("viewBox", definition.viewBox);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", label ? "false" : "true");
  if (label) svg.setAttribute("aria-label", label);
  for (const d of definition.paths) {
    const path = documentRef.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export function hydrateIcons(root = document) {
  for (const slot of root.querySelectorAll("[data-icon]")) {
    if (slot.firstChild) continue;
    const node = icon(slot.dataset.icon || "", "", slot.ownerDocument);
    if (node) slot.append(node);
  }
}
