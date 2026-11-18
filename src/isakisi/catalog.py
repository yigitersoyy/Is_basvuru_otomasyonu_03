import json
import re
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

from .db import digest, pack, stamp
from .profile import key
from .settings import Problem, Source, web_url


class Posting(BaseModel):
    external_key: str
    title: str = Field(min_length=1, max_length=500)
    company: str = Field(min_length=1, max_length=300)
    url: str
    apply_url: str = ""
    canonical: str = ""
    requisition: str = ""
    description: str = ""
    language: str = "und"
    location: str = ""
    city: str = ""
    country: str = ""
    workplace: str = ""
    remote_region: str = ""
    work_right: str = ""
    seniority: str = ""
    skills: list[str] = []
    tasks: list[str] = []
    min_years: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str = ""
    salary_period: str = ""
    salary_basis: str = ""
    deadline: str = ""
    published: str = ""
    closed: bool = False
    evidence: dict[str, str] = {}
    questions: list[dict] = []
    schema_verified: bool = False

    @field_validator("url", "apply_url", "canonical")
    @classmethod
    def safe_link(cls, value):
        return web_url(value) if value else value


SKILLS = {
    "Python": ("python",), "SQL": ("sql",), "PostgreSQL": ("postgres", "postgresql"),
    "SQLite": ("sqlite",), "FastAPI": ("fastapi",), "Django": ("django",),
    "JavaScript": ("javascript", "js"), "TypeScript": ("typescript",), "React": ("react", "reactjs"),
    "Node.js": ("node.js", "nodejs"), "Java": ("java",), "C#": ("c#", "c sharp"),
    "C++": ("c++",), "Go": ("golang", "go programming"), "Rust": ("rust",),
    "Docker": ("docker",), "Kubernetes": ("kubernetes", "k8s"), "AWS": ("aws",),
    "Azure": ("azure",), "GCP": ("gcp", "google cloud"), "Git": ("git",),
    "Linux": ("linux",), "REST": ("rest api", "restful"), "GraphQL": ("graphql",),
    "Redis": ("redis",), "Terraform": ("terraform",), "CI/CD": ("ci/cd",),
    "Figma": ("figma",), "Excel": ("excel",), "Power BI": ("power bi",),
}


def contains(text, word):
    return bool(re.search(r"(?<!\w)" + re.escape(key(word)) + r"(?!\w)", key(text)))


def plain(content):
    soup = BeautifulSoup(content or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())[:40000]


def normalize(posting: Posting):
    body = posting.description
    posting.workplace = {
        "on-site": "onsite", "on site": "onsite", "unspecified": "", "belirtilmemiş": "",
    }.get(posting.workplace.lower(), posting.workplace.lower())
    lines = [s.strip() for s in re.split(r"\n+|(?<=\.)\s+", body) if s.strip()]
    required = []
    responsibilities = []
    section = ""
    for line in lines:
        line_key = key(line).strip(":")
        if line_key in {"requirements", "qualifications", "what you bring", "gereksinimler", "aranan nitelikler"}:
            section = "required"
            continue
        if line_key in {"responsibilities", "what you'll do", "what you will do", "sorumluluklar", "gorevler"}:
            section = "tasks"
            continue
        if line_key in {"benefits", "nice to have", "preferred qualifications", "yan haklar", "tercihen"}:
            section = ""
            continue
        if section == "required" or re.search(r"\b(required|must have|requirements|zorunlu|bekliyoruz)\b", line_key):
            if not re.search(r"\b(optional|nice to have|preferred|tercihen)\b|\bnot required\b", line_key):
                required.append(line)
        if section == "tasks":
            responsibilities.append(line)
    if not posting.skills:
        for skill, aliases in SKILLS.items():
            evidence = next((line for line in required if any(contains(line, x) for x in aliases)), None)
            if evidence:
                posting.skills.append(skill)
                posting.evidence[f"skill:{skill}"] = evidence
    posting.tasks = posting.tasks or responsibilities[:12]
    if posting.min_years is None:
        result = next(((m, line) for line in required if (m := re.search(
            r"\b(\d{1,2})\s*\+?\s*(?:years?\b|yil\b)", key(line)
        ))), None)
        if result:
            posting.min_years = int(result[0][1])
            posting.evidence["experience"] = result[1]
    if not posting.seniority:
        for level, labels in {"senior": ("senior", "kıdemli"), "junior": ("junior", "entry level"),
                              "lead": ("lead", "principal"), "mid": ("mid-level", "intermediate")}.items():
            if any(contains(posting.title, label) for label in labels):
                posting.seniority = level
                posting.evidence["seniority"] = posting.title
                break
    if not posting.workplace:
        terms = {"remote": ("remote", "uzaktan"), "hybrid": ("hybrid", "hibrit"),
                 "onsite": ("onsite", "on-site", "ofiste")}
        for mode, aliases in terms.items():
            if any(contains(posting.location, alias) for alias in aliases):
                posting.workplace = mode
                posting.evidence["workplace"] = posting.location
                break
    if not posting.remote_region:
        for line in [posting.location, *lines]:
            pattern = r"(?:remote\s*[-:(]?\s*|uzaktan\s*[-:(]?\s*|based in |reside in )(united states|u\.?s\.?a\.?\b|u\.?s\.?\b|uk\b|united kingdom|turkiye|turkey|canada|eu\b|emea\b|europe\b|worldwide\b|global\b|dunya geneli)"
            result = re.search(pattern, key(line))
            if result:
                posting.remote_region = result[1].strip(".")
                posting.evidence["remote_region"] = line
                break
    if not posting.work_right:
        for line in lines:
            result = re.search(r"(?:authorized|authorised|eligible) to work in (?:the )?(us\b|usa\b|united states|uk\b|united kingdom|turkey|turkiye|canada|eu\b)", key(line))
            if result:
                posting.work_right = result[1]
                posting.evidence["work_right"] = line
                break
    if not posting.country:
        regions = {"TR": ("turkey", "türkiye"), "US": ("united states", "usa"),
                   "CA": ("canada", "kanada"), "GB": ("united kingdom", "birleşik krallık")}
        country = next((code for code, aliases in regions.items()
                        if any(contains(posting.location, alias) for alias in aliases)), "")
        if country:
            posting.country = country
            posting.evidence["country"] = posting.location
    if not posting.salary_basis:
        for line in lines:
            match = re.search(r"\b(gross|net|brut)\b", key(line))
            if match and re.search(r"\b(salary|compensation|ucret|maas)\b", key(line)):
                posting.salary_basis = "gross" if match[1] in {"gross", "brut"} else "net"
                posting.evidence["salary"] = line
                break
    if posting.language not in {"tr", "en"}:
        posting.language = "tr" if re.search(r"[çğıöşüÇĞİÖŞÜ]", f"{posting.title} {body}") else "en"
    return posting


def canonical_url(value):
    if not value:
        return ""
    url = urlsplit(value)
    query = [(k, v) for k, v in parse_qsl(url.query) if not k.startswith("utm_") and k not in {"ref", "source", "gh_src"}]
    host = "boards.greenhouse.io" if url.hostname == "job-boards.greenhouse.io" else url.hostname
    return urlunsplit(("https", host, url.path.rstrip("/"), urlencode(sorted(query)), ""))


def source_row(conn, source: Source):
    config = pack(source.model_dump(exclude={"secret_env"}))
    existing = conn.execute("SELECT * FROM sources WHERE id=?", (source.id,)).fetchone()
    if existing:
        previous = json.loads(existing["config"])
        if (existing["platform"] != source.platform
                or any(previous[f] != getattr(source, f) for f in ("board", "region", "url", "company"))):
            raise Problem("Pano kimliği değişti. Yeni bir kaynak kimliği kullanın.")
        conn.execute("UPDATE sources SET config=? WHERE id=?", (config, source.id))
    else:
        conn.execute("INSERT INTO sources(id,platform,config) VALUES (?,?,?)", (source.id, source.platform, config))


def save_posting(conn, source, item: Posting, raw=None):
    item = normalize(item)
    now = stamp()
    data = item.model_dump()
    content_id = digest(data)
    canonical = canonical_url(item.canonical)
    company, title = key(item.company), key(item.title)
    posting = conn.execute("SELECT * FROM postings WHERE source_id=? AND external_key=?",
                           (source.id, item.external_key)).fetchone()
    if posting:
        opportunity = posting["opportunity_id"]
        posting_id = posting["id"]
    else:
        matches = {r[0] for r in conn.execute(
            """SELECT id FROM opportunities WHERE (company_key=? AND requisition=? AND ?<>'')
            OR (canonical_url=? AND ?<>'')""", (company, item.requisition, item.requisition, canonical, canonical)
        )}
        if len(matches) == 1:
            opportunity = matches.pop()
            conn.execute("UPDATE opportunities SET requisition=COALESCE(requisition,?), canonical_url=COALESCE(canonical_url,?) WHERE id=?",
                         (item.requisition or None, canonical or None, opportunity))
        else:
            similar = next(iter(sorted(matches)), None)
            if similar is None and source.platform != "fixture":
                similar = possible_duplicate(conn, company, title, item)
            opportunity = conn.execute(
                "INSERT INTO opportunities(company_key,title_key,requisition,canonical_url,duplicate_of,created) VALUES (?,?,?,?,?,?)",
                (company, title, item.requisition or None, canonical or None, similar, now)).lastrowid
        posting_id = conn.execute(
            "INSERT INTO postings(source_id,external_key,opportunity_id,first_seen,last_seen) VALUES (?,?,?,?,?)",
            (source.id, item.external_key, opportunity, now, now)).lastrowid
    latest = conn.execute("SELECT digest FROM posting_versions WHERE posting_id=? ORDER BY id DESC LIMIT 1",
                          (posting_id,)).fetchone()
    if not latest or latest[0] != content_id:
        conn.execute("INSERT INTO posting_versions(posting_id,digest,data,raw,created) VALUES (?,?,?,?,?)",
                     (posting_id, content_id, pack(data), pack(raw) if raw is not None else None, now))
    conn.execute("UPDATE postings SET last_seen=?, misses=0,state=? WHERE id=?",
                 (now, "closed" if expired(item) else "open", posting_id))
    return opportunity


def possible_duplicate(conn, company, title, item):
    import difflib
    rows = conn.execute(
        """SELECT o.id,v.data FROM opportunities o JOIN postings p ON p.opportunity_id=o.id
        JOIN posting_versions v ON v.id=(SELECT MAX(id) FROM posting_versions WHERE posting_id=p.id)
        WHERE o.company_key=? AND o.title_key=? AND (o.requisition IS NULL OR ?='' OR o.requisition=?)""",
        (company, title, item.requisition, item.requisition)
    )
    for row in rows:
        other = json.loads(row["data"])
        if key(other["location"]) == key(item.location) and len(item.description) >= 40:
            ratio = difflib.SequenceMatcher(None, key(other["description"][:4000]), key(item.description[:4000])).ratio()
            if ratio >= .85:
                return row["id"]
    return None


def expired(item):
    if item.closed:
        return True
    if not item.deadline:
        return False
    try:
        if len(item.deadline) == 10:
            return date.fromisoformat(item.deadline) < datetime.now(UTC).date()
        end = datetime.fromisoformat(item.deadline.replace("Z", "+00:00"))
        return (end if end.tzinfo else end.replace(tzinfo=UTC)) <= datetime.now(UTC)
    except ValueError:
        return False


def job_rows(conn):
    rows = conn.execute(
        """SELECT o.*,p.id AS posting_id,p.source_id,p.external_key,p.state,p.last_seen,s.platform,
        v.id AS version_id,v.data,v.digest
        FROM opportunities o JOIN postings p ON p.opportunity_id=o.id JOIN sources s ON s.id=p.source_id
        JOIN posting_versions v ON v.id=(SELECT MAX(id) FROM posting_versions WHERE posting_id=p.id)
        ORDER BY CASE p.state WHEN 'open' THEN 0 WHEN 'uncertain' THEN 1 ELSE 2 END,
        CASE s.platform WHEN 'wwr' THEN 2 WHEN 'manual' THEN 1 ELSE 0 END, p.last_seen DESC, p.id DESC"""
    )
    jobs = {}
    authoritative = {"greenhouse", "lever", "ashby", "jsonld"}
    for row in rows:
        data = json.loads(row["data"])
        row = dict(row)
        row["item"] = data
        if expired(Posting.model_validate(data)):
            row["state"] = "closed"
        selected = jobs.get(row["id"])
        authority = row["platform"] in authoritative
        if (not selected or (authority and selected["platform"] not in authoritative)
                or (authority == (selected["platform"] in authoritative)
                    and selected["state"] == "closed" and row["state"] != "closed")):
            jobs[row["id"]] = row
    return jobs


def import_manual(conn, data):
    source = Source(id="manual", platform="manual")
    source_row(conn, source)
    item = Posting.model_validate({**data, "external_key": canonical_url(web_url(data["url"])),
                                  "canonical": data["url"]})
    return save_posting(conn, source, item)
