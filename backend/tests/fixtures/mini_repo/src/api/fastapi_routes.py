"""FastAPI routes — known ground truth for endpoint extraction tests
(SPEC.md Phase 2 acceptance criterion: >=90% of real endpoints extracted
with correct method, path, and handler location).

Ground truth (router prefix "/api/users" merged with each route's own path):
  GET    /api/users/{user_id}  -> get_user
  POST   /api/users/           -> create_user
  DELETE /api/users/{user_id}  -> delete_user   (has an auth dependency)
"""

from fastapi import APIRouter, Depends

from src.auth.auth import verify_password

router = APIRouter(prefix="/api/users")


@router.get("/{user_id}")
def get_user(user_id: int):
    """Fetch a user by id."""
    return {"id": user_id}


@router.post("/")
def create_user(email: str, password: str):
    """Create a new user."""
    return {"email": email}


@router.delete("/{user_id}", dependencies=[Depends(verify_password)])
def delete_user(user_id: int):
    """Delete a user. Requires authentication."""
    return {"deleted": user_id}
