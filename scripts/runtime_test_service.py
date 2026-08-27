"""Serviço de teste seguro e reversível para o Runtime Supervisor (ACT -> VERIFY).

HTTP mínimo em 127.0.0.1:18765 com /health retornando ok. Long-running por design.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 18765


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        payload = {"status": "online", "service": "nyra_test_service"}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # silence access logs
        sys.stdout.write(f"[runtime_test_service] {format % args}\n")
        sys.stdout.flush()


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[runtime_test_service] online", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
