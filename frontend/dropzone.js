function transferHasFiles(event) {
  const types = Array.from(event?.dataTransfer?.types || []);
  return !types.length || types.includes('Files');
}

function detachDropzone(zone, host, handlers, reset) {
  for (const [name, handler] of Object.entries(handlers)) {
    zone.removeEventListener?.(name, handler);
  }
  host?.removeEventListener?.('dragend', reset);
  host?.removeEventListener?.('blur', reset);
  reset();
}

function attachDropzone(zone, host, handlers, reset) {
  for (const [name, handler] of Object.entries(handlers)) {
    zone.addEventListener(name, handler);
  }
  host?.addEventListener?.('dragend', reset);
  host?.addEventListener?.('blur', reset);
  return () => detachDropzone(zone, host, handlers, reset);
}

function bindDropzone(zone, onFiles, {
  host = globalThis.window,
  onEmpty = () => {},
} = {}) {
  let dragDepth = 0;
  const setActive = (active) => zone.classList?.toggle?.('dragging', active);
  const reset = () => { dragDepth = 0; setActive(false); };
  const consume = (event) => {
    event.preventDefault();
    event.stopPropagation?.();
  };
  const handlers = {};
  handlers.dragenter = (event) => {
    if (!transferHasFiles(event)) return;
    consume(event);
    dragDepth += 1;
    setActive(true);
  };
  handlers.dragover = (event) => {
    if (!transferHasFiles(event)) return;
    consume(event);
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    dragDepth = Math.max(1, dragDepth);
    setActive(true);
  };
  handlers.dragleave = (event) => {
    if (dragDepth <= 0) return;
    consume(event);
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setActive(false);
  };
  handlers.drop = async (event) => {
    consume(event);
    reset();
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length) return onEmpty();
    return onFiles(files);
  };
  return attachDropzone(zone, host, handlers, reset);
}

export function setupFileDropzone(zone, onFiles, options = {}) {
  if (!zone?.addEventListener || typeof onFiles !== 'function') {
    throw new TypeError('A dropzone and file handler are required');
  }
  return bindDropzone(zone, onFiles, options);
}
