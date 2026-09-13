"""Private-network CDP sidecar. Do not publish its port to the Internet."""
import subprocess
import time
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

if any(k.startswith('WH_') for k in os.environ):
    raise SystemExit('Browser service must not receive WORM application variables')

with sync_playwright() as p:
    # No sandbox here: the dedicated container is the boundary. It holds no application secrets
    # or database mounts. The signer never runs a no-sandbox browser locally.
    browser = p.chromium.launch(headless=True, args=['--remote-debugging-port=9222', '--disable-dev-shm-usage'])
    # Railway private networking can use IPv6; accept both families without opening a public route.
    relay = subprocess.Popen(['socat', 'TCP6-LISTEN:9223,ipv6only=0,reuseaddr,fork', 'TCP:127.0.0.1:9222'])
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200 if self.path == '/healthz' and browser.is_connected() and relay.poll() is None else 503
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'ok' if status == 200 else b'unavailable')

        def log_message(self, *_):
            pass

    class HealthServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    health = HealthServer(('::', int(os.environ.get('PORT', '8080'))), HealthHandler)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    try:
        while browser.is_connected() and relay.poll() is None:
            time.sleep(1)
    finally:
        health.shutdown()
        relay.terminate()
        browser.close()
    raise SystemExit('Browser connection ended; service restart required')
