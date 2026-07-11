export class ApiError extends Error {
  constructor(message, { status = 0, code = "request_failed", details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const STATUS_MESSAGES = Object.freeze({
  429: "请求过于频繁，请稍后再试",
  423: "游戏正在运行，请关闭游戏后重试",
  409: "操作存在冲突，请确认后继续",
});

export async function request(path, options = {}) {
  const { timeout = 15000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const headers = new Headers(fetchOptions.headers || {});
  let body = fetchOptions.body;
  if (body && !(body instanceof FormData) && typeof body === "object") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  try {
    const response = await fetch(path, { ...fetchOptions, headers, body, signal: controller.signal });
    let payload = null;
    try { payload = await response.json(); } catch { /* handled below */ }
    if (!response.ok || !payload?.ok) {
      const error = payload?.error;
      throw new ApiError(
        (typeof error === "string" && error) || error?.message || STATUS_MESSAGES[response.status] || `请求失败 (${response.status})`,
        { status: response.status, code: payload?.error_code || payload?.code || error?.code || "request_failed", details: payload?.details || error?.details || null },
      );
    }
    return payload.data === undefined ? payload : payload.data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error?.name === "AbortError") throw new ApiError("请求超时，请重试", { code: "timeout" });
    throw new ApiError("无法连接本地服务", { code: "network_error", details: String(error) });
  } finally {
    clearTimeout(timer);
  }
}
