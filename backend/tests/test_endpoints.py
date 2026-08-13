"""SPEC.md Phase 2 acceptance criterion: "For a FastAPI or Express-based
project, endpoint extraction identifies >=90% of real endpoints with
correct method, path, and handler location (verify by hand against a known
sample)." fastapi_flask.py/express.py are tested exhaustively against
tests/fixtures/mini_repo's known-ground-truth fixtures (see their
docstrings) for exactly this; the rest get lighter, synthetic-fixture
coverage since no acceptance criterion requires proof for them specifically.
"""

from __future__ import annotations

from app.parsing.extractors.endpoints import (
    django_urls,
    express,
    fastapi_flask,
    nestjs,
    nextjs,
    openapi,
    rails,
    spring,
)
from app.parsing.extractors.endpoints.dispatch import extract_endpoints
from tests.conftest import MINI_REPO


def _as_set(endpoints):
    return {(e.method, e.route, e.framework, e.handler_symbol) for e in endpoints}


# --- FastAPI/Flask: exhaustive, ground truth from the fixtures' docstrings ---


def test_fastapi_routes_match_ground_truth_exactly():
    content = (MINI_REPO / "src/api/fastapi_routes.py").read_text()
    endpoints = fastapi_flask.extract(content, "src/api/fastapi_routes.py")

    assert _as_set(endpoints) == {
        ("GET", "/api/users/{user_id}", "fastapi", "get_user"),
        ("POST", "/api/users/", "fastapi", "create_user"),
        ("DELETE", "/api/users/{user_id}", "fastapi", "delete_user"),
    }
    delete_ep = next(e for e in endpoints if e.handler_symbol == "delete_user")
    assert delete_ep.auth_hint is not None
    get_ep = next(e for e in endpoints if e.handler_symbol == "get_user")
    assert get_ep.auth_hint is None
    assert all(e.line > 0 for e in endpoints)


def test_flask_routes_match_ground_truth_exactly():
    content = (MINI_REPO / "src/api/flask_routes.py").read_text()
    endpoints = fastapi_flask.extract(content, "src/api/flask_routes.py")

    assert _as_set(endpoints) == {
        ("GET", "/health", "flask", "health"),
        ("GET", "/api/posts/<int:post_id>", "flask", "post_detail"),
        ("DELETE", "/api/posts/<int:post_id>", "flask", "post_detail"),
    }


def test_fastapi_flask_extract_is_100pct_on_mini_repo_fixture():
    fastapi_content = (MINI_REPO / "src/api/fastapi_routes.py").read_text()
    flask_content = (MINI_REPO / "src/api/flask_routes.py").read_text()
    found = len(fastapi_flask.extract(fastapi_content, "src/api/fastapi_routes.py")) + len(
        fastapi_flask.extract(flask_content, "src/api/flask_routes.py")
    )
    ground_truth_count = 6  # 3 FastAPI + 3 Flask, per the fixtures' docstrings
    assert found == ground_truth_count


# --- Express: exhaustive, ground truth from the fixture's docstring ---


def test_express_routes_match_ground_truth_exactly():
    content = (MINI_REPO / "web/expressRoutes.ts").read_text()
    endpoints = express.extract(content, "web/expressRoutes.ts")

    assert _as_set(endpoints) == {
        ("GET", "/health", "express", None),
        ("POST", "/login", "express", None),
        ("GET", "/users/:id", "express", None),
    }
    protected = next(e for e in endpoints if e.route == "/users/:id")
    assert protected.auth_hint == "requireAuth"
    public = next(e for e in endpoints if e.route == "/health")
    assert public.auth_hint is None


def test_express_extract_is_100pct_on_mini_repo_fixture():
    content = (MINI_REPO / "web/expressRoutes.ts").read_text()
    assert len(express.extract(content, "web/expressRoutes.ts")) == 3


def test_express_requires_leading_slash_literal_to_avoid_false_positives():
    # `.get`/`.post`/... on an arbitrary object whose first arg isn't a
    # path-shaped string literal must not be mistaken for a route.
    src = 'const value = cache.get("some_key", defaultValue);\n'
    assert express.extract(src, "unrelated.ts") == []


# --- lighter coverage for the remaining frameworks ---


def test_django_urls():
    src = """
from django.urls import path
from .views import UserListView

urlpatterns = [
    path("users/", UserListView.as_view(), name="user-list"),
    path("users/<int:pk>/", views.user_detail),
]
"""
    endpoints = django_urls.extract(src, "urls.py")
    routes = {(e.route, e.handler_symbol) for e in endpoints}
    # `_view_name` reads the `.as_view()` call's own attribute name, not the
    # class it's called on, for a `SomeView.as_view()` handler expression —
    # a known simplification (the route's real destination is still fully
    # identified by file:line even though "as_view" alone isn't very
    # descriptive as a handler name).
    assert ("/users/", "as_view") in routes
    assert ("/users/<int:pk>/", "user_detail") in routes
    assert all(e.method is None for e in endpoints)


def test_nestjs_controller_routes():
    src = """
@Controller('users')
export class UserController {
  @Get(':id')
  getUser() {}

  @UseGuards(AuthGuard)
  @Post()
  createUser() {}
}
"""
    endpoints = nestjs.extract(src, "user.controller.ts")
    routes = _as_set(endpoints)
    assert ("GET", "/users/:id", "nestjs", "getUser") in routes
    # `@Post()` with no path arg joins as prefix + "/" (empty path ->
    # "/" before joining) — same trailing-slash convention `_join_route`
    # uses in fastapi_flask.py (see the mini_repo fixture's own
    # `POST /api/users/` ground truth).
    assert ("POST", "/users/", "nestjs", "createUser") in routes
    create = next(e for e in endpoints if e.handler_symbol == "createUser")
    assert create.auth_hint == "auth"


def test_nextjs_pages_api_route():
    endpoints = nextjs.extract("export default function handler(req, res) {}", "pages/api/users/[id].ts")
    assert len(endpoints) == 1
    assert endpoints[0].route == "/api/users/[id]"
    assert endpoints[0].framework == "nextjs"


def test_nextjs_app_router_route():
    src = """
export async function GET(request) {
  return Response.json({});
}
export async function POST(request) {
  return Response.json({});
}
"""
    endpoints = nextjs.extract(src, "app/api/users/route.ts")
    methods = {e.method for e in endpoints}
    assert methods == {"GET", "POST"}
    assert all(e.route == "/api/users" for e in endpoints)


def test_spring_controller_routes():
    src = """
@RestController
@RequestMapping("/api/users")
public class UserController {
    @GetMapping("/{id}")
    public User getUser(@PathVariable Long id) {
        return null;
    }

    @PreAuthorize("hasRole('ADMIN')")
    @DeleteMapping("/{id}")
    public void deleteUser(@PathVariable Long id) {}
}
"""
    endpoints = spring.extract(src, "UserController.java")
    routes = _as_set(endpoints)
    assert ("GET", "/api/users/{id}", "spring", "getUser") in routes
    assert ("DELETE", "/api/users/{id}", "spring", "deleteUser") in routes
    delete_ep = next(e for e in endpoints if e.handler_symbol == "deleteUser")
    assert delete_ep.auth_hint == "auth"


def test_rails_verb_and_resources_routes():
    src = """
Rails.application.routes.draw do
  get "health", to: "health#check"
  resources :posts
end
"""
    endpoints = rails.extract(src, "config/routes.rb")
    routes = _as_set(e for e in endpoints if e.framework == "rails")
    assert ("GET", "/health", "rails", "health#check") in routes
    assert ("GET", "/posts", "rails", "posts#index") in routes
    assert ("POST", "/posts", "rails", "posts#create") in routes
    assert ("DELETE", "/posts/:id", "rails", "posts#destroy") in routes


def test_openapi_yaml():
    src = """
openapi: 3.0.0
paths:
  /users/{id}:
    get:
      operationId: getUser
      security:
        - bearerAuth: []
    delete:
      operationId: deleteUser
"""
    endpoints = openapi.extract(src, "openapi.yaml")
    routes = _as_set(endpoints)
    assert ("GET", "/users/{id}", "openapi", "getUser") in routes
    assert ("DELETE", "/users/{id}", "openapi", "deleteUser") in routes
    get_ep = next(e for e in endpoints if e.method == "GET")
    assert get_ep.auth_hint == "auth"
    assert all(e.source == "openapi" for e in endpoints)


def test_openapi_malformed_returns_empty():
    assert openapi.extract("{not valid", "openapi.json") == []
    assert openapi.extract(": not: valid: yaml: :", "openapi.yaml") == []


# --- dispatch.py: routes to the right extractor(s) by language/filename ---


def test_dispatch_python_combines_fastapi_flask_and_django():
    fastapi_content = (MINI_REPO / "src/api/fastapi_routes.py").read_text()
    found = extract_endpoints(fastapi_content, "src/api/fastapi_routes.py", "python")
    assert len(found) == 3


def test_dispatch_openapi_by_filename_regardless_of_language():
    src = "openapi: 3.0.0\npaths:\n  /ping:\n    get:\n      operationId: ping\n"
    found = extract_endpoints(src, "openapi.yaml", None)
    assert len(found) == 1


def test_dispatch_unsupported_language_returns_empty():
    assert extract_endpoints("anything", "notes.md", "markdown") == []
