"""
Canvas Brief — Web App
A tool for Stanford students to generate and share AI study briefs
from their Canvas course modules.
"""

import hashlib
import logging
import os
import re

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_from_directory, jsonify,
)
from dotenv import load_dotenv

load_dotenv()

import canvas_api
import brief_generator
import database

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32).hex())

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
BRIEFS_DIR = os.path.join(DATA_DIR, "briefs")
os.makedirs(BRIEFS_DIR, exist_ok=True)


def ip_hash():
    """Hash the user's IP for anonymous analytics."""
    ip = request.remote_addr or "unknown"
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def safe_filename(text):
    return re.sub(r"[^\w\-]", "_", text).strip("_")


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.route("/")
def index():
    """Landing page — connect with Canvas token or browse library."""
    return render_template("index.html")


@app.route("/connect", methods=["POST"])
def connect():
    """Validate Canvas token and store in session."""
    token = request.form.get("token", "").strip()
    if not token:
        flash("Please enter your Canvas API token.", "error")
        return redirect(url_for("index"))

    if not canvas_api.validate_token(token):
        flash("Invalid Canvas token. Please check and try again.", "error")
        return redirect(url_for("index"))

    session["canvas_token"] = token
    database.log_event("login", ip_hash=ip_hash())
    flash("Connected to Canvas!", "success")
    return redirect(url_for("dashboard"))


@app.route("/disconnect")
def disconnect():
    session.pop("canvas_token", None)
    flash("Disconnected from Canvas.", "info")
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    """Show user's courses and sessions."""
    token = session.get("canvas_token")
    if not token:
        flash("Please connect your Canvas account first.", "error")
        return redirect(url_for("index"))

    try:
        courses = canvas_api.get_courses(token)
    except Exception as e:
        flash(f"Error fetching courses: {e}", "error")
        return redirect(url_for("index"))

    # For each course, get modules and check which have briefs
    course_data = []
    for course in courses:
        modules = canvas_api.get_modules(token, course["id"])
        sessions = []
        for mod in modules:
            existing = database.get_brief(course["id"], mod["id"])
            sessions.append({
                "id": mod["id"],
                "name": mod["name"],
                "has_brief": existing is not None,
                "brief_id": existing["id"] if existing else None,
            })
        course_data.append({
            "id": course["id"],
            "name": course["name"],
            "sessions": sessions,
        })

    return render_template("dashboard.html", courses=course_data)


@app.route("/generate/<int:course_id>/<int:module_id>", methods=["POST"])
def generate(course_id, module_id):
    """Generate a brief for a specific session."""
    token = session.get("canvas_token")
    if not token:
        return jsonify({"error": "Not connected"}), 401

    # Check if already exists
    existing = database.get_brief(course_id, module_id)
    if existing:
        return jsonify({"status": "exists", "brief_id": existing["id"]})

    try:
        # Get course name
        app.logger.info(f"Generate: fetching courses for course_id={course_id}")
        courses = canvas_api.get_courses(token)
        course = next((c for c in courses if c["id"] == course_id), None)
        if not course:
            return jsonify({"error": "Course not found"}), 404
        course_name = course["name"]

        # Get module
        app.logger.info(f"Generate: fetching modules for {course_name}")
        modules = canvas_api.get_modules(token, course_id)
        module = next((m for m in modules if m["id"] == module_id), None)
        if not module:
            return jsonify({"error": "Module not found"}), 404
        module_name = module["name"]

        # Fetch content
        app.logger.info(f"Generate: fetching content for {module_name}")
        content = canvas_api.fetch_module_content(token, course_id, module)
        if not content:
            return jsonify({"error": "No readable content in this module yet"}), 404

        # Generate brief
        app.logger.info(f"Generate: calling Claude for {module_name}")
        brief_text = brief_generator.generate_brief_text(course_name, module_name, content)

        # Save PDF
        app.logger.info(f"Generate: creating PDF for {module_name}")
        filename = f"{safe_filename(course_name)}__{safe_filename(module_name)}.pdf"
        filepath = os.path.join(BRIEFS_DIR, filename)
        brief_generator.create_pdf(brief_text, filepath)

        # Save to database
        database.save_brief(course_id, course_name, module_id, module_name, filename)
        database.log_event("generate", course_name, module_name, ip_hash=ip_hash())

        brief = database.get_brief(course_id, module_id)
        return jsonify({"status": "created", "brief_id": brief["id"]})

    except Exception as e:
        app.logger.exception(f"Generate failed for course={course_id} module={module_id}")
        return jsonify({"error": str(e)}), 500


@app.route("/library")
def library():
    """Public library of all generated briefs."""
    briefs = database.get_all_briefs()

    # Group by course
    courses = {}
    for b in briefs:
        cname = b["course_name"]
        if cname not in courses:
            courses[cname] = []
        courses[cname].append(dict(b))

    return render_template("library.html", courses=courses)


@app.route("/download/<int:brief_id>")
def download(brief_id):
    """Download a brief PDF."""
    brief = database.get_brief_by_id(brief_id)
    if not brief:
        flash("Brief not found.", "error")
        return redirect(url_for("library"))

    database.log_event("download", brief["course_name"], brief["module_name"], ip_hash=ip_hash())
    return send_from_directory(BRIEFS_DIR, brief["filename"], as_attachment=True)


@app.route("/view/<int:brief_id>")
def view(brief_id):
    """View a brief PDF in browser."""
    brief = database.get_brief_by_id(brief_id)
    if not brief:
        flash("Brief not found.", "error")
        return redirect(url_for("library"))

    database.log_event("download", brief["course_name"], brief["module_name"], ip_hash=ip_hash())
    return send_from_directory(BRIEFS_DIR, brief["filename"], as_attachment=False)


@app.route("/metrics")
def metrics():
    """Analytics dashboard for case study."""
    data = database.get_metrics()
    return render_template("metrics.html", metrics=data)


@app.route("/api/metrics")
def api_metrics():
    """JSON endpoint for metrics."""
    return jsonify(database.get_metrics())


# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------

database.init_db()

# Configure logging for Render / Gunicorn
if not app.debug:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
