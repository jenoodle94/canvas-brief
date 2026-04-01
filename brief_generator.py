"""Brief generation (Claude API) and PDF creation."""

import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")


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
    """Convert markdown to simple HTML for fpdf2."""
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

        if stripped.startswith("### "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[4:]
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            html_parts.append(f"<br><b><font size='12'>{text}</font></b><br>")
        elif stripped.startswith("## "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[3:]
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            html_parts.append(f"<br><b><font size='14'>{text}</font></b><br>")
        elif stripped.startswith("# "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[2:]
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
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            html_parts.append(f"<li>{text}</li>")
        elif stripped.startswith(("  - ", "    - ", "  * ", "    * ")):
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
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", stripped)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            html_parts.append(f"{text}<br>")

    if in_list:
        html_parts.append("</ul>")

    return "\n".join(html_parts)


def generate_brief_text(course_name, session_name, session_text):
    """Send module content to Claude and return markdown brief."""
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


def create_pdf(markdown_text, output_path):
    """Convert markdown brief to a printable PDF."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_left_margin(15)
    pdf.set_right_margin(15)
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)

    html = markdown_to_simple_html(sanitize_text(markdown_text))
    pdf.write_html(html)
    pdf.output(output_path)
