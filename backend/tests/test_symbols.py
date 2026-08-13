from __future__ import annotations

from app.parsing.symbols import extract_symbols


def test_python_class_and_methods():
    src = '''
class UserService:
    """Manages users."""

    def get_user(self, user_id: int):
        """Fetch a user."""
        return user_id

    def _internal(self):
        pass
'''
    symbols = extract_symbols(src, "python")
    by_name = {s.name: s for s in symbols}

    assert by_name["UserService"].kind == "class"
    assert by_name["UserService"].docstring == "Manages users."
    assert by_name["UserService"].is_exported is True

    assert by_name["get_user"].parent_symbol == "UserService"
    assert by_name["get_user"].docstring == "Fetch a user."
    assert by_name["get_user"].is_exported is True

    # Leading underscore => not exported, by Python convention.
    assert by_name["_internal"].is_exported is False


def test_python_decorated_class_signature_is_not_the_decorator():
    src = """
@dataclass
class User:
    name: str
"""
    symbols = extract_symbols(src, "python")
    (user,) = [s for s in symbols if s.name == "User"]
    # Regression: signature used to come from the decorator-inclusive span,
    # showing "@dataclass" instead of the actual declaration.
    assert user.signature == "class User:"
    assert user.start_line == 2  # span (start_line) still includes the decorator line
    assert user.end_line == 4


def test_typescript_export_wrapped_class_and_methods():
    src = """
export class UserClient {
  fetchUser(id: string) {
    return id;
  }
}

class InternalHelper {
  helperMethod() {}
}
"""
    symbols = extract_symbols(src, "typescript")
    by_name = {s.name: s for s in symbols}

    assert by_name["UserClient"].is_exported is True
    # Regression: a method of an exported class isn't itself export-wrapped
    # syntactically — must still be reported as exported (inherited from
    # the enclosing container).
    assert by_name["fetchUser"].is_exported is True
    assert by_name["fetchUser"].parent_symbol == "UserClient"

    assert by_name["InternalHelper"].is_exported is False
    assert by_name["helperMethod"].is_exported is False


def test_go_exported_by_capitalization():
    src = """
package main

func PublicFunc() {}

func privateFunc() {}
"""
    symbols = extract_symbols(src, "go")
    by_name = {s.name: s for s in symbols}
    assert by_name["PublicFunc"].is_exported is True
    assert by_name["privateFunc"].is_exported is False


def test_unsupported_language_returns_empty():
    assert extract_symbols("anything", None) == []
    assert extract_symbols("anything", "yaml") == []


def test_unparseable_source_returns_empty():
    # Deliberately broken Python — tree-sitter's error node should make
    # extract_symbols bail out with [] rather than return garbage.
    assert extract_symbols("def f(:\n    !!!broken!!!", "python") == []
