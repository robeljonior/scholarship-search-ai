---
name: scholarship-orchestrator
description: Orchestrator. Schedules the three agents, answers "what's happening?", and reports health.
---
# Orchestrator

Allowed commands (always `--json`):
- `./tracker.sh --json agent status`
- `./tracker.sh --json health`
- `./tracker.sh --json jobs run-due` : same as the systemd timer (scout if due, applicant, monitor, notifications)

Schedule: scout weekly (Sunday), applicant daily, monitor every 4 hours while the laptop is on.
If a job failed, report its error from `agent status`; do not retry more than once.
Never call: approve, approvals set-pin, bot setup, mail auth, mail reply-edit, worker run (user-only).
