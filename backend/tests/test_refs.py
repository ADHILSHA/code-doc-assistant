from __future__ import annotations

from app.parsing.refs import extract_calls, extract_imports, resolve_import


def test_extract_calls_python():
    src = """
def handler():
    user = get_user(1)
    return user.save()
"""
    calls = extract_calls(src, "python")
    names = [c.target_name for c in calls]
    assert "get_user" in names
    assert "save" in names  # `user.save()` -> attribute call, name is the last segment


def test_extract_calls_typescript():
    src = """
function run() {
  const x = doThing();
  helper.process(x);
}
"""
    calls = extract_calls(src, "typescript")
    names = [c.target_name for c in calls]
    assert "doThing" in names
    assert "process" in names


def test_extract_calls_unsupported_language_or_broken_source():
    assert extract_calls("anything", None) == []
    assert extract_calls("anything", "yaml") == []
    assert extract_calls("def f(:\n!!!broken!!!", "python") == []


def test_extract_imports_python():
    src = "from src.auth import verify_password\nimport os\n"
    refs = extract_imports(src, "python")
    modules = [r.module_text for r in refs]
    assert "src.auth" in modules
    assert "os" in modules


def test_extract_imports_typescript():
    src = 'import { requireAuth } from "./authMiddleware";\nconst x = require("express");\n'
    refs = extract_imports(src, "typescript")
    modules = [r.module_text for r in refs]
    assert "./authMiddleware" in modules
    assert "express" in modules


def test_resolve_import_python_internal_module():
    known = {"src/auth/auth.py", "src/users/service.py"}
    resolved, is_external = resolve_import(
        "src.auth.auth", from_path="src/users/service.py", language="python", known_paths=known
    )
    assert resolved == "src/auth/auth.py"
    assert is_external is False


def test_resolve_import_python_package_init():
    known = {"src/auth/__init__.py"}
    resolved, is_external = resolve_import(
        "src.auth", from_path="src/users/service.py", language="python", known_paths=known
    )
    assert resolved == "src/auth/__init__.py"
    assert is_external is False


def test_resolve_import_python_stdlib_is_external():
    resolved, is_external = resolve_import(
        "os", from_path="src/users/service.py", language="python", known_paths=set()
    )
    assert resolved is None
    assert is_external is True


def test_resolve_import_typescript_relative():
    known = {"web/authMiddleware.ts"}
    resolved, is_external = resolve_import(
        "./authMiddleware", from_path="web/expressRoutes.ts", language="typescript", known_paths=known
    )
    assert resolved == "web/authMiddleware.ts"
    assert is_external is False


def test_resolve_import_typescript_parent_relative_normalizes_dotdot():
    known = {"web/authMiddleware.ts"}
    resolved, is_external = resolve_import(
        "../authMiddleware", from_path="web/nested/routes.ts", language="typescript", known_paths=known
    )
    assert resolved == "web/authMiddleware.ts"
    assert is_external is False


def test_resolve_import_typescript_bare_specifier_is_external():
    resolved, is_external = resolve_import(
        "express", from_path="web/expressRoutes.ts", language="typescript", known_paths=set()
    )
    assert resolved is None
    assert is_external is True


def test_resolve_import_unresolvable_language_always_external():
    resolved, is_external = resolve_import(
        "fmt", from_path="main.go", language="go", known_paths={"fmt.go"}
    )
    assert resolved is None
    assert is_external is True
