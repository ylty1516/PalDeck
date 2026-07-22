export function actionableErrorMessage(error) {
  if (error?.status === 423 && error?.code === "game_running") return "请先退出游戏，再重试此操作";
  if (error?.status === 403 && error?.code === "permission_denied") return "权限不足，请点击“以管理员身份重启”后重试";
  if (error?.status === 410 && error?.code === "upload_expired") return "暂存文件已过期，请重新选择文件";
  return error?.message || "操作失败";
}

export function dynamicActionKey(action, id) {
  return action === "rescanMods" ? "rescan-mods" : `${action}:${id || "global"}`;
}

export function pendingUploadTokenAfterError(_previousToken, error) {
  if (error?.status === 409 && error?.code === "mod_conflict") {
    return error?.details?.upload_token || null;
  }
  return null;
}

export function resetModFileSelectionState(state) {
  return { ...state, pendingUploadToken: null, selectedModFile: null };
}

export function nextModsGeneration(current) {
  return Number(current) + 1;
}

export function createSerialQueue() {
  let tail = Promise.resolve();
  return Object.freeze({
    enqueue(operation) {
      if (typeof operation !== "function") return Promise.reject(new TypeError("operation must be a function"));
      const result = tail.then(operation, operation);
      tail = result.catch(() => undefined);
      return result;
    },
  });
}

export function createRevisionGuard(initial = 0) {
  let revision = Number.isSafeInteger(initial) && initial >= 0 ? initial : 0;
  return Object.freeze({
    capture() { return revision; },
    bump() { revision += 1; return revision; },
    apply(expected, operation) {
      if (expected !== revision) return false;
      operation();
      return true;
    },
  });
}
