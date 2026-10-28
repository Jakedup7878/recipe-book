"""Checks on the plain-JS frontend that don't need a browser: every API path
app.js calls is a route API Gateway forwards, the scripts parse, and the HTML
pages link files and anchors that exist and keep to the site's CSP."""

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


PAGES = ["index.html", "privacy.html", "terms.html"]
POLICY_PAGES = ["privacy.html", "terms.html"]


def read(name):
    with open(os.path.join(FRONTEND, name), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("page", PAGES)
def test_pages_reference_files_that_exist(page):
    # A missing file isn't a 404 on the live site: private S3 behind CloudFront answers 403.
    # Local paths only (no mailto: or https:), without any #anchor; "/" is the home page.
    refs = {ref.lstrip("/") for ref in re.findall(r'(?:href|src)="(?![a-z]+:)([^"#]*)', read(page))} - {""}
    assert {"favicon.ico", "favicon.svg", "apple-touch-icon.png", "styles.css", "theme.js"} <= refs
    if page == "index.html":
        assert {"app.js", "privacy.html", "terms.html"} <= refs
    assert [r for r in refs if not os.path.isfile(os.path.join(FRONTEND, r))] == []
    # Browsers also fetch /favicon.ico on their own, whatever the page links.
    assert os.path.isfile(os.path.join(FRONTEND, "favicon.ico"))


@pytest.mark.parametrize("page", PAGES)
def test_pages_keep_to_the_content_security_policy(page):
    # The CSP (infra/recipe_stack.py) allows only same-origin scripts and styles:
    # inline ones would be blocked on the live site but still work locally.
    html = read(page)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert "<style" not in html
    assert not re.search(r'\sstyle="', html), "inline style attribute"
    assert not re.search(r'\son[a-z]+="', html), "inline event handler"


@pytest.mark.parametrize("page", PAGES)
def test_every_page_links_both_policies_in_the_footer(page):
    footer = re.search(r'<footer class="site-footer">(.*?)</footer>', read(page), re.S).group(1)
    assert 'href="privacy.html"' in footer
    assert 'href="terms.html"' in footer


@pytest.mark.parametrize("page", POLICY_PAGES)
def test_policy_anchor_links_point_at_headings_that_exist(page):
    # Covers the contents lists and the cross-links such as terms.html#who-can-use.
    html = read(page)
    links = re.findall(r'href="([a-z]*\.html)?#([\w-]+)"', html)
    assert len(links) > 10
    ids = {name: set(re.findall(r'\sid="([\w-]+)"', read(name))) for name in POLICY_PAGES}
    missing = [f"{target}#{anchor}" for target, anchor in links if anchor not in ids[target or page]]
    assert missing == []


@pytest.mark.parametrize("page", POLICY_PAGES)
def test_policy_pages_have_a_date_and_a_contact(page):
    html = read(page)
    assert re.search(r"Effective \w+ \d{1,2}, \d{4}", html)
    assert 'href="mailto:support@familyfoodiebook.com"' in html


def test_every_page_link_goes_to_a_page_the_router_handles():
    # A link to a page route() doesn't know silently falls through to the home feed.
    source = read("app.js")
    router = source[source.index("async function route()"):source.index("// Errors thrown from form")]
    handled = set(re.findall(r'page === "([a-z]+)"', router)) | {""}
    # Every "#/page..." string in the app, except prefix checks like startsWith("#/sign").
    linked = set(re.findall(r'(?<!startsWith\()[`"]#/([a-z]*)', source))
    assert {"mine", "family", "recipe", "new", "edit", "signin", "signup"} <= linked
    assert linked - handled == set()


def test_sign_up_requires_agreeing_to_both_policies():
    source = read("app.js")
    sign_up = source[source.index("function signUpView()"):source.index("function confirmView()")]
    assert sign_up.count('h("input", { type: "checkbox", required: true })') == 2
    assert 'policyLink("terms.html", "Terms of Service")' in sign_up
    assert 'policyLink("privacy.html", "Privacy Policy")' in sign_up
    # The handler also refuses to call Cognito until both are ticked.
    assert sign_up.index("!terms.checked || !privacy.checked") < sign_up.index('cognito("SignUp"')


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js isn't installed")
@pytest.mark.parametrize("script", ["app.js", "theme.js"])
def test_frontend_scripts_parse(script):
    result = subprocess.run(["node", "--check", os.path.join(FRONTEND, script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
