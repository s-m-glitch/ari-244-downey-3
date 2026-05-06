# Ari — v0 Prototype

Tenant Inbox Agent for 244 Downey St, Unit 3. Built to the spec dated 2026-05-05.

## What runs end-to-end today

Drop an inbound email JSON into the pipeline, get back: a classification, a draft to the tenant, a cover note to Shaw with subject-line urgency flag, and the appropriate state updates (ticket / ledger / HOA log). Nothing is sent — every reply lives as a draft awaiting Shaw's approval, per §5.

```
python3 scripts/ari_pipeline.py tests/fixtures/email_blinds.json
```

For live operation against `244downeyapt3@gmail.com`, the production loop is `scripts/run_poll.py`, which talks directly to the Gmail API. One-time setup (download OAuth credentials from Google Cloud, run `auth_setup.py`) is documented in [`docs/gmail_api_setup.md`](docs/gmail_api_setup.md). After that:

```
python3 scripts/run_poll.py --dry-run    # see what it'd do
python3 scripts/run_poll.py              # go live
```

Wire that to cron / launchd / GitHub Actions / Cowork's scheduler — your call.

## Layout

```
ari/
├── README.md                       — this file
├── prompts/
│   └── system_prompt.md            — Ari's persona + policy contract; the brain
├── kb/
│   ├── property.json               — ground-truth facts (parties, rent, deposit, vendor, consent routing, etc.)
│   ├── lease_raw.txt               — full extracted lease text (2,667 lines, searchable)
│   └── ccrs_ocr.txt                — partial OCR of the CC&Rs (scanned PDF; spec encodes the operationally-relevant rules)
├── state/
│   ├── tickets.json                — maintenance tickets (Open → Owner reviewing → Vendor scheduled → In progress → Resolved)
│   ├── payment_ledger.json         — month-by-month rent receipts, updated when Shaw confirms via email
│   ├── permissions.json            — anything Shaw has explicitly granted (one-time exceptions, expiry-tracked)
│   ├── rent_calendar.json          — last increase, next eligible date (10/1/2026), comp-pull trigger (~8/1/2026)
│   ├── hoa_log.json                — HOA notices received, deadlines, tenant-impact flag
│   └── tenant_context.json         — Mareika's history, communication patterns, settled matters (Bones is settled)
├── scripts/
│   ├── ari_pipeline.py             — the pipeline: classify → draft → cover note → state updates
│   ├── llm_drafter.py              — Claude-backed draft generation (default)
│   ├── gmail_client.py             — thin Gmail API wrapper (auth refresh, list/get/draft/send)
│   ├── auth_setup.py               — one-time OAuth consent flow; saves refresh token
│   ├── run_poll.py                 — production poll loop (calls pipeline + Gmail API)
│   └── run_inbox_poll.md           — operational runbook
├── docs/
│   ├── gmail_api_setup.md          — Google Cloud / Gmail API walkthrough
│   └── llm_drafting_setup.md       — Anthropic API key setup + cost expectations
├── tests/fixtures/                 — 7 test emails covering every §6 category
└── drafts/                         — generated draft packages (.json) and human-readable .eml.txt files
└── logs/pipeline.log.jsonl         — append-only run log
```

## Test coverage

Seven fixtures cover the whole §6 surface:

| Fixture | Category | Subcategory | Urgency | Cover-note flag |
|---|---|---|---|---|
| `email_blinds.json` | maintenance | routine | routine | `[normal]` |
| `email_mail.json` | general | informational | n/a | `[normal]` |
| `email_emergency_leak.json` | maintenance | emergency | emergency | `[URGENT — emergency]` |
| `email_window_ac.json` | policy_question | consent_required | routine | `[normal]` |
| `email_payment_delay.json` | payment | tenant_initiated_delay | urgent | `[URGENT]` |
| `email_hoa_water_shutoff.json` | hoa_correspondence | building_notice | routine | `[normal]` |
| `email_ai_question.json` | (general) + disclosure override | informational | n/a | `[normal]` |

Outputs match the §8 voice anchors verbatim for the two anchor cases (blinds, mail), and the emergency / payment / policy / HOA / disclosure paths produce drafts in the same register.

Side effects, verified in state files after the run:
- Ticket #1 (blinds, routine) and #2 (leak, emergency) opened with photo refs.
- Ledger entry for Dec 2025 annotated with the tenant's delay flag.
- HOA notice #1 logged with `tenant_impact: true` and a relayable summary drafted to Mareika.
- AI-disclosure path: triggered correctly when the tenant asks "are you a real person or an AI?" — body uses the §8 disclosure template, `ai_disclosure_triggered: true` flag set.

## Hard guardrails (enforced in code + prompt)

- Pipeline never sends. Every draft carries `send_only_after: shaw_approval`.
- Vendor (Gilberto) is never contacted until Shaw approves a "dispatch Gilberto" reply.
- Tenant-facing copy never references AB 1482, CPI, or state rent caps.
- Bones is treated as a settled accommodation; the support-animal/CC&R conflict is never re-raised.
- Anything Ari can't confidently classify routes as `escalation_only` with `[URGENT]` flag.
- Legal-threat / move-out / habitability language → `escalation_only`, no tenant-facing draft.

## v1 → vN

Drafting is now LLM-backed by default — Claude reads each inbound and writes the reply using `prompts/system_prompt.md` as guidance. Classification stays deterministic (fast and predictable). If the API call fails, the pipeline falls back transparently to the v0 templates. See `docs/llm_drafting_setup.md` for the API key setup.

What's deferred per §12: auto-send for any category, vendor dispatch automation, move-out / turnover, multi-property, dashboard surfaces. Drafts in email are the only owner surface in v1.

## Open items (per §14, awaiting Shaw)

- Gilberto's contact info + standing expense pre-approval threshold.
- Confirmation that the HOA head and other 3 tenants have been notified of the new inbox.
- Run the one-time Gmail API setup (`docs/gmail_api_setup.md`) so `run_poll.py` can hit `244downeyapt3@gmail.com` directly.

## Running it

```bash
cd <ari root>

# Single email
python3 scripts/ari_pipeline.py tests/fixtures/email_blinds.json

# Whole batch
python3 scripts/ari_pipeline.py tests/fixtures/*.json

# Inspect the most recent draft package
ls -t drafts/*.json | head -1 | xargs cat | jq .

# Inspect a draft as plain email
ls -t drafts/*.eml.txt | head -1 | xargs cat
```

Scheduled task `ari-inbox-poll` runs every 30 minutes once the inbox is connected.
