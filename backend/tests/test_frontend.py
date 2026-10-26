"""Checks on the plain-JS frontend that don't need a browser: every API path
app.js calls is a route API Gateway forwards, and the scripts parse."""

import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
FRONTEND = os.path.join(ROOT, "frontend")
sys.path.insert(0, os.path.join(ROOT, "infra"))

from recipe_stack import ROUTES  # noqa: E402

# api("/path") or api(`/path/${id}`), optionally followed by { method: "POST" ... }.
API_CALL = re.compile(r'api\((["`])(/[^"`]*)\1(?:\s*,\s*\{\s*method:\s*"([A-Z]+)")?')


def frontend_calls():
    with open(os.path.join(FRONTEND, "app.js"), encoding="utf-8") as f:
        source = f.read()
    return [(method or "GET", path) for _, path, method in API_CALL.findall(source)]


def gateway_route_for(method, path):
    for route_path, route_method, _ in ROUTES:
        pattern = "^" + re.sub(r"\\\{\w+\\\}", r"[^/]+", re.escape(route_path)) + "$"
        if route_method == method and re.match(pattern, path):
            return route_path
    return None


def test_frontend_api_calls_have_gateway_routes():
    calls = frontend_calls()
    assert len(calls) > 15, "the regex stopped finding api() calls in app.js"
    # ${...} in a template literal stands for one path segment, like {id} in a route.
    missing = [(m, p) for m, p in calls if not gateway_route_for(m, re.sub(r"\$\{[^}]+\}", "x", p))]
    assert not missing


def test_index_html_references_files_that_exist():
    # A missing file isn't a 404 on the live site: private S3 behind CloudFront answers 403.
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
        refs = re.findall(r'(?:href|src)="([^"#:]+)"', f.read())
    assert {"favicon.ico", "favicon.svg", "apple-touch-icon.png", "styles.css", "app.js", "theme.js"} <= set(refs)
    assert [r for r in refs if not os.path.isfile(os.path.join(FRONTEND, r))] == []
    # Browsers also fetch /favicon.ico on their own, whatever the page links.
    assert os.path.isfile(os.path.join(FRONTEND, "favicon.ico"))


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js isn't installed")
@pytest.mark.parametrize("script", ["app.js", "theme.js"])
def test_frontend_scripts_parse(script):
    result = subprocess.run(["node", "--check", os.path.join(FRONTEND, script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
