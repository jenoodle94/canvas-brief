"""
Canvas Brief — Web App
A tool for Stanford students to generate and share AI study briefs
from their Canvas course modules.
"""

import gc
import hashlib
import logging
import os
import re

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_from_directory, jsonify,
)
import anthropic
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


@app.errorhandler(500)
def handle_500(e):
    import traceback
    tb = traceback.format_exc()
    app.logger.error(f"Unhandled 500: {e}\n{tb}")
    if request.path.startswith(("/generate", "/api/")):
        return jsonify({"error": str(e), "traceback": tb}), 500
    return f"<h1>500</h1><pre>{tb}</pre>", 500


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
        gc.collect()
        if not content:
            return jsonify({"error": "No readable content in this module yet"}), 404

        # Generate brief
        app.logger.info(f"Generate: calling Claude for {module_name}")
        brief_text = brief_generator.generate_brief_text(course_name, module_name, content)
        del content
        gc.collect()

        # Save PDF
        app.logger.info(f"Generate: creating PDF for {module_name}")
        filename = f"{safe_filename(course_name)}__{safe_filename(module_name)}.pdf"
        filepath = os.path.join(BRIEFS_DIR, filename)
        brief_generator.create_pdf(brief_text, filepath)
        del brief_text
        gc.collect()

        # Save to database
        database.save_brief(course_id, course_name, module_id, module_name, filename)
        database.log_event("generate", course_name, module_name, ip_hash=ip_hash())

        brief = database.get_brief(course_id, module_id)
        return jsonify({"status": "created", "brief_id": brief["id"]})

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        app.logger.error(f"Generate failed: {tb}")
        return jsonify({"error": str(e), "traceback": tb}), 500


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


@app.route("/weekly")
def weekly():
    """Weekly assignment grid view."""
    token = session.get("canvas_token")
    if not token:
        flash("Please connect your Canvas account first.", "error")
        return redirect(url_for("index"))
    return render_template("weekly.html")


@app.route("/api/weekly-data")
def api_weekly_data():
    """Fetch all courses and their assignments for the full-quarter weekly view."""
    token = session.get("canvas_token")
    if not token:
        return jsonify({"error": "Not connected"}), 401

    try:
        courses = canvas_api.get_courses(token)
        colors = canvas_api.get_course_colors(token)

        all_assignments = []
        course_list = []
        for course in courses:
            cid = course["id"]
            course_list.append({
                "id": cid,
                "name": course["name"],
                "color": colors.get(cid, "#8C1515"),
            })
            assignments = canvas_api.get_assignments(token, cid)
            for a in assignments:
                a["course_name"] = course["name"]
                a["course_color"] = colors.get(cid, "#8C1515")
            all_assignments.extend(assignments)

        return jsonify({
            "courses": course_list,
            "assignments": all_assignments,
        })
    except Exception as e:
        app.logger.error(f"Weekly data error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/assignment-summary", methods=["POST"])
def api_assignment_summary():
    """Generate a short AI summary for an assignment description."""
    token = session.get("canvas_token")
    if not token:
        return jsonify({"error": "Not connected"}), 401

    data = request.get_json()
    assignment_id = data.get("assignment_id")
    description = data.get("description", "")
    name = data.get("name", "")

    if not description.strip():
        return jsonify({"summary": name})

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": (
                f"Summarize this assignment in 2-3 sentences. "
                f"Be action-oriented and use plain language. Describe what the student needs to do. "
                f"Assignment: \"{name}\"\n\nDescription: {description[:2000]}"
            )}],
        )
        return jsonify({"summary": message.content[0].text})
    except Exception as e:
        app.logger.error(f"Summary generation error: {e}")
        return jsonify({"summary": name})


@app.route("/api/announcements", methods=["POST"])
def api_announcements():
    """Generate AI-powered pre-task announcements from assignment descriptions."""
    token = session.get("canvas_token")
    if not token:
        return jsonify({"error": "Not connected"}), 401

    data = request.get_json()
    assignments = data.get("assignments", [])

    # Filter to assignments that mention pre-task keywords
    keywords = ["team", "partner", "sign up", "peer review", "draft due",
                 "proposal", "group", "register", "form a", "pre-work",
                 "before class", "preparation", "prerequisite"]

    candidates = []
    for a in assignments:
        desc = (a.get("description") or "").lower()
        name = (a.get("name") or "").lower()
        text = desc + " " + name
        if any(kw in text for kw in keywords):
            candidates.append(a)

    if not candidates:
        return jsonify({"announcements": []})

    # Sort by due date (soonest first) and limit to 5
    candidates.sort(key=lambda x: x.get("due_at", ""))
    candidates = candidates[:5]

    try:
        descs = "\n\n".join(
            f"- \"{a['name']}\" (due {a['due_at']}, course: {a.get('course_name', 'Unknown')}): "
            f"{(a.get('description') or '')[:500]}"
            for a in candidates
        )
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": (
                f"From these upcoming assignments, extract 3-5 time-sensitive action items "
                f"that students need to do BEFORE the due date (like forming teams, signing up, "
                f"submitting drafts, doing peer reviews, etc). For each, write a single-line "
                f"reminder starting with a pin emoji. Include the course name and key date. "
                f"If none have pre-task actions, return an empty list.\n\n{descs}\n\n"
                f"Return ONLY the reminder lines, one per line. No extra text."
            )}],
        )
        lines = [l.strip() for l in message.content[0].text.strip().split("\n") if l.strip()]
        announcements = []
        for i, line in enumerate(lines[:5]):
            # Try to match to the original assignment for linking
            linked_url = ""
            for a in candidates:
                if a["name"].lower()[:20] in line.lower() or a.get("course_name", "").lower() in line.lower():
                    linked_url = a.get("html_url", "")
                    break
            announcements.append({"text": line, "url": linked_url})
        return jsonify({"announcements": announcements})
    except Exception as e:
        app.logger.error(f"Announcements error: {e}")
        return jsonify({"announcements": []})


@app.route("/metrics")
def metrics():
    """Analytics dashboard for case study."""
    data = database.get_metrics()
    return render_template("metrics.html", metrics=data)


@app.route("/api/metrics")
def api_metrics():
    """JSON endpoint for metrics."""
    return jsonify(database.get_metrics())


@app.route("/health")
def health():
    """Diagnostic endpoint."""
    import anthropic
    checks = {}

    # Check data dir
    checks["data_dir"] = DATA_DIR
    checks["data_dir_exists"] = os.path.exists(DATA_DIR)
    checks["briefs_dir"] = BRIEFS_DIR
    checks["briefs_dir_exists"] = os.path.exists(BRIEFS_DIR)
    checks["briefs_dir_writable"] = os.access(BRIEFS_DIR, os.W_OK)

    # Check DB
    try:
        briefs = database.get_all_briefs()
        checks["db_ok"] = True
        checks["db_brief_count"] = len(briefs)
    except Exception as e:
        checks["db_ok"] = False
        checks["db_error"] = str(e)

    # Check Anthropic key
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    checks["anthropic_key_set"] = bool(api_key)
    checks["anthropic_key_prefix"] = api_key[:12] + "..." if api_key else "(not set)"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        client.models.list()
        checks["anthropic_api_ok"] = True
    except Exception as e:
        checks["anthropic_api_ok"] = False
        checks["anthropic_error"] = str(e)

    return jsonify(checks)


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
