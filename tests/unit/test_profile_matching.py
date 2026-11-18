from datetime import date

import pytest
from docx import Document

from isakisi.catalog import Posting, normalize
from isakisi.matching import assess
from isakisi.profile import experience_years, import_facts, load_profile
from isakisi.settings import Problem, allowed_url, web_url


def test_utf8_docx_import_is_unverified(tmp_path):
    path = tmp_path / "özgeçmiş.docx"
    cv = Document()
    cv.add_heading("Beceriler", 1)
    cv.add_paragraph("Python, SQL, Türkçe")
    cv.save(path)
    facts = import_facts(path.name, path.read_bytes())
    assert [fact["text"] for fact in facts] == ["Python", "SQL", "Türkçe"]
    assert all(fact["kind"] == "skill" and not fact["verified_at"] for fact in facts)


def test_overlapping_experience_counted_once():
    dates = [("2020-01-01", "2022-01-01"), ("2021-01-01", "2023-01-01"), ("2023-01-01", None)]
    facts = [{"kind": "experience", "start": start, "end": end, "verified_at": "yes"} for start, end in dates]
    assert experience_years(facts, date(2024, 1, 1)) == pytest.approx(4, abs=.01)


def test_missing_salary_and_restricted_remote(candidate):
    with candidate.connect() as conn:
        profile = load_profile(conn)
    profile["preferences"].append({"kind": "salary", "required": True, "value": "60000|EUR|year|gross"})
    job = normalize(Posting(external_key="example", url="https://example.invalid/job", title="Backend Engineer",
                            company="Test", location="Remote - US", description="Requirements\nPython is required.")).model_dump()
    result = assess(job, profile)
    assert result == assess(job, profile)
    assert result["conflict"] and result["review"] and not result["priority"]
    assert any(x["state"] == "conflict" and x["evidence"] == "Remote - US" for x in result["reasons"])
    assert any(x["label"] == "Ücret" and x["state"] == "unknown" for x in result["reasons"])
    assert any(x["facts"][0]["source"] == "Doğrulanan beceriler" for x in result["reasons"] if x["facts"])


@pytest.mark.parametrize("link", ["javascript:alert(0)", "http://example.com", "https://a@host.example", "https://host.example:8080"])
def test_links_validate(link):
    with pytest.raises(Problem):
        web_url(link)
    with pytest.raises(Problem):
        allowed_url("https://other.example/jobs", ["company.example"])
