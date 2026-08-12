"""Tests for src/users/service.py — should be indexed with is_test=1
(SPEC.md §7.1: "Keep test files ... Mark them is_test=1")."""

from src.users.service import UserService


def test_create_and_get_user():
    service = UserService()
    user = service.create_user("a@example.com", "hunter2")
    assert service.get_user_by_id(user.id) is user


def test_delete_user():
    service = UserService()
    user = service.create_user("b@example.com", "hunter2")
    assert service.delete_user(user.id) is True
    assert service.get_user_by_id(user.id) is None
