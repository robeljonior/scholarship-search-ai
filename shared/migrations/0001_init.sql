-- Phase 2 schema. One SQLite database is the handoff between the three agents.

CREATE TABLE source (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    name TEXT,
    tier TEXT NOT NULL DEFAULT 'web',          -- catalog | university | country | web | lead
    source_type TEXT NOT NULL DEFAULT 'official', -- official | aggregator | lead
    check_interval_days INTEGER NOT NULL DEFAULT 7,
    last_checked_at TEXT,
    content_hash TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',     -- active | failing | disabled
    opportunities_found INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE lead (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES source(id),
    post_url TEXT,
    excerpt TEXT,
    classification TEXT NOT NULL,              -- opportunity | advert | scam_suspect | self_funded | wrong_level | irrelevant
    funding_type TEXT,
    official_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',        -- new | resolved | dropped
    drop_reason TEXT,
    content_hash TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE candidate (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES source(id),
    lead_id INTEGER REFERENCES lead(id),
    url TEXT NOT NULL,
    extracted_json TEXT NOT NULL,
    excerpt TEXT,
    model TEXT,
    content_hash TEXT,
    fetched_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | needs_user_review | rejected | invalid | duplicate
    review_reasons TEXT,
    accepted_by TEXT,                          -- scout | user
    opportunity_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE opportunity (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER REFERENCES candidate(id),
    name TEXT NOT NULL,
    provider TEXT,
    university TEXT,
    country TEXT,
    region TEXT,
    degree_level TEXT,
    field TEXT,
    funding_type TEXT,                         -- full | partial | unknown
    coverage TEXT,
    amount REAL,
    currency TEXT,
    deadline TEXT,                             -- ISO date or datetime
    deadline_tz TEXT,
    official_url TEXT NOT NULL UNIQUE,
    official_domain TEXT NOT NULL,
    requirements_json TEXT NOT NULL DEFAULT '{}',
    verified_at TEXT,                          -- set by user accept or by applicant re-validation
    verified_by TEXT,
    rechecked_at TEXT,                         -- applicant re-fetched the official page and it still matched
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE application (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL UNIQUE REFERENCES opportunity(id),
    status TEXT NOT NULL,
    eligibility_class TEXT,
    score REAL,
    weights_version TEXT,
    breakdown_json TEXT,
    reasons_json TEXT,
    unknowns_json TEXT,
    next_action TEXT,
    portal_url TEXT,
    reference_number TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE packet (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id),
    version INTEGER NOT NULL,
    answers_json TEXT NOT NULL,
    checklist_json TEXT NOT NULL,
    documents_json TEXT NOT NULL,
    cv_text TEXT,
    sop_text TEXT,
    reference_drafts_json TEXT,
    checksum TEXT NOT NULL,
    dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (application_id, version)
);

CREATE TABLE fill_run (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id),
    packet_id INTEGER NOT NULL REFERENCES packet(id),
    mode TEXT NOT NULL,                        -- simulate | playwright
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,                              -- paused_for_review | needs_user | submitted | blocked
    pages_json TEXT,
    form_values_json TEXT,
    form_state_hash TEXT,
    problem TEXT
);

CREATE TABLE evidence (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id),
    kind TEXT NOT NULL,                        -- screenshot | confirmation | email
    path TEXT,
    note TEXT,
    sha256 TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE approval (
    id INTEGER PRIMARY KEY,
    action_type TEXT NOT NULL,                 -- APPLY | SUBMIT | STATUS | REPLY
    resource_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | expired | invalidated
    decided_at TEXT,
    decided_via TEXT,
    consumed_at TEXT,
    notified_at TEXT
);
CREATE INDEX approval_pending ON approval(decision, action_type);

CREATE TABLE status_history (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT,
    reason TEXT,
    changed_at TEXT NOT NULL
);

CREATE TABLE task (
    id INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES application(id),
    kind TEXT NOT NULL,                        -- document | test | reference | verify | portal | other
    title TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'TODO',       -- TODO | IN_PROGRESS | BLOCKED | DONE | CANCELLED
    created_at TEXT NOT NULL,
    UNIQUE (application_id, title)
);

CREATE TABLE email_record (
    id INTEGER PRIMARY KEY,
    gmail_id TEXT NOT NULL UNIQUE,
    thread_id TEXT,
    message_id_header TEXT,
    received_at TEXT,
    from_addr TEXT,
    from_domain TEXT,
    reply_to TEXT,
    subject TEXT,
    category TEXT,                             -- confirmation | admit | reject | interview | docs_requested | other
    confidence REAL,
    application_id INTEGER REFERENCES application(id),
    suspicious INTEGER NOT NULL DEFAULT 0,
    handled_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE reply_draft (
    id INTEGER PRIMARY KEY,
    email_record_id INTEGER NOT NULL REFERENCES email_record(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    in_reply_to TEXT,
    thread_id TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_approval', -- awaiting_approval | sent | rejected | failed
    created_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT
);

CREATE TABLE reminder_sent (
    application_id INTEGER NOT NULL,
    threshold_days INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (application_id, threshold_days)
);

CREATE TABLE event_outbox (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    outcome TEXT,
    detail TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE job_run (
    id INTEGER PRIMARY KEY,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    summary_json TEXT
);

CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
