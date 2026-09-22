"""hello-app: the smallest thing Gojira can deploy. Zero dependencies.

It honours the platform contract every generated manifest assumes:
listens on 8080, answers /health (the web class's readiness + liveness
probe) and /metrics (what the generated PodMonitoring scrapes).
"""

import http.server
import os
import socketserver

PORT = 8080
_requests = 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _requests
        _requests += 1
        if self.path == "/health":
            self._respond("ok\n")
        elif self.path == "/metrics":
            self._respond(
                "# HELP hello_requests_total Requests served.\n"
                "# TYPE hello_requests_total counter\n"
                f"hello_requests_total {_requests}\n",
                "text/plain; version=0.0.4",
            )
        else:
            self._respond(f"hello from {os.environ.get('HOSTNAME', 'hello-app')}\n")

    def _respond(self, body: str, content_type: str = "text/plain"):
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        print(f"hello-app listening on :{PORT}", flush=True)
        httpd.serve_forever()
