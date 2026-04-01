"""
Canvas Brief Generator
Fetches session modules from Stanford Canvas Spring 2026 courses,
generates a study brief per session via Claude, and saves as PDF.
Re-running only generates briefs for new/unseen sessions.
"""

import io
import json
import os
import re
import sys
from datetime import datetime

import anthropic
import requests
from dotenv import load_dotenv
from pypdf import PdfReader

# Force unbuffered output so we see progress in real time
sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

CANVAS_TOKEN = os.getenv("CANVAS_API_TOKEN")
CANVAS_BASE = os.getenv("CANVAS_BASE_URL", "https://canvas.stanford.edu")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

CANVAS_HEADERS = {"Authorization": f"Bearer {CANVAS_TOKEN}"}

BRIEFS_DIR = "briefs"
TRACKER_FILE = os.path.join(BRIEFS_DIR, ".generated.json")

# Courses to skip entirely
IGNORED_COURSES = {
    "24F-MBA-Program",
    "Career Management - MBA'26",
    "OB-374 / 375 Prequalification",
}

# Module names to skip (not real sessions)
SKIP_MODULES = {
    "Canvas Help for Students",
    "Course Overview",
    "Tax News!",
}


# ---------------------------------------------------------------------------
# Tracker — remembers which session briefs have already been generated
# ---------------------------------------------------------------------------


def load_tracker():
    """Return a set of keys like 'CourseID:ModuleID' for already-generated briefs."""
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE) as f:
            return set(json.load(f))
    return set()


def save_tracker(tracker):
    with open(TRACKER_FILE, "w") as f:
        json.dump(sorted(tracker), f, indent=2)


# ---------------------------------------------------------------------------
# HTML / text helpers
# ---------------------------------------------------------------------------


def strip_html(html):
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(text):
    """Turn arbitrary text into a filesystem-safe name."""
    return re.sub(r"[^\w\-]", "_", text).strip("_")


# ---------------------------------------------------------------------------
# Canvas API helpers
# ---------------------------------------------------------------------------


def get_courses():
    url = f"{CANVAS_BASE}/api/v1/courses"
    params = {"enrollment_state": "active", "per_page": 50}
    resp = requests.get(url, headers=CANVAS_HEADERS, params=params)
    resp.raise_for_status()
    return [c for c in resp.json()
            if c.get("name") and c["name"] not in IGNORED_COURSES]


def get_modules(course_id):
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/modules"
    resp = requests.get(url, headers=CANVAS_HEADERS, params={"per_page": 50})
    if resp.status_code == 400:
        return []
    resp.raise_for_status()
    return resp.json()


def get_module_items(course_id, module_id):
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/modules/{module_id}/items"
    resp = requests.get(url, headers=CANVAS_HEADERS, params={"per_page": 50})
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Content extraction (pages, PDFs, files)
# ---------------------------------------------------------------------------


def download_pdf_text(file_url):
    """Download a PDF from Canvas and extract its text."""
    resp = requests.get(file_url, headers=CANVAS_HEADERS, allow_redirects=True)
    resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))
    pages = [p.extract_text() for p in reader.pages if p.extract_text()]
    return "\n\n".join(pages)


def get_file_content(content_id):
    """Fetch file metadata and download PDFs."""
    url = f"{CANVAS_BASE}/api/v1/files/{content_id}"
    resp = requests.get(url, headers=CANVAS_HEADERS)
    if not resp.ok:
        return None

    info = resp.json()
    filename = info.get("display_name", "")
    download_url = info.get("url")
    if not download_url:
        return None

    if filename.lower().endswith(".pdf"):
        try:
            text = download_pdf_text(download_url)
            if text.strip():
                return {"title": filename, "body": text}
            return {"title": filename,
                    "body": "[PDF downloaded but no extractable text — may be scanned images]"}
        except Exception as e:
            return {"title": filename, "body": f"[Could not read PDF: {e}]"}

    return {"title": filename, "body": f"[File: {filename} — not a PDF, skipped]"}


def get_item_content(item):
    """Extract content from a single module item."""
    item_type = item.get("type", "")
    title = item.get("title", "Untitled")

    if item_type == "Page" and item.get("url"):
        resp = requests.get(item["url"], headers=CANVAS_HEADERS)
        if resp.ok:
            body = strip_html(resp.json().get("body", "") or "")
            if body:
                return {"title": title, "body": body}

    if item_type == "File":
        content_id = item.get("content_id")
        if content_id:
            result = get_file_content(content_id)
            if result:
                return result
        return {"title": title, "body": f"[File attachment: {title}]"}

    if item_type == "ExternalUrl":
        return {"title": title, "body": f"[External link: {item.get('external_url', '')}]"}

    if item_type == "SubHeader":
        return {"title": title, "body": ""}

    return None


# ---------------------------------------------------------------------------
# Build text for a single session module
# ---------------------------------------------------------------------------


def build_session_text(module, items_content):
    lines = [f"## {module.get('name', 'Untitled')}\n"]

    if not items_content:
        lines.append("(No readable content found.)\n")
        return "\n".join(lines)

    for item in items_content:
        if item["body"]:
            lines.append(f"### {item['title']}")
            body = item["body"]
            if len(body) > 5000:
                body = body[:5000] + "\n... [truncated]"
            lines.append(body + "\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude — generate brief
# ---------------------------------------------------------------------------


def generate_brief(course_name, session_name, session_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""Here is content from "{session_name}" in my Stanford course "{course_name}":

{session_text}

Please generate a study brief for this session that:
1. Summarizes the case (who, what, context, key tension/decision)
2. If there are multiple readings, summarize each one separately
3. Answers any study questions or discussion questions shown in the content
4. Highlights key concepts, frameworks, or takeaways
5. Notes any files or links I should review that you couldn't access

Keep it concise but thorough. Format with clear headers and bullet points.
This will be printed as a 1-2 page PDF for class, so make it scannable."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Markdown → PDF conversion
# ---------------------------------------------------------------------------


def sanitize_text(text):
    """Replace Unicode characters that latin-1 PDF fonts can't render."""
    replacements = {
        "\u2014": "--", "\u2013": "-", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2022": "-",
        "\u25e6": "-", "\u00a0": " ", "\u2192": "->", "\u2190": "<-",
        "\u2264": "<=", "\u2265": ">=", "\u2260": "!=", "\u00b7": "-",
        "\u2023": ">", "\u25aa": "-", "\u25ab": "-",
    }
    for char, repl in replacements.items():
        text = text.replace(char, repl)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def markdown_to_simple_html(md_text):
    """Convert markdown to simple HTML that fpdf2's write_html can handle."""
    lines = md_text.split("\n")
    html_parts = []
    in_list = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append("<br>")
            continue

        # Headers
        if stripped.startswith("### "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[4:].replace("**", "<b>", 1).replace("**", "</b>", 1)
            html_parts.append(f"<br><b><font size='12'>{text}</font></b><br>")
        elif stripped.startswith("## "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[3:].replace("**", "<b>", 1).replace("**", "</b>", 1)
            html_parts.append(f"<br><b><font size='14'>{text}</font></b><br>")
        elif stripped.startswith("# "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[2:].replace("**", "<b>", 1).replace("**", "</b>", 1)
            html_parts.append(f"<b><font size='16'>{text}</font></b><br>")
        elif stripped.startswith("---"):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append("<br>")
        elif stripped.startswith(("- ", "* ")):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            text = stripped[2:]
            # Convert **bold** to <b>bold</b>
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            html_parts.append(f"<li>{text}</li>")
        elif stripped.startswith(("  - ", "    - ", "  * ", "    * ")):
            # Nested list items — just render as indented list items
            text = stripped.lstrip(" -*").strip()
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>&nbsp;&nbsp;{text}</li>")
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            # Convert inline markdown
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", stripped)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            html_parts.append(f"{text}<br>")

    if in_list:
        html_parts.append("</ul>")

    return "\n".join(html_parts)


def markdown_to_pdf(markdown_text, output_path):
    """Convert markdown text to a clean printable PDF."""
    from fpdf import FPDF
    from fpdf.html import HTMLMixin

    class MyPDF(FPDF, HTMLMixin):
        pass

    pdf = MyPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_left_margin(15)
    pdf.set_right_margin(15)
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)

    html = markdown_to_simple_html(sanitize_text(markdown_text))
    pdf.write_html(html)
    pdf.output(output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not CANVAS_TOKEN:
        print("Error: Set CANVAS_API_TOKEN in your .env file.")
        sys.exit(1)
    if not ANTHROPIC_KEY:
        print("Error: Set ANTHROPIC_API_KEY in your .env file.")
        sys.exit(1)

    os.makedirs(BRIEFS_DIR, exist_ok=True)
    tracker = load_tracker()

    print("Fetching Spring 2026 courses from Canvas...")
    courses = get_courses()

    if not courses:
        print("No matching courses found.")
        return

    print(f"Found {len(courses)} course(s):\n")
    for c in courses:
        print(f"  • {c['name']}")
    print()

    new_count = 0

    for course in courses:
        course_name = course["name"]
        cid = course["id"]
        print(f"── {course_name} ──")

        modules = get_modules(cid)
        if not modules:
            print("   No modules found.\n")
            continue

        # Filter to session modules only
        session_modules = [m for m in modules if m.get("name") not in SKIP_MODULES]
        if not session_modules:
            print("   No session modules found.\n")
            continue

        print(f"   {len(session_modules)} session(s)")

        for mod in session_modules:
            mod_name = mod["name"]
            tracker_key = f"{cid}:{mod['id']}"

            if tracker_key in tracker:
                print(f"   ✓ {mod_name} (already generated)")
                continue

            # Fetch module content
            print(f"   → {mod_name}")
            items = get_module_items(cid, mod["id"])
            items_content = [get_item_content(item) for item in items]
            items_content = [ic for ic in items_content if ic]

            if not any(ic["body"] for ic in items_content):
                print(f"     (no readable content yet — skipping)")
                continue

            session_text = build_session_text(mod, items_content)

            # Generate brief
            print(f"     Generating brief with Claude...")
            brief = generate_brief(course_name, mod_name, session_text)

            # Save as PDF
            course_short = safe_filename(course_name)
            session_short = safe_filename(mod_name)
            filename = f"{course_short}__{session_short}.pdf"
            filepath = os.path.join(BRIEFS_DIR, filename)

            markdown_to_pdf(brief, filepath)
            print(f"     Saved → {filepath}")

            # Mark as done
            tracker.add(tracker_key)
            save_tracker(tracker)
            new_count += 1

        print()

    if new_count == 0:
        print("No new sessions to generate briefs for. All up to date!")
    else:
        print(f"Done! Generated {new_count} new brief(s) in {BRIEFS_DIR}/")


if __name__ == "__main__":
    main()
