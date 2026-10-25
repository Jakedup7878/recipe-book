"""API Gateway only forwards routes listed in infra/recipe_stack.py, so that list
must match the FastAPI app exactly, and only /public/* may skip sign-in."""

import os
import sys

from fastapi.routing import APIRoute

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "infra"))

import devtools  # noqa: E402

devtools.use_fake_aws("recipes-test")

import main  # noqa: E402
from recipe_stack import ROUTES  # noqa: E402


def test_gateway_routes_match_fastapi():
    # Local-dev-only routes (config.json, the fake Cognito) are hidden from the schema.
    fastapi_routes = {(method, route.path) for route in main.app.routes
                      if isinstance(route, APIRoute) and route.include_in_schema
                      for method in route.methods}
    gateway_routes = {(method, path) for path, method, _ in ROUTES}
    assert gateway_routes == fastapi_routes


def test_only_public_routes_skip_sign_in():
    for path, method, signed_in in ROUTES:
        assert signed_in != path.startswith("/public/"), f"{method} {path}"
