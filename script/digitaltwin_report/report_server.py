import base64
import hmac
import html
import json
import mimetypes
import os
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sensor_dash_app import start_sensor_dash_if_enabled
from report_logic import (
    build_report,
    build_diagnostics,
    delete_s3_memory_key,
    get_s3_memory_key,
    get_analysis_for_lang,
    list_s3_memory_state,
    load_sensor_validate_from_s3,
    load_env_from_json,
    render,
    render_loading_page,
)

LAST_REPORT_TIMING: dict[str, Any] = {}


class Handler(BaseHTTPRequestHandler):
    def _auth_enabled(self) -> bool:
        return os.environ.get("REPORT_AUTH_ENABLED", "1") == "1"

    def _check_basic_auth(self) -> bool:
        if not self._auth_enabled():
            return True
        user = os.environ.get("REPORT_AUTH_USER", "admin")
        pwd = os.environ.get("REPORT_AUTH_PASSWORD", "123")
        expected = "Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, expected)

    def _check_memory_auth(self) -> bool:
        user = os.environ.get("REPORT_MEMORY_AUTH_USER", "admin")
        pwd = os.environ.get("REPORT_MEMORY_AUTH_PASSWORD", "admin123")
        expected = "Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, expected)

    def _need_auth(self, realm: str = "DigitalTwin Report") -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{realm}"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = "Unauthorized".encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, code: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, code: int = 200) -> None:
        if not path.exists() or not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path.startswith("/memory") or u.path.startswith("/api/memory"):
            if not self._check_memory_auth():
                self._need_auth("DigitalTwin Memory Admin")
                return
        elif u.path != "/health" and not self._check_basic_auth():
            self._need_auth("DigitalTwin Report")
            return

        q = parse_qs(u.query)
        report_date = q.get("date", [date.today().isoformat()])[0]
        lang = q.get("lang", ["zh"])[0]
        provider = q.get("provider", [os.environ.get("REPORT_LLM_PROVIDER", "deepseek")])[0].lower()
        mode = q.get("mode", ["fast"])[0].lower()
        refresh_flag = q.get("refresh", ["0"])[0].strip().lower() in {"1", "true", "yes", "y"}

        if provider not in ("deepseek", "glm"):
            provider = "deepseek"
        if lang not in ("zh", "en"):
            lang = "zh"
        if mode not in ("fast", "detailed"):
            mode = "fast"

        if u.path.startswith("/static/"):
            static_root = Path(__file__).resolve().parent / "static"
            req = u.path[len("/static/"):].lstrip("/")
            target = (static_root / req).resolve()
            if str(target).startswith(str(static_root.resolve())):
                self._file(target)
                return
            self._json({"error": "not found"}, 404)
            return

        if u.path == "/":
            self._html(render_loading_page(report_date, lang, provider, mode))
            return

        if u.path == "/report":
            t0 = time.perf_counter()
            report = build_report(report_date, lang)
            t1 = time.perf_counter()
            report["llm_provider"] = provider
            try:
                analysis = get_analysis_for_lang(report, lang, provider, mode, force_regen=refresh_flag)
            except Exception as exc:
                self._html(
                    f"<html><body><h2>LLM Required But Failed</h2><pre>{html.escape(str(exc))}</pre></body></html>",
                    502,
                )
                return
            t2 = time.perf_counter()
            html_out = render(report, lang, analysis, mode)
            t3 = time.perf_counter()
            LAST_REPORT_TIMING.clear()
            LAST_REPORT_TIMING.update({
                "date": report_date,
                "lang": lang,
                "provider": provider,
                "mode": mode,
                "build_report_sec": round(t1 - t0, 3),
                "llm_sec": round(t2 - t1, 3),
                "render_sec": round(t3 - t2, 3),
                "total_sec": round(t3 - t0, 3),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            self._html(html_out)
            return

        if u.path == "/api/report":
            t0 = time.perf_counter()
            report = build_report(report_date, lang)
            t1 = time.perf_counter()
            report["llm_provider"] = provider
            try:
                report["llm_analysis"] = get_analysis_for_lang(report, lang, provider, mode, force_regen=refresh_flag)
                t2 = time.perf_counter()
                report["html"] = render(report, lang, report["llm_analysis"], mode)
                t3 = time.perf_counter()
            except Exception as exc:
                self._json({"error": "llm_required_failed", "message": str(exc), "report": report}, 502)
                return
            timing = {
                "date": report_date,
                "lang": lang,
                "provider": provider,
                "mode": mode,
                "build_report_sec": round(t1 - t0, 3),
                "llm_sec": round(t2 - t1, 3),
                "render_sec": round(t3 - t2, 3),
                "total_sec": round(t3 - t0, 3),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            report["timing"] = timing
            LAST_REPORT_TIMING.clear()
            LAST_REPORT_TIMING.update(timing)
            self._json(report)
            return

        if u.path == "/api/diagnostics":
            d = build_diagnostics(lang)
            d["last_report_timing"] = LAST_REPORT_TIMING
            self._json(d)
            return

        if u.path == "/api/sensor/data":
            try:
                n = int(q.get("n", ["1200"])[0])
            except Exception:
                n = 1200
            self._json(load_sensor_validate_from_s3(max_points=max(100, min(5000, n))))
            return

        if u.path == "/sensor":
            dash_port = int(os.environ.get("REPORT_SENSOR_DASH_PORT", "8060"))
            embed = q.get("embed", ["0"])[0] == "1"
            title = "Sensor Validate (Dash)"
            self._html(
                f"""<!doctype html><html><head><meta charset="utf-8"/><title>{title}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#0b1220;color:#dbeafe;margin:0}}
.bar{{padding:10px 14px;background:#111a2d;border-bottom:1px solid #1f365b;display:flex;justify-content:space-between;align-items:center}}
.btn{{display:inline-block;padding:5px 10px;border:1px solid #4f7fb5;border-radius:999px;text-decoration:none;color:#dbeafe}}
iframe{{width:100%;height:{'760px' if embed else 'calc(100vh - 52px)'};border:0;background:#0b1220}}
</style></head><body>
<div class="bar"><div>{title}</div><div><a class="btn" href="/report">Back Report</a></div></div>
<iframe id="dashFrame" src=""></iframe>
<script>
const scheme = window.location.protocol === 'https:' ? 'https:' : 'http:';
const host = window.location.hostname;
const port = {dash_port};
document.getElementById('dashFrame').src = `${{scheme}}//${{host}}:${{port}}/`;
</script></body></html>"""
            )
            return

        if u.path == "/api/memory/list":
            try:
                limit = int(q.get("limit", ["200"])[0])
            except Exception:
                limit = 200
            self._json(list_s3_memory_state(limit=max(1, min(1000, limit))))
            return

        if u.path == "/api/memory/item":
            key = q.get("key", [""])[0].strip()
            if not key:
                self._json({"error": "invalid_key", "message": "key is required"}, 400)
                return
            try:
                self._json(get_s3_memory_key(key))
            except Exception as exc:
                self._json({"error": "get_failed", "message": str(exc)}, 500)
            return

        if u.path == "/memory":
            self._html(
                """<!doctype html><html><head><meta charset="utf-8"/><title>Memory Admin</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;background:#f7f9fc;color:#1f2937;margin:0;padding:20px}
.card{background:#fff;border:1px solid #d0d7e2;border-radius:12px;padding:14px;margin-bottom:10px}
button{padding:6px 10px;border:1px solid #3b82f6;background:#eff6ff;color:#1d4ed8;border-radius:8px;cursor:pointer}
table{width:100%;border-collapse:collapse}
th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;font-size:13px}
code{font-size:12px}
</style></head><body>
<div class="card"><h2>Memory Admin</h2><button onclick="loadData()">Refresh</button></div>
<div class="card"><div id="meta"></div></div>
<div class="card"><table><thead><tr><th>Key</th><th>Size</th><th>Last Modified</th><th>Action</th></tr></thead><tbody id="rows"></tbody></table></div>
<script>
async function loadData(){
  const r=await fetch('/api/memory/list?limit=300',{credentials:'same-origin'});
  const d=await r.json();
  document.getElementById('meta').innerHTML =
    '<div>bucket='+d.bucket+' | prefix='+d.prefix+'</div>' +
    '<div>index='+d.index_key+' | updated='+d.index_updated_at+' | index_items='+d.index_items+' | runs='+d.run_count+'</div>';
  const rows=(d.runs||[]).map(x=>`<tr><td><code>${x.key}</code></td><td>${x.size}</td><td>${x.last_modified}</td><td><button onclick="viewKey('${x.key.replace(/'/g, "\\'")}')">View</button> <button onclick="delKey('${x.key.replace(/'/g, "\\'")}')">Delete</button></td></tr>`).join('');
  document.getElementById('rows').innerHTML=rows;
}
function viewKey(key){
  const u='/api/memory/item?key='+encodeURIComponent(key);
  window.open(u, '_blank');
}
async function delKey(key){
  if(!confirm('Delete '+key+' ?')) return;
  const r=await fetch('/api/memory/delete',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
  const d=await r.json();
  if(!r.ok){ alert('Delete failed: '+(d.message||d.error||'unknown')); return; }
  await loadData();
}
loadData();
</script></body></html>"""
            )
            return

        if u.path == "/health":
            self._json({"ok": True, "service": "opencode_digitaltwin_report"})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path.startswith("/api/memory"):
            if not self._check_memory_auth():
                self._need_auth("DigitalTwin Memory Admin")
                return
        else:
            if not self._check_basic_auth():
                self._need_auth("DigitalTwin Report")
                return

        if u.path == "/api/memory/delete":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(max(0, length))
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                key = str(payload.get("key", "")).strip()
                if not key:
                    self._json({"error": "invalid_key", "message": "key is required"}, 400)
                    return
                self._json(delete_s3_memory_key(key))
                return
            except Exception as exc:
                self._json({"error": "delete_failed", "message": str(exc)}, 500)
                return

        self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    load_env_from_json()
    dash_state = start_sensor_dash_if_enabled()
    host = os.environ.get("REPORT_HOST", "127.0.0.1")
    port = int(os.environ.get("REPORT_PORT", "8003"))
    server = ThreadingHTTPServer((host, port), Handler)
    if dash_state.get("enabled"):
        print(
            "sensor dash status: "
            f"available={dash_state.get('available')} "
            f"url=http://{dash_state.get('host')}:{dash_state.get('port')}"
        )
    print(f"digitaltwin report server listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
