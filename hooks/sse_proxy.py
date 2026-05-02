"""
sse_proxy.py

Sits between Claude Code and the Anthropic API.
Watches the SSE token stream in real time.
Writes signal files when context thresholds are crossed.

Usage:
  python sse_proxy.py

Then in Claude Code settings.json, add to the env section:
  ANTHROPIC_BASE_URL=http://127.0.0.1:9099

The Stop hook (stop_hook_checkpoint.py) reads the signal files
and triggers checkpoint writes when bells are rung.
"""

import json
import ssl
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path

PROXY_PORT = 9099
ANTHROPIC_API_BASE = "https://api.anthropic.com"

SIGNALS_DIR = Path.home() / ".claude" / "hooks" / "signals"
LOG = Path.home() / ".claude" / "hooks" / "sse_proxy.log"

# Context percentage thresholds that ring bells
THRESHOLDS = [70, 85, 95]

# Known context window sizes by model substring
MODEL_CONTEXTS = {
    "claude-opus-4-7": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-haiku-4-5": 200000,
}
DEFAULT_CONTEXT = 200000


def log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().isoformat()[:19], msg))
    except Exception:
        pass


def write_signal(level, token_count, max_tokens):
    try:
        SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        path = SIGNALS_DIR / "bell_{}.signal".format(level)
        data = {
            "level": level,
            "token_count": token_count,
            "max_tokens": max_tokens,
            "percentage": round(token_count / max_tokens * 100, 1),
            "timestamp": datetime.now().isoformat(),
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        log("BELL {}: {}/{} ({:.1f}%)".format(
            level, token_count, max_tokens, data["percentage"]))
    except Exception as e:
        log("Signal write error: {}".format(e))


def get_max_tokens(model_str):
    for key, val in MODEL_CONTEXTS.items():
        if key in model_str:
            return val
    return DEFAULT_CONTEXT


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default access log

    def do_GET(self):
        self._forward("GET", b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self._forward("POST", body)

    def _forward(self, method, body):
        target_url = "{}{}".format(ANTHROPIC_API_BASE, self.path)

        # Detect model from request body
        max_tokens = DEFAULT_CONTEXT
        try:
            req_data = json.loads(body)
            model_str = req_data.get("model", "")
            max_tokens = get_max_tokens(model_str)
        except Exception:
            pass

        # Build forwarded headers
        skip = {"host", "content-length", "transfer-encoding", "connection"}
        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in skip
        }

        try:
            req = urllib.request.Request(
                target_url,
                data=body if body else None,
                headers=fwd_headers,
                method=method,
            )

            ssl_ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ssl_ctx) as resp:
                self.send_response(resp.status)
                skip_resp = {"transfer-encoding", "connection"}
                for key, val in resp.headers.items():
                    if key.lower() not in skip_resp:
                        self.send_header(key, val)
                self.send_header("Connection", "close")
                self.end_headers()

                buf = b""
                input_tokens = 0
                thresholds_fired = set()

                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break

                    # Relay immediately -- don't hold up the stream
                    self.wfile.write(chunk)
                    self.wfile.flush()

                    # Accumulate and scan for complete SSE events
                    buf += chunk
                    while b"\n\n" in buf:
                        event_bytes, buf = buf.split(b"\n\n", 1)
                        for line in event_bytes.decode("utf-8", errors="replace").split("\n"):
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:].strip()
                            if data_str in ("[DONE]", ""):
                                continue
                            try:
                                event = json.loads(data_str)
                                etype = event.get("type", "")

                                if etype == "message_start":
                                    usage = event.get("message", {}).get("usage", {})
                                    input_tokens = (
                                        usage.get("input_tokens", 0)
                                        + usage.get("cache_read_input_tokens", 0)
                                        + usage.get("cache_creation_input_tokens", 0)
                                    )
                                    pct = (input_tokens / max_tokens) * 100
                                    log("message_start: tokens={} ({:.1f}%)".format(
                                        input_tokens, pct))

                                    for t in THRESHOLDS:
                                        if pct >= t and t not in thresholds_fired:
                                            thresholds_fired.add(t)
                                            write_signal(t, input_tokens, max_tokens)

                            except Exception:
                                pass

        except urllib.error.HTTPError as e:
            body_err = e.read()
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(body_err)
            log("HTTP error {}: {}".format(e.code, self.path))

        except Exception as e:
            log("Proxy error: {}".format(e))
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass


def main():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = HTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    except OSError:
        # Port already in use -- another instance is running, exit quietly
        log("Port {} already in use -- proxy already running.".format(PROXY_PORT))
        return
    log("Proxy started on port {}.".format(PROXY_PORT))
    print("SSE proxy running on http://127.0.0.1:{}".format(PROXY_PORT))
    print("Add to Claude Code environment:")
    print("  ANTHROPIC_BASE_URL=http://127.0.0.1:{}".format(PROXY_PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Proxy stopped.")
        print("\nProxy stopped.")


if __name__ == "__main__":
    main()
