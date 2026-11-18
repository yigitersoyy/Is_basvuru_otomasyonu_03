CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY CHECK (id = 1)
);
CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
    identity TEXT NOT NULL, facts TEXT NOT NULL, preferences TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, platform TEXT NOT NULL, config TEXT NOT NULL, last_complete TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY, company_key TEXT NOT NULL, title_key TEXT NOT NULL,
    requisition TEXT, canonical_url TEXT, duplicate_of INTEGER REFERENCES opportunities(id),
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunity_req ON opportunities(company_key, requisition);
CREATE INDEX IF NOT EXISTS opportunity_url ON opportunities(canonical_url);
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    external_key TEXT NOT NULL, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, misses INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open', UNIQUE (source_id, external_key)
);
CREATE TABLE IF NOT EXISTS posting_versions (
    id INTEGER PRIMARY KEY, posting_id INTEGER NOT NULL REFERENCES postings(id),
    digest TEXT NOT NULL, data TEXT NOT NULL, raw TEXT, created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posting_current ON posting_versions(posting_id, id);
CREATE TABLE IF NOT EXISTS matches (
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
    posting_version_id INTEGER NOT NULL REFERENCES posting_versions(id),
    profile_version_id INTEGER NOT NULL REFERENCES profile_versions(id),
    rules TEXT NOT NULL, result TEXT NOT NULL, created TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, posting_version_id, profile_version_id, rules)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
    state TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
    UNIQUE(profile_id, opportunity_id)
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
    state TEXT NOT NULL, evidence TEXT NOT NULL, note TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bundles (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
    posting_version_id INTEGER NOT NULL REFERENCES posting_versions(id),
    profile_version_id INTEGER NOT NULL REFERENCES profile_versions(id),
    digest TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bundle_current ON bundles(application_id, id);
CREATE TABLE IF NOT EXISTS documents (
    bundle_id INTEGER NOT NULL REFERENCES bundles(id), name TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY(bundle_id, name)
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    digest TEXT NOT NULL, target TEXT NOT NULL, route TEXT NOT NULL,
    created TEXT NOT NULL, expires TEXT NOT NULL, consumed TEXT, revoked TEXT
);
CREATE TABLE IF NOT EXISTS submission_attempts (
    id TEXT PRIMARY KEY, approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals(id),
    application_id INTEGER NOT NULL REFERENCES applications(id),
    target_key TEXT NOT NULL, route TEXT NOT NULL, state TEXT NOT NULL,
    receipt TEXT, created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simulated_receipts (
    attempt TEXT PRIMARY KEY, receipt TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL,
    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available TEXT NOT NULL,
    lease_until TEXT, worker TEXT, error TEXT, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, message TEXT NOT NULL,
    link TEXT NOT NULL, created TEXT NOT NULL, seen TEXT
);
CREATE TABLE IF NOT EXISTS control (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
