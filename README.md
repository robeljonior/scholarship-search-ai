# AI Scholarship Tracker: phase 2

Three agents on one shared core, as described in `PHASE2_ARCHITECTURE.md`.

| Folder | Agent | Steps |
|---|---|---|
| `search_find_validate/` | Scout | 1 SEARCH: source registry, Telegram/aggregator leads -> official page, extraction, validation, fake check, handoff |
| `validateagain_applay/` | Applicant | 2 RANK (validate again + eligibility + score), 3 APPROVE #1, 4 PREPARE, 5 FILL, 6 APPROVE #2, 7 SUBMIT |
| `track_response/` | Monitor | 8 TRACK: confirmation check, Gmail, STATUS/REPLY approvals, deadline reminders |
| `shared/` | core | SQLite + migrations, state machine, approvals (PIN, hash, expiry, single use), vault, safe fetcher, Ollama client, jobs/outbox, Telegram approval bot, CLI |
| `app/` | web UI | local app on 127.0.0.1 + a fake demo portal for browser tests |
| `demo/` | test data | fixture pages, demo inbox |

The core needs only Python 3.10+ (no pip installs). Ollama, Playwright and the Gmail libraries are optional.

## 1. Try it now (offline demo, about 5 minutes)

```bash
cd ~/Videos/3dportfolio/ollama/phase2
./demo.sh
```
Open the URL it prints (`http://127.0.0.1:8765/?token=...`). Demo PIN: `1234`. Then click through:

1. **Overview -> "1 Scout: search all sources"**. 5 opportunities accepted, 2 stopped by the fake check
   (an aggregator page and an expired deadline), Telegram advert/scam/self-funded posts dropped.
   See them under **1 Scout**.
2. **"2-4 Applicant"**. Each one is re-fetched, checked for eligibility and scored. The top ones get an APPLY approval.
3. **2-7 Applicant -> click the Erasmus AI row -> Approve with PIN**. The packet (answers, checklist, CV,
   SOP draft, reference emails) is built automatically.
4. **"5 Fill (simulated portal)"**. Screenshots of every page plus a SUBMIT approval (#2).
   Try "Change field" before approving: submit is then blocked and a new approval #2 is requested.
5. **Approve #2 -> "7 Submit"**. The confirmation page and reference number are saved as evidence.
6. **Overview -> "8 Monitor"**. The demo inbox gives a confirmation, an interview invitation (STATUS
   proposal + reply draft) and a phishing email (flagged, not linked). Approve the STATUS and REPLY.

Without Ollama running, extraction and drafting use built-in rules (the header shows "LLM: rules only").
With Ollama running (`ollama serve` and `ollama pull qwen2.5:7b-instruct qwen2.5:3b-instruct`), the
local models are used and every output is still validated.

To test the **real browser worker** on the built-in demo portal:
```bash
python3 -m pip install playwright && python3 -m playwright install chromium
```
Then, on an application in READY_TO_FILL, set its portal URL to `http://127.0.0.1:8765/demo-portal/start`
(`TRACKER_HOME=$PWD/data-demo ./tracker.sh application portal <id> http://127.0.0.1:8765/demo-portal/start`)
and click "Fill with real browser". A Chromium window fills the form and waits for approval #2. You tick the
declaration in that window, approve in the app, and the worker submits.

## 2. Real use

```bash
./tracker.sh init                      # creates ./data, the vault and 13 seeded official sources
nano data/vault/profile.json           # your real profile (stays local, chmod 600)
cp ~/your-docs/*.pdf data/vault/documents/   # match the paths in profile.json
./tracker.sh approvals set-pin         # approval PIN (terminal only)
./tracker.sh bot setup                 # optional: the tracker's OWN Telegram bot (not OpenClaw's)
./tracker.sh app                       # UI (no approving here; approvals via bot or terminal)
./tracker.sh approve <id>              # approve in the terminal with your PIN
```
Gmail: `python3 -m pip install google-api-python-client google-auth-oauthlib`, put your OAuth desktop client
JSON at `data/vault/gmail_client_secret.json`, then run `./tracker.sh mail auth` (add `--send` to allow approved replies).

Scheduling (survives laptop off):
```bash
mkdir -p ~/.config/systemd/user && cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tracker-run-due.timer tracker-approval-bot.service
```
OpenClaw: give each agent only its skill in `skills/<agent>/SKILL.md`. The commands they list are all it may run.
`approve`, `approvals set-pin`, `bot setup`, `mail auth`, `mail reply-edit` and `worker run` refuse to run without a terminal.

Real portals: copy `validateagain_applay/field_mappings/demo_portal.json`, set `domain` and the selectors
for that portal, and test with `./tracker.sh worker run <id>`. Without a mapping the worker stops with "needs you". It never guesses.

## 3. Safety rules enforced in code

- **Approvals** need the PIN, are single use and expire. Each one is bound to a hash of what you saw. If the
  deadline, form values, status or reply text changed since, the approval is invalidated.
- **Submit** happens only in SUBMITTING (approval #2 consumed) and only if the live form still matches the approved hash.
- The worker never ticks declarations, never pays and never creates accounts. CAPTCHAs and unknown pages make it stop.
- **Status** after submission changes only through a STATUS approval. The email classifier only proposes.
- **Replies** are at most 2000 characters with no links and no payment, bank or password words. One approval sends one email, with no retries.
- The **fetcher** blocks non-http(s) URLs, localhost, private IPs and metadata IPs. It re-checks every redirect and enforces size and time limits.
- **Leads** (Telegram, aggregators) are never evidence. They must resolve to an official page.
- Email bodies are never stored, and the agents only see summaries.

## 4. Tests

```bash
python3 -m unittest discover -s tests -t . -v
```
These cover the scout pipeline, ranking, the full 8-step flow with a tampered form, approval PIN lockout,
replay, expiry, wrong Telegram chat, forbidden transitions, SSRF blocks, extraction, scam signals, reply
checks and app auth.

## 5. Not done yet

- Vault encryption at rest (use full-disk encryption meanwhile)
- Field mappings for real portals (written per portal)
- Gmail history-ID sync (it currently uses a time window with overlap and dedupe)
- An extraction accuracy evaluation on 20-50 labelled real pages
- `program`/`intake` split from the rev 2.3 data model (phase 2 uses one `opportunity` table)
