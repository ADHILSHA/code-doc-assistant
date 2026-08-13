"""Flask routes — known ground truth for endpoint extraction tests.

Ground truth:
  GET    /health                     -> health          (plain @app.route)
  GET    /api/posts/<int:post_id>    -> post_detail      (blueprint, url_prefix merged)
  DELETE /api/posts/<int:post_id>    -> post_detail      (same handler, second method)
"""

from flask import Blueprint, Flask

app = Flask(__name__)
bp = Blueprint("posts", __name__, url_prefix="/api/posts")


@app.route("/health", methods=["GET"])
def health():
    return "ok"


@bp.route("/<int:post_id>", methods=["GET", "DELETE"])
def post_detail(post_id):
    """Get or delete a post."""
    return str(post_id)
