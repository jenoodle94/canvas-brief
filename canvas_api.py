"""Canvas API helpers — extracted for use by both CLI and web app."""

import io
import re

import requests
from pypdf import PdfReader

CANVAS_BASE = "https://canvas.stanford.edu"

IGNORED_COURSES = {
    "24F-MBA-Program",
    "Career Management - MBA'26",
    "OB-374 / 375 Prequalification",
}

SKIP_MODULES = {
    "Canvas Help for Students",
    "Course Overview",
    "Tax News!",
}


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def strip_html(html):
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def validate_token(token):
    """Check if a Canvas token is valid. Returns True/False."""
    resp = requests.get(
        f"{CANVAS_BASE}/api/v1/users/self",
        headers=_headers(token),
        timeout=10,
    )
    return resp.ok


def get_courses(token):
    resp = requests.get(
        f"{CANVAS_BASE}/api/v1/courses",
        headers=_headers(token),
        params={"enrollment_state": "active", "per_page": 50},
        timeout=15,
    )
    resp.raise_for_status()
    return [c for c in resp.json()
            if c.get("name") and c["name"] not in IGNORED_COURSES]


def get_modules(token, course_id):
    resp = requests.get(
        f"{CANVAS_BASE}/api/v1/courses/{course_id}/modules",
        headers=_headers(token),
        params={"per_page": 50},
        timeout=15,
    )
    if resp.status_code == 400:
        return []
    resp.raise_for_status()
    return [m for m in resp.json() if m.get("name") not in SKIP_MODULES]


def get_module_items(token, course_id, module_id):
    resp = requests.get(
        f"{CANVAS_BASE}/api/v1/courses/{course_id}/modules/{module_id}/items",
        headers=_headers(token),
        params={"per_page": 50},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def download_pdf_text(token, file_url):
    resp = requests.get(file_url, headers=_headers(token), allow_redirects=True, timeout=60)
    resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))
    pages = [p.extract_text() for p in reader.pages if p.extract_text()]
    return "\n\n".join(pages)


def get_file_content(token, content_id):
    resp = requests.get(
        f"{CANVAS_BASE}/api/v1/files/{content_id}",
        headers=_headers(token),
        timeout=15,
    )
    if not resp.ok:
        return None

    info = resp.json()
    filename = info.get("display_name", "")
    download_url = info.get("url")
    if not download_url:
        return None

    if filename.lower().endswith(".pdf"):
        try:
            text = download_pdf_text(token, download_url)
            if text.strip():
                return {"title": filename, "body": text}
            return {"title": filename,
                    "body": "[PDF downloaded but no extractable text]"}
        except Exception as e:
            return {"title": filename, "body": f"[Could not read PDF: {e}]"}

    return {"title": filename, "body": f"[File: {filename} — not a PDF, skipped]"}


def get_item_content(token, item):
    item_type = item.get("type", "")
    title = item.get("title", "Untitled")

    if item_type == "Page" and item.get("url"):
        resp = requests.get(item["url"], headers=_headers(token), timeout=15)
        if resp.ok:
            body = strip_html(resp.json().get("body", "") or "")
            if body:
                return {"title": title, "body": body}

    if item_type == "File":
        content_id = item.get("content_id")
        if content_id:
            result = get_file_content(token, content_id)
            if result:
                return result
        return {"title": title, "body": f"[File attachment: {title}]"}

    if item_type == "ExternalUrl":
        return {"title": title, "body": f"[External link: {item.get('external_url', '')}]"}

    if item_type == "SubHeader":
        return {"title": title, "body": ""}

    return None


def fetch_module_content(token, course_id, module):
    """Fetch all readable content from a module. Returns text blob."""
    items = get_module_items(token, course_id, module["id"])
    contents = [get_item_content(token, item) for item in items]
    contents = [c for c in contents if c and c.get("body")]

    if not contents:
        return None

    lines = [f"## {module.get('name', 'Untitled')}\n"]
    for c in contents:
        lines.append(f"### {c['title']}")
        body = c["body"]
        if len(body) > 5000:
            body = body[:5000] + "\n... [truncated]"
        lines.append(body + "\n")

    return "\n".join(lines)
