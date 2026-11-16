"""Local Anthropic-compatible forwarder that meters Z.ai response usage."""
from __future__ import annotations

import http.client
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class GLMUsageMeter:
    """Accumulate Z.ai token usage and estimated pay-as-you-go spend."""

    def __init__(self, *, max_cost_usd: float | None = None):
        self.max_cost_usd = max_cost_usd
        self.input_rate = float(os.environ.get("GLM_INPUT_USD_PER_MTOK", "0.15"))
        self.output_rate = float(os.environ.get("GLM_OUTPUT_USD_PER_MTOK", "0.50"))
        # Until cache-specific rates are configured, charge cached input at the
        # regular input rate so the meter does not understate spend.
        self._lock = threading.Lock()
        self.input_tokens = self.output_tokens = self.cache_read_tokens = self.cache_write_tokens = 0
        self.responses_with_usage = 0
        self.responses_without_usage = 0
        self.on_usage = None

    def add_usage(self, usage: dict) -> None:
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        if not isinstance(inp, int) or isinstance(inp, bool) or not isinstance(out, int) or isinstance(out, bool):
            with self._lock:
                self.responses_without_usage += 1
            return
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        if not isinstance(cache_read, int) or isinstance(cache_read, bool):
            cache_read = 0
        if not isinstance(cache_write, int) or isinstance(cache_write, bool):
            cache_write = 0
        with self._lock:
            self.input_tokens += inp
            self.output_tokens += out
            self.cache_read_tokens += cache_read
            self.cache_write_tokens += cache_write
            self.responses_with_usage += 1
            total_input = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
            spent = (total_input * self.input_rate + self.output_tokens * self.output_rate) / 1_000_000
            current_cost = ((inp + cache_read + cache_write) * self.input_rate + out * self.output_rate) / 1_000_000
            record = {
                "usage": dict(usage),
                "response_estimated_cost_usd": current_cost,
                "cumulative_estimated_spend_usd": spent,
                "estimated_remaining_usd": None if self.max_cost_usd is None else max(0.0, self.max_cost_usd - spent),
                "responses_with_usage": self.responses_with_usage,
                "model": usage.get("model"),
            }
        if self.on_usage is not None:
            self.on_usage(record)

    def mark_missing_usage(self) -> None:
        with self._lock:
            self.responses_without_usage += 1

    def status(self) -> dict:
        with self._lock:
            total_input = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
            if not self.responses_with_usage or self.responses_without_usage:
                spent = None
            else:
                spent = ((self.input_tokens + self.cache_read_tokens + self.cache_write_tokens) * self.input_rate
                         + self.output_tokens * self.output_rate) / 1_000_000
            return {
                "estimated_spent_usd": spent,
                "estimated_remaining_usd": None if spent is None or self.max_cost_usd is None else max(0.0, self.max_cost_usd - spent),
                "budget_usd": self.max_cost_usd,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "responses_with_usage": self.responses_with_usage,
                "responses_without_usage": self.responses_without_usage,
                "tracking": "Z.ai final response usage; cost estimated with configured per-million-token rates",
            }


class GLMAccountingProxy:
    """Forward a Claude Code session through localhost and meter final SSE usage."""

    def __init__(self, upstream_base_url: str, api_key: str, meter: GLMUsageMeter):
        self.upstream = urlsplit(upstream_base_url)
        if self.upstream.scheme != "https" or not self.upstream.hostname:
            raise ValueError("GLM upstream must be an https URL")
        self.api_key = api_key
        self.meter = meter
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "GLMAccountingProxy":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._forward()

            def do_POST(self):
                self._forward()

            def do_DELETE(self):
                self._forward()

            def log_message(self, fmt, *args):
                return

            def _forward(self):
                meter_request = self.path.split("?", 1)[0].endswith("/v1/messages")
                headers_sent = False
                status = outer.meter.status()
                if status["responses_without_usage"]:
                    body = json.dumps({"type": "error", "error": {"type": "usage_unavailable", "message": "TaskForge cannot safely continue because Z.ai usage was missing from a completed response"}}).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if outer.meter.max_cost_usd is not None and status["estimated_spent_usd"] is not None and status["estimated_spent_usd"] >= outer.meter.max_cost_usd:
                    body = json.dumps({"type": "error", "error": {"type": "budget_exceeded", "message": "TaskForge GLM spend limit reached"}}).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    request_body = self.rfile.read(length) if length else None
                    path = outer.upstream.path.rstrip("/") + self.path
                    conn = http.client.HTTPSConnection(outer.upstream.hostname, outer.upstream.port or 443, timeout=600)
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length", "connection", "authorization", "x-api-key", "accept-encoding"}}
                    headers["accept-encoding"] = "identity"
                    headers["x-api-key"] = outer.api_key
                    headers["host"] = outer.upstream.hostname
                    conn.request(self.command, path, body=request_body, headers=headers)
                    response = conn.getresponse()
                    response_headers = {k.lower(): v for k, v in response.getheaders()}
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"connection", "transfer-encoding", "content-length", "content-encoding"}:
                            self.send_header(key, value)
                    if response_headers.get("content-type", "").startswith("text/event-stream"):
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        headers_sent = True
                        pending = b""
                        saw_usage = False
                        while True:
                            chunk = response.read1(8192)
                            if not chunk:
                                break
                            pending += chunk
                            normalized = pending.replace(b"\r\n", b"\n")
                            while b"\n\n" in normalized:
                                event, remainder = normalized.split(b"\n\n", 1)
                                pending = remainder
                                normalized = remainder
                                saw_usage = outer._read_sse_event(event) or saw_usage
                            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                            self.wfile.flush()
                        if pending:
                            saw_usage = outer._read_sse_event(pending.replace(b"\r\n", b"\n")) or saw_usage
                        if meter_request and response.status < 400 and not saw_usage:
                            outer.meter.mark_missing_usage()
                        self.wfile.write(b"0\r\n\r\n")
                    else:
                        body = response.read()
                        if response_headers.get("content-type", "").startswith("application/json"):
                            try:
                                data = json.loads(body)
                                has_usage = isinstance(data, dict) and isinstance(data.get("usage"), dict)
                                if has_usage:
                                    outer.meter.add_usage(data["usage"])
                            except (ValueError, TypeError):
                                pass
                            else:
                                if meter_request and response.status < 400 and not has_usage:
                                    outer.meter.mark_missing_usage()
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        headers_sent = True
                        self.wfile.write(body)
                    conn.close()
                except Exception as exc:
                    if headers_sent:
                        self.close_connection = True
                        return
                    body = json.dumps({"type": "error", "error": {"type": "proxy_error", "message": str(exc)}}).encode()
                    try:
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def base_url(self) -> str:
        if self.server is None:
            raise RuntimeError("proxy is not running")
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, exc_type, exc, tb):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _read_sse_event(self, event: bytes) -> bool:
        event_type = ""
        data_lines = []
        for line in event.split(b"\n"):
            if line.startswith(b"event:"):
                event_type = line[6:].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if event_type != "message_delta" or not data_lines:
            return False
        try:
            data = json.loads(b"\n".join(data_lines))
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                self.meter.add_usage(usage)
                return isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int)
        except (ValueError, TypeError):
            pass
        return False
