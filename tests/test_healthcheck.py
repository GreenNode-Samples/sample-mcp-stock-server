"""Container healthcheck: it must probe the address the server really listens on (HOST / PORT)."""

import http.server
import os
import subprocess
import sys
import threading
from pathlib import Path

import healthcheck
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("env,url", [
    ({}, "http://127.0.0.1:8080/health"),
    ({"HOST": ""}, "http://127.0.0.1:8080/health"),
    ({"HOST": "0.0.0.0", "PORT": "9000"}, "http://127.0.0.1:9000/health"),
    ({"HOST": "::"}, "http://127.0.0.1:8080/health"),
    ({"HOST": "10.0.0.7", "PORT": "8081"}, "http://10.0.0.7:8081/health"),
    ({"HOST": "::1"}, "http://[::1]:8080/health"),
])
def test_probe_url_follows_host_and_port(env, url):
    assert healthcheck.probe_url(env) == url


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_probe(port: int, **env) -> int:
    clean = {k: v for k, v in os.environ.items() if k not in ("HOST", "PORT")}
    script = ROOT / "src" / "mcp_server" / "healthcheck.py"
    return subprocess.run([sys.executable, str(script)], env={**clean, "PORT": str(port), **env}, timeout=30).returncode


def test_probe_exit_code_against_a_server_bound_to_a_specific_address():
    server = http.server.HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        assert run_probe(port) == 0                          # HOST unset -> 127.0.0.1
        assert run_probe(port, HOST="127.0.0.1") == 0        # HOST set to the listening address
        assert run_probe(port, HOST="127.0.0.2") != 0        # HOST points elsewhere -> unhealthy
    finally:
        server.shutdown()
        server.server_close()
    assert run_probe(port) != 0                              # nothing listening any more


def test_image_and_compose_files_use_the_healthcheck_script():
    assert "healthcheck.py" in (ROOT / "Dockerfile").read_text()
    for compose in ("vserver", "onprem"):
        text = (ROOT / "deploy" / compose / "docker-compose.yml").read_text()
        assert '"healthcheck.py"' in text and "urlopen" not in text
