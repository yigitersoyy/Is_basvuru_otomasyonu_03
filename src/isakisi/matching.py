import json
import re
from datetime import date

from .catalog import SKILLS, contains, job_rows
from .db import notify, pack, stamp
from .profile import experience_years, key, load_profile

RULES = "rules-1"
WEIGHTS = {"skills": 40, "tasks": 25, "experience": 15, "title": 10, "preferences": 10}
LABELS = {
    "title": "Unvan", "country": "Ülke", "work_right": "Çalışma izni", "remote_region": "Uzaktan bölgesi",
    "workplace": "Çalışma biçimi", "seniority": "Kıdem", "salary": "Ücret",
    "skill": "Teknoloji", "exclude_company": "Hariç işveren",
}
ALIASES = {
    "tr": {"tr", "turkiye", "turkey"}, "us": {"us", "u.s", "u.s.a", "usa", "united states", "abd"},
    "uk": {"uk", "gb", "united kingdom", "birlesik krallik"}, "ca": {"ca", "canada", "kanada"},
    "eu": {"eu", "european union", "avrupa birligi"}, "europe": {"europe", "avrupa"},
    "worldwide": {"worldwide", "global", "dunya geneli", "anywhere in the world"},
    "remote": {"remote", "uzaktan"}, "hybrid": {"hybrid", "hibrit"}, "onsite": {"onsite", "on-site", "ofiste"},
    "senior": {"senior", "kidemli"}, "junior": {"junior", "baslangic"},
}


def equivalent(value):
    token = key(value)
    return next((canonical for canonical, names in ALIASES.items() if token in names), token)


def check(pref, job, facts):
    kind, target = pref["kind"], pref["value"].strip()
    expected = equivalent(target)
    got = job.get(kind)
    evidence = job["evidence"].get(kind, str(got or ""))
    if kind == "salary":
        minimum, currency, period, basis = target.split("|")
        valid = (job["currency"].upper(), job["salary_period"], job["salary_basis"]) == (currency, period, basis)
        low, high = job["salary_min"], job["salary_max"]
        if not valid or (low is None and high is None):
            return "unknown", evidence
        evidence = f"{low if low is not None else '—'} – {high if high is not None else '—'} {currency} / {period} / {basis}"
        if low is not None and low >= int(minimum):
            return "met", evidence
        if high is not None and high < int(minimum):
            return "conflict", evidence
        return "unknown", evidence
    if kind == "exclude_company":
        return ("conflict" if key(job["company"]) == key(target) else "met"), job["company"]
    if kind == "skill":
        aliases = next((aliases for name, aliases in SKILLS.items() if key(name) == key(target)), (target,))
        found = next((skill for skill in job["skills"] if any(contains(skill, alias) for alias in aliases)), None)
        return ("met", job["evidence"].get(f"skill:{found}", found)) if found else ("unknown", "")
    if kind == "title":
        return ("met" if contains(job["title"], target) else "conflict"), job["title"]
    if kind == "remote_region" and job["workplace"] in {"hybrid", "onsite"}:
        return "conflict", job["location"] or job["workplace"]
    if not got:
        return "unknown", evidence
    actual = equivalent(got)
    if kind == "remote_region":
        if actual in {expected, "worldwide"}:
            return "met", evidence
        if {actual, expected} & {"eu", "europe", "emea"} and (actual not in {"us", "ca"} and expected not in {"us", "ca"}):
            return "unknown", evidence
    return ("met" if actual == expected else "conflict"), evidence


def assess(job, profile, *, today=None):
    facts = [f for f in profile["facts"] if f["verified_at"]]
    preferences = profile["preferences"]
    reasons = []
    groups = {}

    def score(group, outcomes):
        if outcomes:
            groups[group] = sum(outcomes) / len(outcomes)

    outcomes = []
    for skill in job["skills"]:
        aliases = SKILLS.get(skill, (skill,))
        verified = [f for f in facts if f["kind"] in {"skill", "experience", "project", "certification"}
                    and any(contains(f["text"], alias) for alias in aliases)]
        outcomes.append(bool(verified))
        reasons.append(reason(f"Beceri · {skill}", "met" if verified else "missing",
                              job["evidence"].get(f"skill:{skill}", skill), verified))
    score("skills", outcomes)

    outcomes = []
    for task in job["tasks"]:
        verified = [f for f in facts if f["kind"] in {"experience", "project"} and task_overlap(task, f["text"])]
        outcomes.append(bool(verified))
        reasons.append(reason("Görev / proje", "met" if verified else "missing", task, verified))
    score("tasks", outcomes)

    experience = []
    if job["min_years"] is not None:
        dated = [f for f in facts if f["kind"] == "experience" and f["start"]]
        years = experience_years(dated, today)
        fits = years >= job["min_years"]
        experience.append(fits)
        reasons.append(reason("Deneyim", "met" if fits else "missing",
                              job["evidence"].get("experience", f"{job['min_years']:g} yıl"), dated,
                              f"{years:.1f} yıl doğrulanmış deneyim"))
    required = []
    other = []
    titles = []
    for pref in preferences:
        state, evidence = check(pref, job, facts)
        item = reason(LABELS[pref["kind"]], state, evidence, [], pref["value"])
        item["required"] = pref["required"]
        reasons.append(item)
        if pref["required"]:
            required.append(item)
        if state == "unknown":
            continue
        if pref["kind"] == "title":
            titles.append(state == "met")
        elif pref["kind"] == "seniority":
            experience.append(state == "met")
        else:
            other.append(state == "met")
    score("experience", experience)
    score("title", [any(titles)] if titles else [])
    score("preferences", other)
    coverage = sum(WEIGHTS[group] for group in groups)
    weighted = sum(WEIGHTS[group] * fraction for group, fraction in groups.items())
    points = round(100 * weighted / coverage) if coverage else None
    conflict = any(r["state"] == "conflict" for r in required)
    unknown = any(r["state"] == "unknown" for r in required)
    return {"score": points, "coverage": coverage, "reasons": reasons, "conflict": conflict,
            "review": unknown, "priority": points is not None and points >= 75 and coverage >= 70 and not (conflict or unknown)}


def task_overlap(task, evidence):
    discarded = {"the", "and", "with", "for", "our", "your", "ve", "icin", "gibi", "ile"}
    expected = set(re.findall(r"[a-z0-9+#]{3,}", key(task))) - discarded
    actual = set(re.findall(r"[a-z0-9+#]{3,}", key(evidence))) - discarded
    return bool(expected) and (contains(evidence, task) or len(expected & actual) >= max(2, len(expected) * .65))


def reason(label, state, evidence, facts, detail=""):
    return {"label": label, "state": state, "evidence": evidence, "detail": detail,
            "facts": [{"id": f["id"], "text": f["text"], "source": f["source"]} for f in facts]}


def refresh(conn):
    profile = load_profile(conn)
    if not profile["id"]:
        return 0
    today = date.today()
    rules = f"{RULES}:{today.isoformat()}"
    jobs = job_rows(conn)
    for job in jobs.values():
        result = assess(job["item"], profile, today=today)
        conn.execute("INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?,?)",
                     (job["id"], job["version_id"], profile["id"], rules, pack(result), stamp()))
        if result["priority"] and job["state"] == "open" and not job["duplicate_of"]:
            notify(conn, f"priority:{job['id']}", f"Yüksek öncelik · {job['item']['title']}", f"/jobs/{job['id']}")
    return len(jobs)


def ranked(conn):
    jobs = job_rows(conn)
    profile = load_profile(conn)
    rules = f"{RULES}:{date.today().isoformat()}"
    for job in jobs.values():
        row = conn.execute("SELECT result FROM matches WHERE opportunity_id=? AND posting_version_id=? AND profile_version_id=? AND rules=?",
                           (job["id"], job["version_id"], profile["id"], rules)).fetchone()
        job["match"] = json.loads(row[0]) if row else (assess(job["item"], profile) if profile["id"] else None)
    ordered = sorted(jobs.values(), key=lambda j: j["last_seen"], reverse=True)
    return sorted(ordered, key=lambda j: (
        j["state"] != "open", bool(j["duplicate_of"]), not (j["match"] or {}).get("priority", False),
        (j["match"] or {}).get("conflict", False),
        -((j["match"] or {}).get("score") if (j["match"] or {}).get("score") is not None else -1),
        j["item"]["deadline"] or "9999",
    ))
