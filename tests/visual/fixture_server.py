"""Loopback-only deterministic server for PalDeck visual screenshot tests."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
ALLOWED_VIEWS = frozenset({"mods", "import", "nexus", "settings", "credits"})
ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
INJECTION_TAG = '<script type="module" src="/__fixture__.js"></script>'

FIXTURE_SCRIPT = r'''const allowed = new Set(["mods", "import", "nexus", "settings", "credits"]);
const requested = new URLSearchParams(window.location.search).get("view") || "mods";
if (!allowed.has(requested)) throw new TypeError("Unknown visual fixture view");
window.__VISUAL_READY__ = false;
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const waitUntil = async (predicate, label) => {
  for (let attempt = 0; attempt < 250; attempt += 1) {
    if (predicate()) return;
    await sleep(10);
  }
  throw new Error(`Visual fixture timed out waiting for ${label}`);
};
await waitUntil(() => {
  const health = document.querySelector("#healthStatus")?.textContent || "";
  const list = document.querySelector("#modList")?.textContent || "";
  return !health.includes("正在连接") && !list.includes("正在加载");
}, "initial API render");
document.querySelector(`[data-view="${requested}"]`)?.click();
const rendered = {
  mods: () => Boolean(document.querySelector("#modList")?.children.length),
  import: () => Boolean(document.querySelector("#dropzone")),
  nexus: () => {
    const status = document.querySelector("#nexusStatus")?.textContent || "";
    return !status.includes("等待加载") && !status.includes("正在连接") && Boolean(document.querySelector("#nexusGrid")?.children.length);
  },
  settings: () => {
    const status = document.querySelector("#ue4ssStatus")?.textContent || "";
    return Boolean(status) && !status.includes("未知") && !status.includes("正在读取");
  },
  credits: () => Boolean(document.querySelector("#creditsCore")?.children.length),
};
await waitUntil(() => {
  const target = document.querySelector(`#view-${requested}`);
  return target?.classList.contains("active") && rendered[requested]();
}, `${requested} view render`);
await document.fonts?.ready;
await Promise.all([...document.images].map((image) => image.complete ? null : new Promise((resolve) => {
  image.addEventListener("load", resolve, { once: true });
  image.addEventListener("error", resolve, { once: true });
})));
await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
document.documentElement.dataset.visualReady = "true";
window.__VISUAL_READY__ = true;
'''


def load_fixtures() -> dict[str, dict]:
    return {
        view: json.loads((FIXTURES / f"{view}.json").read_text(encoding="utf-8"))
        for view in ALLOWED_VIEWS
    }


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "PalDeckVisualFixture/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _selected_view(self) -> str:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == "visual_fixture_view" and value in ALLOWED_VIEWS:
                return value
        return "mods"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            query = parse_qs(parsed.query)
            view = query.get("view", ["mods"])[0]
            if view not in ALLOWED_VIEWS:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "unknown fixture view"})
                return
            html = (FRONTEND / "index.html").read_text(encoding="utf-8")
            html = html.replace("</body>", f"  {INJECTION_TAG}\n</body>")
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", f"visual_fixture_view={view}; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/__fixture__.js":
            self._send(HTTPStatus.OK, FIXTURE_SCRIPT.encode("utf-8"), "text/javascript; charset=utf-8")
            return
        if path.startswith("/api/"):
            fixture = self.server.fixtures[self._selected_view()]  # type: ignore[attr-defined]
            api = fixture["api"]
            if path == "/api/appearance/background/current":
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "fixture has no background image"})
                return
            if path not in api:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown fixture endpoint"})
                return
            self._json(HTTPStatus.OK, {"ok": True, "data": api[path]})
            return

        relative = path.lstrip("/")
        target = (FRONTEND / relative).resolve()
        try:
            target.relative_to(FRONTEND.resolve())
        except ValueError:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "text/javascript"}:
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 chooses a random free port")
    parser.add_argument("--ready-file", type=Path, help="optional file receiving the selected port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")

    server = ThreadingHTTPServer((HOST, args.port), FixtureHandler)
    server.fixtures = load_fixtures()  # type: ignore[attr-defined]
    port = server.server_address[1]
    if args.ready_file:
        args.ready_file.write_text(str(port), encoding="ascii")
    print(f"READY http://{HOST}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
