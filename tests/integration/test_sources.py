import httpx
import pytest

from isakisi.catalog import import_manual, job_rows
from isakisi.settings import Source
from isakisi.sources import ReadFailure, scan


def source(id, platform, hosts, **kwargs):
    return Source(id=id, platform=platform, hosts=hosts, permission_url="https://example.com/permission",
                  checked_on="2026-11-18", **kwargs)


def test_greenhouse_dedup_and_closure(store):
    portal = source("greenhouse", "greenhouse", ["boards-api.greenhouse.io"], company="A", board="example")
    jobs = [{"id": 4, "title": "Backend Engineer", "requisition_id": "REQ-1", "content": "Python",
             "absolute_url": "https://job-boards.greenhouse.io/example/jobs/4"}]
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"jobs": jobs}))) as client:
        scan(store, portal, client)
        scan(store, portal, client)
        with store.connect(write=True) as conn:
            opp = import_manual(conn, {"title": "Backend Engineer", "company": "A",
                                       "url": "https://boards.greenhouse.io/example/jobs/4?utm_source=none"})
            assert list(job_rows(conn)) == [opp]
            assert conn.execute("SELECT COUNT(*) FROM posting_versions").fetchone()[0] == 2
        jobs.clear()
        scan(store, portal, client)
        with store.connect() as conn:
            assert conn.execute("SELECT state FROM postings WHERE source_id='greenhouse'").fetchone()[0] == "open"
        scan(store, portal, client)
        with store.connect() as conn:
            assert conn.execute("SELECT state FROM postings WHERE source_id='greenhouse'").fetchone()[0] == "closed"


def test_partial_lever_does_not_advance_misses_and_honors_retry_after(store):
    portal = source("lever", "lever", ["api.eu.lever.co"], company="A", board="example", region="eu")
    mode = "one"
    seen = []

    def job(i):
        return {"id": str(i), "text": f"Engineer {i}", "hostedUrl": f"https://jobs.eu.lever.co/example/{i}",
                "applyUrl": f"https://jobs.eu.lever.co/example/{i}/apply"}

    def handler(req):
        assert req.url.host == "api.eu.lever.co"
        skip = req.url.params["skip"]
        seen.append(skip)
        if mode == "one":
            return httpx.Response(200, json=[job(200)])
        if skip == "0":
            return httpx.Response(200, json=[job(i) for i in range(100)])
        return httpx.Response(429, headers={"retry-after": "70"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        scan(store, portal, client)
        mode = "partial"
        with pytest.raises(ReadFailure) as failed:
            scan(store, portal, client, sleep=lambda value: None)
        assert failed.value.retry_after == 70
    assert seen == ["0", "0", "100"]
    with store.connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT external_key,state,misses FROM postings")] == [("200", "open", 0)]
        assert conn.execute("SELECT status FROM source_runs ORDER BY id DESC").fetchone()[0] == "failed"


def test_wwr_disappearance_is_uncertain(store):
    portal = source("wwr", "wwr", ["weworkremotely.com"], url="https://weworkremotely.com/remote-jobs.rss")
    items = ['<item><title>Blue: Python Backend</title><link>https://weworkremotely.com/remote-jobs/blue</link>'
             '<description>Python required.</description></item>']

    def feed(req):
        return httpx.Response(200, text=f'<rss version="2.0"><channel><title>WWR</title>{"".join(items)}</channel></rss>')

    with httpx.Client(transport=httpx.MockTransport(feed)) as client:
        scan(store, portal, client)
        items.clear()
        scan(store, portal, client)
    with store.connect() as conn:
        job = next(iter(job_rows(conn).values()))
        assert job["state"] == "uncertain" and job["item"]["company"] == "Blue"


def test_ashby_visibility_and_jsonld_valid_through(store):
    ashby = source("ashby", "ashby", ["api.ashbyhq.com"], board="test", company="A")
    page = source("career", "jsonld", ["example.com"], company="B", url="https://example.com/career")
    unpublished = {"isListed": False}
    published = {"isListed": True, "jobUrl": "https://jobs.ashbyhq.com/example/xyz", "title": "Engineer"}

    def handler(req):
        if req.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [unpublished, published]})
        return httpx.Response(200, text='<script type="application/ld+json">{"@type":"JobPosting",'
                              '"title":"Manager","validThrough":"2001-01-01","description":"Archived."}</script>')

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert scan(store, ashby, client) == 1
        assert scan(store, page, client) == 1
    with store.connect() as conn:
        assert {r["item"]["title"]: r["state"] for r in job_rows(conn).values()} == {"Engineer": "open", "Manager": "closed"}


def test_sitemap_only_follows_permitted_hosts(store):
    portal = source("sitemap", "jsonld", ["example.com"], company="A", url="https://example.com/jobs.xml")
    urls = []

    def handler(req):
        urls.append(str(req.url))
        if req.url.path == "/jobs.xml":
            return httpx.Response(200, text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                  '<url><loc>https://example.com/engineer</loc></url></urlset>')
        return httpx.Response(200, text='<script type="application/ld+json">{"@type":"JobPosting","title":"Engineer"}</script>')

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert scan(store, portal, client, sleep=lambda _: None) == 1
    assert urls == ["https://example.com/jobs.xml", "https://example.com/engineer"]
