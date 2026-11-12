# AI Scholarship Tracker: Phase 2 Architecture

**Revision 3.0, 10 November 2026.** Builds on `ARCHITECTURE.md` rev 2.3 (4 Nov 2026) and
*Updated_AI_Scholarship_Tracker_Architecture.pdf* rev 2.0. Nothing in 2.3 is reversed: the
trust zones, the four approval types, the single Playwright path and "tracker is the source
of truth" all still hold. This revision says **where each piece lives** in
`~/Videos/3dportfolio/ollama/phase2/` and **how the three agents hand work to each other**.

Stack: Python 3.11+ modular monolith, SQLite, Ollama (local models), OpenClaw (orchestrator
and conversation), Playwright (one browser worker), Telegram approval bot owned by the tracker,
Gmail API.

---

## 1. The 8 steps mapped to the three folders

```
                         OpenClaw orchestrator (schedules, health, re-runs)
                                        |
   search_find_validate/          validateagain_applay/             track_response/
   AGENT 1: SCOUT                 AGENT 2: APPLICANT                AGENT 3: MONITOR
   ------------------             -------------------               -----------------
   1 SEARCH                       2 RANK (re-validate + score)      8 TRACK
     registry + web search        3 APPROVE #1  "Apply?"              confirmation check
     leads -> official page       4 PREPARE  packet                   Gmail sync + classify
     extract + validate           5 FILL     browser worker           STATUS approvals
     fake check                   6 APPROVE #2  screenshots           REPLY drafts + approvals
     -> candidate accepted        7 SUBMIT + confirmation proof       deadline reminders
                \                          |                          /
                 +------- shared/ : SQLite tracker DB, vault, approvals, Ollama client ------+
```

| Step | What happens | Folder | Agent | Writes to DB | Human? |
|---|---|---|---|---|---|
| 1 SEARCH | weekly sweep of source registry + OpenClaw web search, Telegram/aggregator leads resolved to official pages | `search_find_validate` | Scout | `source`, `lead`, `candidate`, `source_fact` | only for suspicious candidates |
| (1b) VALIDATE | schema validation + fake/scam check; clean official → accepted | `search_find_validate` | Scout | `candidate.review_status = accepted`, `accepted_by = scout` | no |
| 2 RANK | **validate again** against official page (fresh fetch), eligibility class, score, shortlist | `validateagain_applay` | Applicant | `match_result`, `application(status=SHORTLISTED)` | no |
| 3 APPROVE #1 | "#12 Erasmus Mundus MSc AI, full funding, score 86, deadline 15 Jan. Apply?" | `validateagain_applay` | Applicant | `approval(type=APPLY)` | **yes, PIN** |
| 4 PREPARE | checklist, form answers, tailored CV, SOP draft, reference request drafts | `validateagain_applay` | Applicant | `packet`, `document`, `application_task` | no (drafts only) |
| 5 FILL | Playwright worker fills every page, uploads documents, stops at review page | `validateagain_applay` | Applicant (worker) | `fill_run`, screenshots, `form_state_hash` | user logs in once |
| 6 APPROVE #2 | screenshots of every page, SOP, declarations, fee; user edits/pays/ticks, then approves | `validateagain_applay` | Applicant | `approval(type=SUBMIT)` bound to form hash | **yes, PIN** |
| 7 SUBMIT | worker re-reads form, hash must match, clicks submit, saves confirmation page/PDF + reference | `validateagain_applay` | Applicant (worker) | `application(status=SUBMITTED)`, `evidence` | no |
| 8 TRACK | confirmation email check, Gmail classify → STATUS proposals, reply drafts → REPLY approvals, reminders | `track_response` | Monitor | `email_record`, `proposal`, `approval(STATUS/REPLY)`, `status_history` | **yes, PIN** per status/reply |

The folder name `validateagain_applay` is right for agent 2: a scout-accepted candidate is
still unverified, so the applicant fetches the official page again before ranking and again
before filling. Two independent checks of the deadline and eligibility before anything is sent.

---

## 2. Design rules for phase 2

1. **Three folders, one package, one database.** Each folder is an agent *module* of the same
   Python project, not a separate service. They never call each other. They hand off by
   changing a row's state in the shared SQLite DB (section 4).
2. **One shared core.** Code all three need (DB, migrations, vault, approvals, Ollama client,
   fetcher, audit, outbox) goes in a fourth folder, `shared/`. Without it, each agent would
   grow its own copy of the approval and DB code, which is exactly where bugs become security bugs.
3. **Each agent owns its states.** An agent may only move rows *out of* the states it owns
   (table in section 4). This is checked in `shared/state_machine.py`, not in prompts.
4. **Agents are deterministic pipelines with LLM steps inside.** Ollama is used for
   extraction, classification and drafting. Every LLM output is validated by Python before
   it is stored. The LLM never decides a status, an approval or a submit.
5. **OpenClaw is the conductor, not the musician.** It triggers `agent scout|applicant|monitor`
   through the allow-listed CLI, reads JSON summaries and talks to the user. It can't read
   the vault, can't see raw email or page text, can't approve, can't submit.
6. **systemd timer is the safety net.** `tracker jobs run-due` runs the same pipelines when
   OpenClaw is down or the laptop was off.

---

## 3. Folder layout

```
phase2/
  pyproject.toml                 # one package: scholarship_tracker (entry point: tracker)
  ARCHITECTURE.md                # this file
  tracker.sh                     # wrapper: activates venv, runs `tracker "$@"`
  .env.example                   # OLLAMA_HOST, model names, paths (no secrets)

  shared/                        # CORE (Zone A) - used by all three agents
    __init__.py
    config.py                    # paths, model names, thresholds, loaded from config.toml
    db/
      connection.py              # SQLite, WAL, foreign_keys=ON, busy_timeout
      migrations/                # 0001_phase1.sql, 0002_pipeline.sql, 0003_phase2_agents.sql
      repositories/              # one module per table group
    domain/
      models.py                  # dataclasses / pydantic models for every table
      state_machine.py           # allowed transitions + which agent owns each state
    security/
      vault.py                   # profile TOML, 700/600 perms, encryption at rest (todo)
      approvals.py               # create / verify / consume approvals, payload hash, PIN (scrypt)
      approval_bot.py            # tracker-owned Telegram bot (separate token from OpenClaw)
      audit.py                   # audit_log writer, correlation ids
    llm/
      ollama_client.py           # HTTP client, timeouts, retries, JSON-schema output
      schemas/                   # JSON schemas: opportunity, lead, email, sop, reply
      prompts/                   # versioned prompt templates
    fetch/
      fetcher.py                 # SSRF-safe fetch, redirects re-checked, size/time limits
      extract_text.py            # HTML/PDF -> normalised text + content hash
    events/
      outbox.py                  # event_outbox write + dispatcher
      jobs.py                    # job_run, run-due, missed-run catch-up
    cli.py                       # `tracker` root CLI; mounts each agent's sub-commands

  search_find_validate/          # AGENT 1: SCOUT (steps 1, 1b)
    __init__.py
    agent.py                     # `tracker agent scout` : one full run, returns JSON summary
    registry.py                  # source registry, tiers, check_interval, coverage report
    search/
      web_search.py              # receives URLs proposed by OpenClaw web search
      catalog_crawl.py           # Erasmus Mundus, DAAD, Chevening ... catalog pages
      telegram_reader.py         # t.me/s/<channel>, no login
      aggregator_reader.py
    leads.py                     # lead classifier: opportunity / advert / scam / self-funded / level
    resolve.py                   # lead -> official provider/university URL (or drop)
    extract.py                   # Ollama extraction -> opportunity JSON schema
    validate.py                  # types, dates, currency, URL, consistency checks
    fake_check.py                # official domain, scam signals, deadline not passed
    dedupe.py                    # same program/funding already known? -> merge or change proposal
    handoff.py                   # accept candidate (accepted_by=scout) or queue for user review
    skill/SKILL.md               # OpenClaw skill: only scout commands
    tests/

  validateagain_applay/          # AGENT 2: APPLICANT (steps 2-7)
    __init__.py
    agent.py                     # `tracker agent applicant`
    revalidate.py                # fresh fetch of the official page, diff vs candidate
    eligibility.py               # hard requirements -> Eligible / Potentially / Ineligible
    scoring.py                   # weighted score w/ versioned weights + breakdown
    shortlist.py                 # thresholds, max N per week, creates APPLY approvals
    prepare/
      packet.py                  # immutable versioned packet
      checklist.py               # required docs/tests from requirements
      answers.py                 # form answers from the vault profile
      cv.py                      # tailored CV (Ollama draft from master CV)
      sop.py                     # SOP draft (Ollama), ai_generated=true
      references.py              # reference request email drafts (never sent automatically)
    browser_worker/
      worker.py                  # separate process, own Playwright profile
      portal_session.py          # login wait: user signs in, cookie stays in profile
      fill.py                    # mapped fill + generic label mode, uploads
      snapshot.py                # screenshot every page, compute form_state_hash
      submit.py                  # re-read form, compare hash, click submit, capture confirmation
      field_mappings/            # one YAML per portal (erasmus_xyz.yaml, daad_portal.yaml ...)
    skill/SKILL.md               # OpenClaw skill: only applicant commands
    tests/

  track_response/                # AGENT 3: MONITOR (step 8 + reminders)
    __init__.py
    agent.py                     # `tracker agent monitor`
    submission_check.py          # was confirmation saved? did a confirmation email arrive?
    gmail/
      auth.py                    # OAuth loopback; readonly, `--send` adds gmail.send (user only)
      sync.py                    # history-ID incremental sync + overlap + dedupe
      classify.py                # rules first, Ollama second: admit/reject/interview/docs/other
      link.py                    # email -> application (domain, thread, reference number)
    proposals.py                 # classifier result -> STATUS approval (never direct status write)
    replies.py                   # reply drafts (template or tool-less model) -> REPLY approvals
    send.py                      # send exactly the approved text, consume approval first, no retry
    reminders.py                 # 60/30/14/7/1-day deadline + task reminders
    references_follow.py         # did referees submit? nudge user
    skill/SKILL.md               # OpenClaw skill: only monitor commands
    tests/

  orchestrator/
    skill/SKILL.md               # OpenClaw: agent status, health, schedules
  systemd/
    tracker-run-due.service  tracker-run-due.timer  tracker-approval-bot.service

  data/  vault/  logs/  backups/  evidence/      # git-ignored, 700 permissions
```

If phase 1 lives in a sibling folder (`../phase1`), move its modules into `shared/` as in
rev 2.3 section 15 (`db.py -> shared/db/`, `vault.py -> shared/security/`,
`matching.py -> validateagain_applay/eligibility.py + scoring.py`, `llm.py -> shared/llm/`).

---

## 4. Handoff contract between agents

Agents never call each other. The DB is the queue. Each row carries one state, and each
state has exactly one owner that may move it on.

### 4.1 Candidate pipeline (owned by Scout until accepted)

```
lead:       new -> resolved (official_url found) | dropped (advert/scam/self-funded/bachelor/dup)
candidate:  pending -> validated -> accepted (accepted_by = scout | user)
                                 \-> needs_user_review (suspicious) -> accepted | rejected
                                 \-> rejected
```

### 4.2 Application pipeline (owned by Applicant until SUBMITTED, then Monitor)

```
DISCOVERED ............ created when candidate accepted        (Scout hands off here)
  -> SHORTLISTED ...... eligible/potentially + score >= threshold
  -> AWAITING_APPLY ... APPLY approval sent (#1)
  -> PREPARING ........ approval #1 consumed; packet being built
  -> READY_TO_FILL .... packet frozen; waiting for user login if needed
  -> FILLING .......... worker running
  -> AWAITING_SUBMIT .. paused at review page, SUBMIT approval sent (#2)
  -> SUBMITTING ....... approval #2 consumed, hash re-checked
  -> SUBMITTED ........ confirmation saved                    (Applicant hands off here)
  -> UNDER_REVIEW -> INTERVIEW -> ADMITTED -> ACCEPTED_BY_USER | DECLINED_BY_USER
                  \-> WAITING_FOR_DOCUMENTS -> UNDER_REVIEW
                  \-> REJECTED
side exits: SKIPPED (user rejected #1), NEEDS_USER (worker stopped), EXPIRED (deadline passed),
            WITHDRAWN (user)
```

| States | Owner | Who may move it |
|---|---|---|
| lead/candidate up to `accepted` | Scout | scout, user |
| `DISCOVERED` … `SUBMITTED` | Applicant | applicant, worker, user via approvals |
| `SUBMITTED` onward | Monitor | **only** a consumed STATUS approval or the user |

`shared/domain/state_machine.py` holds this table. Every transition writes `status_history`
(old, new, actor, source, reason) and `audit_log` in the same transaction, and drops an event
into `event_outbox` so OpenClaw and the approval bot hear about it.

### 4.3 What each agent reads and writes

| Agent | Reads | Writes | Never touches |
|---|---|---|---|
| Scout | `source`, `lead`, `candidate`, known programs (dedupe) | `lead`, `candidate`, `source_fact`, `source.last_checked_at` | vault, applications, approvals |
| Applicant | accepted candidates, vault profile (in-process only), requirements | `match_result`, `application`, `packet`, `document`, `approval(APPLY, SUBMIT)`, `evidence` | Gmail, STATUS/REPLY approvals |
| Monitor | submitted applications, Gmail (bodies in memory only), tasks, deadlines | `email_record`, `proposal`, `approval(STATUS, REPLY)`, `outbox` | packets, browser worker |

---

## 5. Agent 1: Scout (`search_find_validate/`)

**Trigger:** weekly (`tracker agent scout`), plus per-source intervals from the registry
(catalogs 7 days, shortlisted universities 14 days, Telegram/aggregators 1-3 days).

```
for each due source in registry:
    fetch (shared/fetch, SSRF-safe) -> text + hash -> unchanged? skip (but re-check deadlines
                                                     of high-value items on schedule)
    if source is a lead source (Telegram, aggregator, agency):
        extract lead (Ollama, schema: name, country, level, funding_type, links)
        drop: self-funded | bachelor-only | duplicate | agency advert | scam signals
        resolve to official page (provider/university domain) or drop "no_official_page"
        continue with the official page only
    extract opportunity (Ollama, schema `opportunity.json`)
    validate (Python): required fields, ISO dates + tz, deadline in future, currency code,
                       amount numeric, URL on the source domain, degree_level == master's
    dedupe against known programs/funding -> new candidate | change proposal | no-op
    fake_check:
        official domain (allow-list + .edu/.ac.* /gov patterns, known providers)
        no scam signals (fee to receive, guaranteed admission/visa, free-mail contact only,
        pressure with no source)
        deadline not passed
        originating post not an advert
    pass -> candidate.accepted (accepted_by=scout, verified=false) -> application DISCOVERED
    fail -> needs_user_review, listed in the weekly digest
also: OpenClaw web search queries ("fully funded master's AI Canada 2027 international")
      -> URLs -> `tracker discover propose-url <url>` -> same pipeline
end: coverage report (never-checked, overdue, yield per source) + JSON summary
```

**Ollama use:** extraction and lead classification only. Prompt contains page text as data
inside delimiters, schema-constrained output (`format: json` with schema), temperature 0.
A small instruct model (7-8B) is usually enough for extraction; measure it on 20-50 labelled
pages before trusting it (rev 2.3 section 14).

**Output summary to OpenClaw (no raw text):**
`{"run_id", "sources_checked", "new_candidates", "accepted", "needs_review", "dropped_by_reason", "overdue_sources"}`

**Done when:** a weekly run on the real registry produces accepted candidates with source URL,
excerpt and fetch time, and every scam/advert test fixture is dropped.

---

## 6. Agent 2: Applicant (`validateagain_applay/`)

### 6.1 Validate again + rank (step 2)
```
for each application in DISCOVERED:
    revalidate: fresh fetch of official_url; diff key fields (deadline, funding, eligibility
                text) vs candidate; any change -> change proposal, rank uses the new value
    eligibility (rules, no LLM): nationality/residency, degree + field, GPA with grading-scale
                conversion, English test or waiver, required experience
        -> ELIGIBLE / POTENTIALLY_ELIGIBLE (unknowns -> verification tasks) / INELIGIBLE
    score (weights version w2): funding 30, academic fit 30, readiness 20, location 20,
          competitiveness 0 until real data exists
    shortlist if class != INELIGIBLE and score >= threshold (default 70) and
          days_to_deadline >= minimum prep time for this application type
```

### 6.2 Approval #1: APPLY (step 3)
Sent by the **tracker's own approval bot**, not OpenClaw:
```
#12 Erasmus Mundus - MSc Artificial Intelligence (3 universities)
Full funding: tuition + EUR 1,400/month   Score 86 (funding 30, fit 26, readiness 14, location 16)
Eligible. Unknown: none.    Deadline 15 Jan 2027 23:59 CET (verified 10 Nov from official page)
Needs: 2 references, IELTS 6.5 (you have 7.0), SOP 1000 words
[Approve] [Reject] [Details]   -> PIN
```
Payload hash binds application id, intake id, deadline and packet scope. Expires in 7 days or
at the deadline, whichever is first.

### 6.3 Prepare (step 4)
Runs automatically once approval #1 is consumed. Produces one **immutable, versioned packet**:

| Item | How | Stored as |
|---|---|---|
| Checklist | requirements table -> tasks with due dates (internal prep deadline before official) | `application_task` |
| Form answers | from vault profile, deterministic mapping (name, DOB, degrees, GPA, test scores) | `packet.answers_json` |
| Tailored CV | Ollama rewrites ordering/emphasis of master CV; facts can't be added (diff check: no new employers, dates, degrees) | `document(type=cv, ai_generated=true)` |
| SOP draft | Ollama draft from profile + program facts + user's notes; word limit enforced | `document(type=sop, ai_generated=true)` |
| Reference request drafts | template + program details, one per referee | `document(type=ref_request)` (user sends them) |
| Document selection | transcript, diploma, passport scan, test reports, by checksum | `packet.documents` |

Rule: the profile is read by the tracker process only. OpenClaw sees "packet v3 built, 6 docs,
SOP 980 words", never the content.

### 6.4 Fill (step 5)
```
worker (separate process, own Playwright profile under data/browser_profile/<portal>/)
  open portal URL from packet
  not logged in? -> "needs you: please sign in" + screenshot; wait (user logs in in that window)
  load field_mappings/<portal>.yaml (selectors -> packet keys); none? generic label mode,
       which only fills fields it matches with high confidence and reports the rest
  fill page by page, upload documents, screenshot each page
  stop at the final review page; NEVER click submit here
  anything unexpected (CAPTCHA, new page, error, unknown required field) -> stop, NEEDS_USER
  compute form_state_hash = sha256(normalised field values + uploaded file checksums + page list)
```

### 6.5 Approval #2: SUBMIT (step 6)
Bot sends: screenshots of every page, full SOP text, declaration text, fee amount. The user
may then, in the worker's browser window: edit the SOP or any field, tick declarations,
pay the fee. Then **Approve + PIN**. The approval binds `form_state_hash`. Expires in 24 h and
always before the deadline.

### 6.6 Submit (step 7)
```
consume approval #2 (single use) -> re-read the live form -> recompute hash
hash differs (user edited after screenshots, page changed) -> new screenshots, new approval #2
hash matches -> click final submit -> wait for confirmation page
save confirmation as PDF + screenshot + reference number in evidence/<application_id>/
status SUBMITTED with evidence ids -> event to Monitor
```

**Done when:** on a test portal (fixture site), fill -> screenshots -> approval #2 -> submit ->
confirmation saved, and any change after the screenshots blocks submission.

---

## 7. Agent 3: Monitor (`track_response/`)

**Trigger:** daily (`tracker agent monitor`), Gmail sync every few hours while the laptop is on.

```
submission_check: every SUBMITTED app has a confirmation file? confirmation email within 48 h?
                  missing -> task "check portal"
gmail sync (readonly, history ID, overlap + dedupe by gmail_id)
  for each new message: classify (rules on from-domain/subject first, Ollama second)
     -> link to application (thread id, reference number, university domain)
     -> store metadata only; body discarded after extraction
     category admit/reject/interview/docs_requested -> proposal -> STATUS approval (bot + PIN)
     needs an answer -> reply draft (template or tool-less model, <=2000 chars, no links,
                        no payment/bank/password words) -> REPLY approval (bot + PIN)
                        -> send exactly that text from Gmail, consume first, never retry
reminders: 60/30/14/7/1 days per deadline, missed-run catch-up via job_run
references: referee submitted? if not by 10 days before deadline -> nudge user
weekly digest: new candidates, shortlist, waiting approvals, upcoming deadlines,
               status changes, blind spots ("email 3 professors in X")
```

The classifier can **propose** ADMITTED or REJECTED but never write them. Only a consumed
STATUS approval changes the status.

---

## 8. Approval gates (all four)

| Gate | Agent | Bound to (payload hash) | Expiry | Effect when consumed |
|---|---|---|---|---|
| APPLY #1 | Applicant | application, intake, deadline, packet scope | 7 days / deadline | PREPARING |
| SUBMIT #2 | Applicant | form_state_hash, packet version, document checksums | 24 h / deadline | SUBMITTING |
| STATUS | Monitor | application, proposed status, email id | 7 days | status change + history |
| REPLY | Monitor | reply id, recipient, subject, body sha256, thread | 3 days | send one email |

Verification in `shared/security/approvals.py`: pending, hash matches, not expired, not
consumed, transition allowed, PIN correct (scrypt), message from the user's Telegram chat id.
Action + `consumed_at` + `status_history` + `audit_log` in one transaction. `approve` is not
on any OpenClaw allow-list and needs a TTY.

---

## 9. Data model additions (migration `0003_phase2_agents.sql`)

Rev 2.3 tables stay. Phase 2 adds:

```
requirement     id, target_type, target_id, requirement_type, operator, required_value,
                unit, is_mandatory, source_id, verified_at           -- was columns before
match_result    id, application_id, weights_version, eligibility_class, score,
                breakdown_json, unknowns_json, next_action, computed_at
packet          id, application_id, version, answers_json, documents_json, created_at,
                frozen_at, checksum                                  -- immutable after freeze
fill_run        id, application_id, packet_id, portal, started_at, finished_at, outcome,
                pages_json, form_state_hash, problem
evidence        id, application_id, kind (screenshot|confirmation_pdf|reference_no),
                path, sha256, created_at
agent_run       id, agent (scout|applicant|monitor), job_run_id, started_at, finished_at,
                outcome, summary_json, correlation_id
reply_draft     id, email_record_id, recipient, subject, body_sha256, body_enc, status
```

Columns added: `candidate.accepted_by`, `application.owner_agent` (derived from status, kept
for quick queries), `source.tier`, `source.check_interval_days`, `source.last_checked_at`.

---

## 10. Local models (Ollama)

| Task | Agent | Model size | Output | Validation |
|---|---|---|---|---|
| Lead classification | Scout | small (3-8B) | enum + reasons | enum check |
| Opportunity extraction | Scout | 7-8B instruct | `opportunity.json` | schema + date/currency/URL rules |
| Change detection summary | Applicant | small | diff text | for display only |
| CV tailoring | Applicant | 7-14B | markdown | no new facts (entity diff vs master CV) |
| SOP draft | Applicant | best local model you can run (14B+ if RAM allows) | text | word limit, user edits in #2 |
| Email classification | Monitor | small | category + confidence | rules first, model second |
| Reply draft | Monitor | small, tool-less | text | length, no links, banned words, template fallback |

All calls go through `shared/llm/ollama_client.py`: timeouts, one retry, temperature 0 for
extraction, prompt versions recorded with each output. Model names live in `config.toml`, so
you can swap them after measuring.

---

## 11. Orchestration and schedules

| Job | Command | When | Runs without OpenClaw? |
|---|---|---|---|
| Scout sweep | `agent scout` | weekly (Sun) + per-source intervals | yes (systemd timer) |
| Applicant pass | `agent applicant` | daily, and after each scout run | yes |
| Fill | `agent fill <id>` | after approval #1 + packet frozen; needs user present for login | started by user or bot button |
| Monitor | `agent monitor` | daily; Gmail sync every 4 h | yes |
| Reminders / outbox | `jobs run-due`, `events dispatch` | at boot + daily (Persistent=true) | yes |
| Health | `health` | daily, shown in digest | yes |

OpenClaw's role: run these on its own routines when it's up, answer "what's happening?" with
`agent status`, and relay digests. If it's down, the timer runs the same commands, and the
approval bot still delivers approvals because it's a separate tracker-owned service.

---

## 12. Security boundaries (unchanged from 2.3, mapped to folders)

| Zone | Folder(s) | Note |
|---|---|---|
| A private core | `shared/`, plus agent code running inside the tracker process | only place the vault is decrypted |
| B agent | OpenClaw + each `skill/SKILL.md` | allow-list per agent: scout skill can't run applicant commands, etc. |
| C untrusted | anything fetched by `shared/fetch`, Gmail bodies, portal pages | always data inside delimiters; never reaches OpenClaw |
| D browser worker | `validateagain_applay/browser_worker/` | separate process + profile; submit only with valid #2 |

---

## 13. Failure handling

| Failure | Behaviour |
|---|---|
| Source fetch fails | retry next interval; after 3 failures mark source `failing` in coverage report |
| Extraction invalid | candidate `invalid` with reason; never accepted |
| Deadline changed on re-validate | change proposal; pending APPLY approval invalidated (hash mismatch) and re-issued |
| Worker hits CAPTCHA/unknown page | stop, NEEDS_USER with screenshot; resume from the same page later |
| Approval expires | status back one step (e.g. AWAITING_SUBMIT -> READY_TO_FILL), user told |
| Deadline < 24 h and not approved | EXPIRED warning at 24 h; never auto-submit |
| Gmail token expired | Monitor stops mail steps, reports in health; other steps still run |
| Ollama down | agents finish non-LLM steps, queue the rest, report in health |
| Laptop off | `jobs run-due` catch-up from last successful `job_run` |

---

## 14. Build order for phase 2

Each step is useful the day it ships. Fall 2027 deadlines run Nov 2026 to Feb 2027, so keep
applying by hand in parallel.

| # | Build | Folder | Done when |
|---|---|---|---|
| 1 | Package skeleton, `shared/` (move phase 1 code), migration `0003`, state machine + tests | `shared/` | phase 1 data migrates; all transitions tested |
| 2 | Scout: registry, fetch, extract, validate, fake check, handoff | `search_find_validate/` | weekly run yields accepted candidates; scam fixtures dropped |
| 3 | Applicant ranking: revalidate, eligibility, scoring, shortlist | `validateagain_applay/` | each shortlisted item shows class, breakdown, unknowns |
| 4 | Approval bot + APPLY gate | `shared/security/` + applicant | approve on phone with PIN; replay/expired tests pass |
| 5 | Prepare: packet, checklist, answers, CV, SOP, reference drafts | `validateagain_applay/prepare/` | packet reviewable and regenerable with history |
| 6 | Browser worker on a fixture portal: fill, screenshots, hash, SUBMIT gate, submit, evidence | `validateagain_applay/browser_worker/` | change after screenshots blocks submit |
| 7 | First real portal mapping (the one with the nearest deadline) | `field_mappings/` | one real application submitted end to end |
| 8 | Monitor: submission check, Gmail sync/classify, STATUS + REPLY gates, reminders | `track_response/` | a real email gives a proposal; no status change without approval |
| 9 | OpenClaw skills per agent + orchestrator status, systemd units | `*/skill/`, `systemd/` | agents run on schedule with OpenClaw on and off |
| 10 | Hardening: vault encryption, extraction eval, backup restore test | all | security tests pass; restore succeeds |

---

## 15. Open questions

- Where is the phase 1 code (`../phase1`?) and should phase 2 import it or absorb it into `shared/`?
- Which Ollama models are installed, and how much RAM/VRAM does the laptop have? This decides
  the SOP model.
- Which portal has the nearest real deadline? Its field mapping should be the first one built.
- Which OpenClaw version is installed, and what is its per-skill tool allow-list syntax?
