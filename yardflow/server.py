"""标准库 HTTP/JSON 适配：把 :class:`~yardflow.service.Service` 暴露为一个服务。

启动::

    python3 -m yardflow.server --dir samples --port 8080

只依赖标准库。所有业务规则仍在 ``Service`` 中，HTTP 层只做路由与 JSON 编解码。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import RuleViolation, Service, _jsonable

# 允许通过 POST /api/action 调用的办理动作（白名单，避免任意方法调用）。
ACTIONS = {
    "confirm_materials",
    "authorize_agent",
    "create_submission",
    "record_submission",
    "split_and_redeclare",
    "ingest_receipt",
    "ingest_fleet",
    "ingest_invoice",
    "ingest_yard_presence",
    "form_run",
    "add_to_run",
    "confirm_run",
    "remove_from_run",
    "stage",
    "depart",
    "mark_handling",
    "release_hold",
}

INDEX_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>整车拼报拆单与分票核放</title>
<style>
body{font-family:system-ui,"Noto Sans CJK SC",sans-serif;margin:24px;color:#1a1a1a}
h1{font-size:20px} h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f4f4f4} .bad{color:#b00020;font-weight:600} .ok{color:#1b7f3b}
.muted{color:#666} code{background:#f6f6f6;padding:1px 4px}
</style></head><body>
<h1>整车拼报拆单与分票核放 · 值班/单证看板</h1>
<p class="muted">数据来自启动时载入的交换资料；命令行 <code>python3 -m yardflow</code>
与 JSON 接口 <code>/api/report</code> 使用同一套规则。可加 <code>?at=...</code> 重放时点。</p>
<div id="app">加载中…</div>
<script>
const q = new URLSearchParams(location.search);
const at = q.get('at');
fetch('/api/report' + (at ? '?at=' + encodeURIComponent(at) : ''))
 .then(r => r.json()).then(d => {
  const rel = d.releasable_vins.length ? d.releasable_vins.join(', ') : '（无）';
  let html = `<p>时点 <code>${d.as_of}</code>　当前可核放：<b class="ok">${rel}</b></p>`;
  html += '<h2>逐车状态</h2><table><tr><th>VIN</th><th>物理状态</th><th>班车</th>'
       + '<th>有效依据</th><th>可核放</th><th>问题/待办/后续</th></tr>';
  for (const v of d.vins) {
    const basis = v.basis && v.basis.permit ? v.basis.permit : '—';
    const items = []
      .concat((v.issues||[]).map(i=>'<span class="bad">! '+i.code+'</span>'))
      .concat((v.todos||[]).map(t=>'→ '+(t.text||t.code)))
      .concat(v.follow_up ? ['<span class="bad">⚑ ['+v.follow_up.owner+'] '+v.follow_up.instruction+'</span>'] : []);
    html += `<tr><td>${v.vin}</td><td>${v.physical_state}</td><td>${v.run||''}</td>`
         + `<td>${basis}</td><td class="${v.releasable?'ok':'bad'}">${v.releasable?'是':'否'}</td>`
         + `<td>${items.join('<br>')}</td></tr>`;
  }
  html += '</table><h2>已装车但无有效依据（待卸 / 留场）</h2>';
  if (!d.unsupported_staged.length) html += '<p>无</p>';
  else { html += '<table><tr><th>VIN</th><th>班车</th><th>原因</th><th>处理</th></tr>';
    for (const u of d.unsupported_staged)
      html += `<tr><td>${u.vin}</td><td>${u.run}</td><td class="bad">${u.reason}</td>`
           + `<td>${u.handling.join(' / ')}</td></tr>`;
    html += '</table>'; }
  html += '<h2>申报行单证对账（JSON）</h2><pre>'
       + JSON.stringify(d.lines, null, 2) + '</pre>';
  document.getElementById('app').innerHTML = html;
});
</script></body></html>
"""


def create_server(directory: str | Path = "samples", *, host: str = "127.0.0.1",
                  port: int = 8080, clock: str | None = None) -> ThreadingHTTPServer:
    svc = Service(clock=clock)
    svc.load_directory(directory)
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "YardFlow/1.0"

        def log_message(self, fmt, *args):  # 保持测试输出安静
            pass

        def _send(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _query(self):
            parsed = urlparse(self.path)
            return parsed.path, {k: v[-1] for k, v in parse_qs(parsed.query).items()}

        def do_GET(self):
            path, query = self._query()
            at = query.get("at")
            try:
                if path == "/" or path == "/index.html":
                    body = INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                with lock:
                    if path == "/api/report":
                        return self._send(200, svc.report(at))
                    if path == "/api/releasable":
                        return self._send(200, {"vins": svc.releasable_vins(at)})
                    if path == "/api/unsupported":
                        return self._send(200, {"items": svc.unsupported_staged(at)})
                    if path == "/api/lines":
                        return self._send(200, {"lines": svc.dossiers(at)})
                    if path.startswith("/api/vin/"):
                        vin = path.rsplit("/", 1)[-1]
                        return self._send(200, svc.vin_dossier(vin, at))
                    if path.startswith("/api/line/"):
                        parts = path.strip("/").split("/")
                        # /api/line/<submission>/<line>
                        if len(parts) >= 4:
                            return self._send(200, svc.line_dossier(parts[2], parts[3], at))
                    if path == "/api/health":
                        return self._send(200, {"ok": True})
                self._send(404, {"ok": False, "error": "not_found", "path": path})
            except KeyError as exc:
                self._send(404, {"ok": False, "error": "not_found", "detail": str(exc)})
            except RuleViolation as exc:
                self._send(409, exc.to_dict())
            except Exception as exc:  # noqa: BLE001 - 边界层兜底
                self._send(400, {"ok": False, "error": type(exc).__name__, "detail": str(exc)})

        def do_POST(self):
            path, _ = self._query()
            if path != "/api/action":
                return self._send(404, {"ok": False, "error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                action = payload.pop("action", None)
                if action not in ACTIONS:
                    return self._send(400, {"ok": False, "error": "unknown_action",
                                           "allowed": sorted(ACTIONS)})
                # 接入类动作允许 {"action":"ingest_receipt","record":{...}}
                if action.startswith("ingest_") and "record" in payload:
                    record = payload.pop("record")
                    payload.update(record if isinstance(record, dict) else {})
                with lock:
                    result = getattr(svc, action)(**payload)
                if result is None:
                    result = {"ok": True}
                return self._send(200, _jsonable(result))
            except RuleViolation as exc:
                self._send(409, exc.to_dict())
            except TypeError as exc:
                self._send(400, {"ok": False, "error": "bad_arguments", "detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"ok": False, "error": type(exc).__name__, "detail": str(exc)})

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = svc  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整车核放服务 HTTP/JSON 适配")
    parser.add_argument("--dir", default="samples")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--at", default=None, help="固定服务时钟（可选）")
    args = parser.parse_args(argv)
    httpd = create_server(args.dir, host=args.host, port=args.port, clock=args.at)
    print(f"yardflow serving on http://{args.host}:{args.port} (dir={args.dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
