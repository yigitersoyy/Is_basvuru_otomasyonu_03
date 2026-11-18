import json
import re
import unicodedata
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Literal

from docx import Document
from pydantic import BaseModel, Field, ValidationError, model_validator
from pypdf import PdfReader

from .db import pack, stamp
from .settings import Problem


class Identity(BaseModel):
    name: str = Field(default="", max_length=160)
    email: str = Field(default="", pattern=r"^(?:[^\s@]+@[^\s@]+\.[^\s@]+)?$", max_length=254)
    phone: str = Field(default="", max_length=80)


class Fact(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: Literal["skill", "experience", "project", "education", "language", "certification", "other"] = "other"
    text: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=2400)
    language: Literal["tr", "en", "und"] = "und"
    start: date | None = None
    end: date | None = None
    verified_at: str | None = None

    @model_validator(mode="after")
    def period(self):
        if self.end and (not self.start or self.end < self.start):
            raise ValueError("Deneyim dönemi geçersiz.")
        if not self.text.strip() or not self.source.strip():
            raise ValueError("Olgu ve kaynak metni gerekli.")
        return self


class Preference(BaseModel):
    kind: Literal["title", "country", "work_right", "remote_region", "workplace",
                  "seniority", "salary", "skill", "exclude_company"]
    value: str = Field(min_length=1, max_length=200)
    required: bool = False


def key(value):
    value = value.replace("ı", "i").casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).strip()


def load_profile(conn):
    row = conn.execute("SELECT * FROM profile_versions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"id": None, "identity": Identity().model_dump(), "facts": [], "preferences": []}
    return {"id": row["id"], **{name: json.loads(row[name]) for name in ("identity", "facts", "preferences")}}


def save_profile(conn, identity, facts, preferences):
    try:
        identity = Identity.model_validate(identity)
        facts = [Fact.model_validate(f) for f in facts]
        preferences = [Preference.model_validate(p) for p in preferences]
    except ValidationError:
        raise Problem("Profil, kaynak metni veya tercih alanlarını kontrol edin.") from None
    if len({f.id for f in facts}) != len(facts):
        raise Problem("Olgu kimlikleri benzersiz olmalı.")
    for preference in preferences:
        if preference.kind == "salary" and not re.fullmatch(
            r"[1-9][0-9]*\|[A-Z]{3}\|(hour|month|year)\|(gross|net)", preference.value
        ):
            raise Problem("Ücret biçimi: 60000|EUR|year|gross")
    conn.execute("INSERT OR IGNORE INTO profiles VALUES (1)")
    return conn.execute(
        "INSERT INTO profile_versions(profile_id,identity,facts,preferences,created) VALUES(1,?,?,?,?)",
        (pack(identity.model_dump()), pack([f.model_dump(mode="json") for f in facts]),
         pack([p.model_dump() for p in preferences]), stamp())
    ).lastrowid


def experience_years(facts, as_of=None):
    as_of = as_of or date.today()
    intervals = sorted((date.fromisoformat(f["start"]), date.fromisoformat(f["end"]) if f.get("end") else as_of)
                       for f in facts if f["kind"] == "experience" and f.get("start") and f.get("verified_at"))
    merged = []
    for start, end in intervals:
        end = min(end, as_of)
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum((end - start).days for start, end in merged) / 365.2425


def import_facts(name: str, content: bytes):
    if len(content) > 8 * 1024 * 1024:
        raise Problem("En fazla 8 MB boyutunda CV yükleyin.")
    suffix = Path(name).suffix.lower()
    try:
        if suffix == ".pdf":
            pages = [(i, p.extract_text() or "") for i, p in enumerate(PdfReader(BytesIO(content)).pages, 1)]
        elif suffix == ".docx":
            doc = Document(BytesIO(content))
            lines = [p.text for p in doc.paragraphs]
            lines.extend(" | ".join(c.text for c in r.cells) for t in doc.tables for r in t.rows)
            pages = [(None, "\n".join(lines))]
        elif suffix == ".txt":
            pages = [(None, content.decode("utf-8-sig"))]
        else:
            raise Problem("PDF, DOCX veya UTF-8 metin dosyası seçin.")
    except Problem:
        raise
    except Exception:
        raise Problem("Dosya okunamadı. UTF-8 metin veya elle giriş kullanın.") from None
    facts = []
    section = "other"
    sections = {
        "skills": "skill", "beceriler": "skill", "yetenekler": "skill",
        "experience": "experience", "deneyim": "experience", "is deneyimi": "experience",
        "projects": "project", "projeler": "project", "education": "education", "egitim": "education",
        "languages": "language", "diller": "language", "certifications": "certification",
        "sertifikalar": "certification",
    }
    for page, contents in pages:
        for line in contents.splitlines():
            line = line.strip().lstrip("•–- ").strip()
            if not line:
                continue
            heading = sections.get(key(line.rstrip(":")))
            if heading:
                section = heading
                continue
            candidates = re.split(r"\s*[,;]\s*", line) if section == "skill" else [line]
            for candidate in filter(None, candidates):
                source = f"Sayfa {page}\n{line[:2000]}" if page else line[:2000]
                facts.append(Fact(kind=section, text=candidate[:2000], source=source).model_dump(mode="json"))
    if not facts:
        raise Problem("Seçilebilir CV metni bulunamadı. Metin veya elle giriş kullanın.")
    return facts
