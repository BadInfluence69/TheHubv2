"""The feature request board."""
from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from ..repo import feedback as feedback_repo
from ..security import admin_required, current_user, login_required

bp = Blueprint("feedback", __name__, url_prefix="/feedback")


@bp.route("/")
@login_required
def board():
    user = current_user()
    status = request.args.get("status", "")
    posts = feedback_repo.listing(user["id"], status)
    for post in posts:
        post["comments"] = feedback_repo.comments_for(post["id"])
    return render_template("feedback.html", posts=posts, status=status)


@bp.post("/new")
@login_required
def create():
    user = current_user()
    title = (request.form.get("title") or "").strip()
    suggestion = (request.form.get("suggestion") or "").strip()

    if not title or not suggestion:
        flash("Both a title and a description, please.", "error")
    else:
        feedback_repo.create(user, title, suggestion)
        flash("Posted.", "success")

    return redirect(url_for("feedback.board"))


@bp.post("/<int:feedback_id>/vote")
@login_required
def vote(feedback_id: int):
    user = current_user()
    voted = feedback_repo.toggle_vote(user["id"], feedback_id)
    post = feedback_repo.get(feedback_id)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"voted": voted, "upvotes": post["upvotes"] if post else 0})
    return redirect(url_for("feedback.board"))


@bp.post("/<int:feedback_id>/comment")
@login_required
def comment(feedback_id: int):
    user = current_user()
    body = (request.form.get("body") or "").strip()
    if not feedback_repo.get(feedback_id):
        abort(404)
    if body:
        feedback_repo.add_comment(feedback_id, user, body)
    return redirect(url_for("feedback.board") + f"#post-{feedback_id}")


@bp.post("/<int:feedback_id>/status")
@admin_required
def set_status(feedback_id: int):
    feedback_repo.set_status(feedback_id, request.form.get("status", "open"))
    return redirect(url_for("feedback.board"))


@bp.post("/<int:feedback_id>/delete")
@admin_required
def delete(feedback_id: int):
    feedback_repo.delete(feedback_id)
    flash("Request removed.", "success")
    return redirect(url_for("feedback.board"))
