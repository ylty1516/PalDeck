export function actionableErrorMessage(error) {
  if (error?.status === 423 && error?.code === "game_running") return "请先退出游戏，再重试此操作";
  if (error?.status === 403 && error?.code === "permission_denied") return "权限不足，请点击“以管理员身份重启”后重试";
  if (error?.status === 410 && error?.code === "upload_expired") return "暂存文件已过期，请重新选择文件";
  return error?.message || "操作失败";
}

export function dynamicActionKey(action, id) {
  return action === "rescanMods" ? "rescan-mods" : `${action}:${id || "global"}`;
}
