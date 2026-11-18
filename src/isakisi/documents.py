import os
from io import BytesIO
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from docx import Document
from docx.shared import Pt
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate

from .settings import Problem

FONT_LOCK = Lock()
NAMES = {
    "tr": {"skill": "Beceriler", "experience": "Deneyim", "project": "Projeler", "education": "Eğitim",
           "language": "Diller", "certification": "Sertifikalar", "other": "Ek bilgiler"},
    "en": {"skill": "Skills", "experience": "Experience", "project": "Projects", "education": "Education",
           "language": "Languages", "certification": "Certifications", "other": "Additional information"},
}


def find_font(settings):
    if settings.pdf_font:
        candidates = [settings.pdf_font]
    elif os.name == "nt":
        candidates = [Path(os.getenv("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"]
    else:
        candidates = [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                      Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"),
                      Path("/System/Library/Fonts/Supplemental/Arial.ttf")]
    path = next((path for path in candidates if path.is_file()), None)
    if path is None:
        raise Problem("PDF için Türkçe karakter destekli bir TTF fontu PDF_FONT ile seçin.")
    return path


def lines_for(identity, job, facts):
    labels = NAMES[job["language"]]
    lines = [("title", identity["name"])]
    contact = "  |  ".join(filter(None, [identity["email"], identity["phone"]]))
    if contact:
        lines.append(("body", contact))
    for kind, label in labels.items():
        entries = [f for f in facts if f["kind"] == kind]
        if not entries:
            continue
        lines.append(("section", label))
        for fact in entries:
            text = fact["text"]
            if fact["start"] and fact["kind"] == "experience":
                end = fact["end"] or ("Present" if job["language"] == "en" else "Devam ediyor")
                text += f" ({fact['start']} – {end})"
            lines.append(("body", text))
    return lines


def cover_letter(identity, job, facts, suggested_ids=None):
    selected = [f["text"] for f in facts if f["kind"] in {"experience", "project", "skill"}
                and (suggested_ids is None or f["id"] in suggested_ids)][:4]
    if job["language"] == "tr":
        intro = f"Sayın {job['company']} işe alım ekibi,\n\n{job['title']} pozisyonuna başvurmak istiyorum."
        evidence = "\n\nPozisyonla ilgili özgeçmiş bilgilerim:\n" if selected else ""
        sign = "Başvurumu değerlendirdiğiniz için teşekkür ederim.\n\nSaygılarımla,"
    else:
        intro = f"Dear {job['company']} hiring team,\n\nI would like to apply for the {job['title']} position."
        evidence = "\n\nRelevant details from my CV:\n" if selected else ""
        sign = "Thank you for considering my application.\n\nKind regards,"
    return intro + evidence + "\n".join(f"• {text}" for text in selected) + f"\n\n{sign}\n{identity['name']}"


def safe_xml(text):
    return "".join(c for c in text if c in ("\n", "\t", "\r") or 32 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or ord(c) >= 0x10000)


def pdf_bytes(settings, lines):
    path = find_font(settings)
    font = "LocalCV:" + str(path)
    with FONT_LOCK:
        if font not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font, str(path)))
        styles = {
            "title": ParagraphStyle("title", fontName=font, fontSize=20, leading=27, spaceAfter=12),
            "section": ParagraphStyle("section", fontName=font, fontSize=13, leading=18, spaceBefore=12,
                                      spaceAfter=6, textColor=colors.HexColor("#164d50"), keepWithNext=True),
            "body": ParagraphStyle("body", fontName=font, fontSize=10, leading=15, alignment=TA_LEFT, spaceAfter=5),
        }
        result = BytesIO()
        SimpleDocTemplate(result, title="CV", leftMargin=50, rightMargin=50).build([
            Paragraph(escape(safe_xml(line)).replace("\n", "<br/>"), styles[kind]) for kind, line in lines
        ])
        return result.getvalue()


def docx_bytes(lines):
    doc = Document()
    doc.styles["Normal"].font.name = "Arial"
    doc.styles["Normal"].font.size = Pt(11)
    for kind, line in lines:
        if kind == "body":
            doc.add_paragraph(safe_xml(line))
        else:
            doc.add_heading(safe_xml(line), level=0 if kind == "title" else 1)
    output = BytesIO()
    doc.save(output)
    return output.getvalue()
