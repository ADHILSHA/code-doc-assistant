"""User service — the known Python API used by retrieval tests.

Retrieval should rank `get_user_by_id` in the top 3 for the exact-identifier
query "get_user_by_id" (SPEC.md Phase 1 acceptance criteria).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.auth.auth import hash_password


@dataclass
class User:
    id: int
    email: str
    password_hash: str


class UserService:
    """In-memory user store, used only by tests and examples."""

    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._next_id = 1

    def create_user(self, email: str, password: str) -> User:
        """Create a new user, hashing the password before storage."""
        user = User(id=self._next_id, email=email, password_hash=hash_password(password))
        self._users[user.id] = user
        self._next_id += 1
        return user

    def get_user_by_id(self, user_id: int) -> User | None:
        """Look up a user by primary key, or None if it doesn't exist."""
        return self._users.get(user_id)

    def delete_user(self, user_id: int) -> bool:
        """Remove a user. Returns True if a user was actually deleted."""
        return self._users.pop(user_id, None) is not None

    def list_users(self) -> list[User]:
        return list(self._users.values())
