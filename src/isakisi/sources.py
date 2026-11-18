import html
import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import feedparser
import httpx
from bs4 import BeautifulSoup

from .catalog import Posting, plain, save_posting, source_row
from .db import stamp
from .settings import Problem, allowed_url


class ReadFailure(Problem):
    def __init__(self, code, message, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(message)


def retry_delay(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()))
        except (ValueError, TypeError):
            return 0


class Reader:
    def __init__(self, source, client, sleep=time.sleep):
        self.source = source
        self.client = client
        self.sleep = sleep
        self.requested = False

    def get(self, url, params=None):
        redirects = 0
        while redirects <= 4:
            allowed_url(url, self.source.hosts)
            if self.requested:
                self.sleep(self.source.delay_seconds)
            self.requested = True
            try:
                with self.client.stream("GET", url, params=params, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if "location" not in response.headers:
                            raise ReadFailure("redirect", "Kaynak yönlendirmesi eksik.")
                        url = urljoin(str(response.url), response.headers["location"])
                        params = None
                        redirects += 1
                        continue
                    if response.status_code != 200:
                        raise ReadFailure(str(response.status_code), "Kaynak taraması tamamlanamadı.",
                                          retry_delay(response.headers.get("retry-after")))
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 12 * 1024 * 1024:
                            raise ReadFailure("size", "Kaynak yanıtı boyut sınırını aştı.")
                    return bytes(data)
            except httpx.HTTPError:
                raise ReadFailure("network", "Kaynak bağlantısı kesildi.") from None
        raise ReadFailure("redirect", "Kaynak yönlendirme sınırını aştı.")

    def json(self, url, params=None):
        return json.loads(self.get(url, params))


def board_url(source):
    if source.platform == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{source.board}/jobs"
    if source.platform == "lever":
        host = "api.eu.lever.co" if source.region == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{source.board}"
    if source.platform == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{source.board}"
    return source.url


def greenhouse(source, job):
    location = job.get("location") or {}
    return Posting(external_key=str(job["id"]), title=job["title"], company=source.company,
                   url=job["absolute_url"], canonical=job["absolute_url"],
                   requisition=str(job.get("requisition_id") or ""),
                   description=plain(html.unescape(job.get("content") or "")), language=job.get("language") or "und",
                   location=location.get("name", ""), published=job.get("first_published") or job.get("updated_at") or "")


def lever(source, job):
    categories = job.get("categories") or {}
    location = categories.get("location") or ""
    descriptions = [job.get("descriptionPlain") or plain(job.get("description"))]
    descriptions.extend(f"{group.get('text', '')}\n{plain(group.get('content'))}" for group in job.get("lists") or [])
    salary = job.get("salaryRange") or {}
    return Posting(external_key=job["id"], title=job["text"], company=source.company, url=job["hostedUrl"],
                   canonical=job["hostedUrl"], apply_url=job.get("applyUrl") or "",
                   description="\n".join(descriptions), workplace=job.get("workplaceType") or "",
                   location=location, country=job.get("country") or "",
                   salary_min=salary.get("min"), salary_max=salary.get("max"), currency=salary.get("currency") or "",
                   salary_period=period(salary.get("interval")))


def period(value):
    return {"hour": "hour", "hourly": "hour", "month": "month", "monthly": "month", "year": "year",
            "yearly": "year", "annual": "year"}.get(str(value).lower(), "")


def ashby(source, job):
    address = (job.get("address") or {}).get("postalAddress") or {}
    tiers = (job.get("compensation") or {}).get("compensationTiers") or []
    components = [item for tier in tiers for item in tier.get("components", []) if item.get("compensationType") == "Salary"]
    salary = components[0] if len(components) == 1 else {}
    return Posting(external_key=job["jobUrl"], title=job["title"], company=source.company, url=job["jobUrl"],
                   canonical=job["jobUrl"], apply_url=job.get("applyUrl") or "",
                   workplace=(job.get("workplaceType") or "").lower(),
                   description=job.get("descriptionPlain") or plain(job.get("descriptionHtml")),
                   location=job.get("location") or "", city=address.get("addressLocality") or "",
                   country=address.get("addressCountry") or "", published=job.get("publishedAt") or "",
                   salary_min=salary.get("minValue"), salary_max=salary.get("maxValue"),
                   currency=salary.get("currencyCode") or "", salary_period=period(salary.get("interval")))


def jsonld_nodes(document):
    soup = BeautifulSoup(document, "html.parser")

    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            kinds = node.get("@type", [])
            if kinds == "JobPosting" or (isinstance(kinds, list) and "JobPosting" in kinds):
                yield node
            yield from walk(node.get("@graph"))

    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                value = json.loads(script.string)
            except ValueError:
                continue
            yield from walk(value)


def jsonld(source, job, url):
    identifier = job.get("identifier") or {}
    org = job.get("hiringOrganization") or {}
    locations = job.get("jobLocation") or []
    location = locations[0] if isinstance(locations, list) and len(locations) == 1 else locations
    address = location.get("address", {}) if isinstance(location, dict) else {}
    country = address.get("addressCountry") or ""
    if isinstance(country, dict):
        country = country.get("name") or ""
    salary = job.get("baseSalary") or {}
    value = salary.get("value") or {}
    if not isinstance(value, dict):
        value = {"value": value}
    link = allowed_url(urljoin(url, job.get("url") or url), source.hosts)
    region = job.get("applicantLocationRequirements") or {}
    if isinstance(region, list):
        region = region[0] if len(region) == 1 else {}
    req = identifier.get("value") if isinstance(identifier, dict) else identifier
    return Posting(external_key=str(req or link), requisition=str(req or ""), title=job["title"],
                   company=org.get("name") or source.company, url=link, canonical=link,
                   description=plain(job.get("description")), country=country, city=address.get("addressLocality") or "",
                   location=", ".join(filter(None, [address.get("addressLocality"), country])),
                   workplace="remote" if job.get("jobLocationType") == "TELECOMMUTE" else "",
                   remote_region=region.get("name") or "", deadline=job.get("validThrough") or "",
                   published=job.get("datePosted") or "", salary_min=value.get("minValue", value.get("value")),
                   salary_max=value.get("maxValue", value.get("value")), currency=salary.get("currency") or "",
                   salary_period=period(value.get("unitText")))


def wwr(job, url):
    label = job["title"]
    company, separator, title = label.partition(":")
    company = job.get("company") or (company.strip() if separator else "We Work Remotely")
    link = allowed_url(urljoin(url, job["link"]), ["weworkremotely.com"])
    return Posting(external_key=job.get("id") or link, title=title.strip() if separator else label,
                   company=company, url=link, canonical=link, description=plain(job.get("summary")), workplace="remote",
                   location=job.get("region") or "", remote_region=job.get("region") or "",
                   published=job.get("published") or "")


def structured_urls(data, url):
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    kind = root.tag.rsplit("}", 1)[-1]
    if kind in {"urlset", "sitemapindex"}:
        return [urljoin(url, tag.text.strip()) for tag in root.findall(".//{*}loc") if tag.text]
    if kind in {"rss", "feed", "RDF"}:
        parsed = feedparser.parse(data)
        if parsed.bozo:
            raise ReadFailure("format", "Kariyer beslemesi ayrıştırılamadı.")
        return [urljoin(url, item["link"]) for item in parsed.entries if item.get("link")]
    return None


def career_pages(source, reader, url):
    seen = set()

    def walk(link, depth):
        if link in seen:
            return
        if len(seen) >= 500 or depth > 4:
            raise ReadFailure("pagination", "Kariyer sayfası tarama sınırını aştı.")
        seen.add(link)
        try:
            data = reader.get(link)
        except ReadFailure as error:
            if error.code == "410":
                yield None if depth == 0 else []
                return
            raise
        links = structured_urls(data, link)
        if links is not None:
            for child in links:
                yield from walk(child, depth + 1)
            if not links:
                yield []
            return
        jobs = list(jsonld_nodes(data))
        if not jobs and depth == 0:
            raise ReadFailure("format", "Sayfada JobPosting verisi bulunamadı.")
        yield [(jsonld(source, job, link), job) for job in jobs]

    yield from walk(url, 0)


def pages(source, reader):
    url = board_url(source)
    if source.platform == "fixture":
        yield [(item, None) for item in fixture()]
    elif source.platform == "greenhouse":
        data = reader.json(url, {"content": "true"})
        jobs = data["jobs"]
        if not isinstance(jobs, list) or (data.get("meta") and data["meta"].get("total") not in (None, len(jobs))):
            raise ReadFailure("partial", "Greenhouse listesi eksik.")
        yield [(greenhouse(source, job), job) for job in jobs]
    elif source.platform == "lever":
        ids = set()
        for skip in range(0, 10000, 100):
            jobs = reader.json(url, {"mode": "json", "skip": skip, "limit": 100})
            if not isinstance(jobs, list):
                raise ReadFailure("format", "Lever liste biçimi değişti.")
            page_ids = {job["id"] for job in jobs}
            if len(page_ids) != len(jobs) or page_ids & ids:
                raise ReadFailure("pagination", "Lever sayfalama tamamlanamadı.")
            ids.update(page_ids)
            yield [(lever(source, job), job) for job in jobs]
            if len(jobs) < 100:
                return
        raise ReadFailure("pagination", "Lever sayfa sınırını aştı.")
    elif source.platform == "ashby":
        data = reader.json(url, {"includeCompensation": "true"})
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise ReadFailure("format", "Ashby liste biçimi değişti.")
        yield [(ashby(source, job), job) for job in jobs if job.get("isListed") is True]
    elif source.platform == "wwr":
        data = feedparser.parse(reader.get(url))
        if data.bozo or not data.version:
            raise ReadFailure("format", "RSS beslemesi ayrıştırılamadı.")
        yield [(wwr(job, url), dict(job)) for job in data.entries]
    elif source.platform == "jsonld":
        yield from career_pages(source, reader, url)


def scan(store, source, client=None, sleep=time.sleep, lease=None):
    if not source.read:
        raise Problem("Kaynağın okuma izni kapalı.")
    with store.connect(write=True) as conn:
        source_row(conn, source)
        run = conn.execute("INSERT INTO source_runs(source_id,started,status) VALUES (?,?,'running')",
                           (source.id, stamp())).lastrowid
    fetched = []
    gone = False
    own_client = client is None
    client = client or httpx.Client(timeout=15, trust_env=False, headers={"User-Agent": "isakisi/0.1"})
    try:
        for idx, page in enumerate(pages(source, Reader(source, client, sleep)), 1):
            if page is None:
                gone = True
            else:
                fetched.extend(page)
            with store.connect(write=True) as conn:
                if lease:
                    lease(conn)
                conn.execute("UPDATE source_runs SET page=? WHERE id=?", (idx, run))
        with store.connect(write=True) as conn:
            if lease:
                lease(conn)
            old = {row["external_key"]: row["id"] for row in conn.execute(
                "SELECT id, external_key FROM postings WHERE source_id=?", (source.id,))}
            for item, raw in fetched:
                save_posting(conn, source, item, raw)
                old.pop(item.external_key, None)
            for posting_id in old.values():
                if gone:
                    conn.execute("UPDATE postings SET state='closed' WHERE id=?", (posting_id,))
                elif source.platform == "wwr":
                    conn.execute("UPDATE postings SET state='uncertain' WHERE id=? AND state='open'", (posting_id,))
                else:
                    conn.execute("""UPDATE postings SET misses=misses+1,
                                  state=CASE WHEN misses>=1 THEN 'closed' ELSE state END WHERE id=?""", (posting_id,))
            conn.execute("UPDATE sources SET last_complete=? WHERE id=?", (stamp(), source.id))
            conn.execute("UPDATE source_runs SET status='complete', finished=? WHERE id=?", (stamp(), run))
        return len(fetched)
    except Exception as error:
        reason = getattr(error, "code", "invalid")
        with store.connect(write=True) as conn:
            conn.execute("UPDATE source_runs SET status='failed',finished=?,error=? WHERE id=?", (stamp(), reason, run))
        if isinstance(error, Problem):
            raise
        raise ReadFailure("invalid", "Kaynak verisi beklenen biçimde değil.") from None
    finally:
        if own_client:
            client.close()


def fixture():
    common = {"workplace": "remote", "remote_region": "worldwide", "language": "tr"}
    rows = [
        ("Atlas Teknoloji", "Python Backend Geliştirici", "Uzaktan · Dünya geneli",
         "Gereksinimler\nPython, SQL, FastAPI ve Docker\n3 yıl deneyim\nSorumluluklar\nREST API geliştirme", common),
        ("Meridyen Veri", "Veri Mühendisi", "Türkiye · Hibrit",
         "Aranan nitelikler\nPython, SQL ve PostgreSQL\nSorumluluklar\nVeri hatları geliştirme",
         {"country": "TR", "workplace": "hybrid", "language": "tr"}),
        ("Pusula Yazılım", "Full Stack Engineer", "Remote · Europe",
         "Requirements\nReact, TypeScript and Python\n4 years of experience\nResponsibilities\nBuild web applications",
         {"workplace": "remote", "remote_region": "europe", "language": "en"}),
        ("Kuzey Bulut", "Platform Engineer", "Remote · US",
         "Requirements\nKubernetes and Terraform\n5 years experience\nResponsibilities\nMaintain cloud infrastructure",
         {"workplace": "remote", "remote_region": "us", "language": "en"}),
        ("Kıyı Labs", "Backend Mühendisi", "İstanbul · Hibrit",
         "Gereksinimler\nPython ve Django\n2 yıl deneyim\nSorumluluklar\nREST API geliştirme",
         {"country": "TR", "city": "İstanbul", "workplace": "hybrid", "language": "tr"}),
        ("Yörünge Ürün", "Ürün Analisti", "Uzaktan · Dünya geneli",
         "Gereksinimler\nSQL, Excel ve Power BI\nSorumluluklar\nÜrün ölçümlerini analiz etme", common),
    ]
    for i, (company, title, location, description, fields) in enumerate(rows, 1):
        yield Posting(external_key=str(i), company=company, title=title, location=location,
                      url=f"https://example.invalid/jobs/{i}", description=description, **fields)
