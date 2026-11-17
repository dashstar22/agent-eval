"""真正回环 socket 协议验证；服务端仍为协议替身，不冒充真实模型。"""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from agenteval.adapters import HttpAdapter, PrinterHttpAdapter
from agenteval.config import ConnectionConfig
from test_printer import PrinterProtocol


@contextmanager
def server(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            callback(self)

        do_POST = do_GET

        def log_message(self, *_):
            pass

    local = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    local.daemon_threads = True
    thread = threading.Thread(target=local.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{local.server_port}"
    finally:
        local.shutdown()
        local.server_close()
        thread.join(timeout=2)


def test_printer_real_http_cookie_csrf_order_and_observation_roundtrip():
    protocol = PrinterProtocol()
    def handle(req):
        body = req.rfile.read(int(req.headers.get("Content-Length", 0)))
        response = protocol(httpx.Request(req.command, "http://localhost" + req.path, headers=dict(req.headers), content=body))
        req.send_response(response.status_code)
        for key, value in response.headers.items():
            req.send_header(key, value)
        req.end_headers()
        req.wfile.write(response.content)
    with server(handle) as url:
        result = PrinterHttpAdapter(ConnectionConfig(base_url=url, source_type="PROTOCOL_DOUBLE"),
            environment={"PRINTER_USERNAME": "test-user", "PRINTER_PASSWORD": "private-pass"}).run("知识问题", protocol.saved.append, order_id=7)
    assert result.agent_status == "SUCCEEDED" and result.observation_status == "COMPLETE"
    assert len(protocol.turns) == 1 and protocol.reads == 1 and not result.model_execution_verified


def test_absolute_deadline_cancels_slow_drip_even_when_each_read_is_fast():
    def handle(req):
        req.rfile.read(int(req.headers.get("Content-Length", 0)))
        body = json.dumps({"answer": "slow response"}).encode()
        req.send_response(200)
        req.send_header("Content-Length", str(len(body)))
        req.end_headers()
        try:
            for byte in body:
                req.wfile.write(bytes([byte]))
                req.wfile.flush()
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    with server(handle) as url:
        started = time.monotonic()
        result = HttpAdapter(ConnectionConfig(base_url=url, source_type="PROTOCOL_DOUBLE", request_timeout_seconds=0.11, case_timeout_seconds=2.0)).run("q")
        elapsed = time.monotonic() - started
    assert result.agent_status == "TIMEOUT" and elapsed < 1.2


def test_real_http_body_limit_retains_no_fabricated_answer():
    def handle(req):
        req.rfile.read(int(req.headers.get("Content-Length", 0)))
        body = json.dumps({"answer": "x" * 1024}).encode()
        req.send_response(200)
        req.send_header("Content-Length", str(len(body)))
        req.end_headers()
        req.wfile.write(body)
    with server(handle) as url:
        result = HttpAdapter(ConnectionConfig(base_url=url, source_type="PROTOCOL_DOUBLE", response_max_bytes=256)).run("q")
    assert result.error_code == "RESPONSE_TOO_LARGE" and result.answer is None
