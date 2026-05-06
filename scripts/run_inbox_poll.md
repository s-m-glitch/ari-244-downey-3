# Ari — Inbox Poll Runbook (Gmail API edition)

**Production loop** is `run_poll.py`, a standalone Python script. Wire it up via `cron`, `launchd`, GitHub Actions, or the Cowork scheduler — whatever you prefer. The loop is identical regardless of the runner.

## Each invocation

```bash
python3 scripts/run_poll.py
```

Steps it executes:

1. **Load `state/last_poll.json`.** Creates with `last_seen_iso = now - 1h` if missing.
2. **List new threads.** Default query: `is:unread newer_than:1d -from:me`. Override with `--query`.
3. **For each thread:**
   - `gmail_client.get_thread()` — fetch the full thread.
   - `gmail_client.parse_message()` on the latest message → produces the email JSON shape `ari_pipeline.process_email()` expects.
   - Persist inbound to `drafts/inbound/<thread_id>.json` (audit trail).
   - Run `ari_pipeline.process_email(email)` → classification + draft + cover note + state updates.
   - **Skip** if sender contains `mckean.shaw@gmail.com` (don't auto-respond on owner-outbound).
   - `gmail_client.create_draft(thread_id, ...)` — drops Ari's reply into `244downeyapt3@`'s drafts folder. **Never sends.**
   - `gmail_client.send_email(mckean.shaw@gmail.com, ...)` — emails Shaw the cover note containing classification, suggested action, decisions needed, and the drafted reply quoted in full. Subject carries the urgency flag (`[URGENT — emergency]` / `[URGENT]` / `[normal]`).
4. **Update `state/last_poll.json`** with the new timestamp.
5. **Append to `logs/poll.jsonl`** with run summary.
6. **Print a one-liner** to stdout: `<n> processed, <m> emergency, <k> errors.`

## Hard rules every run

- **Never `send_email` on the tenant inbox.** Only `create_draft`. Sending requires Shaw to send manually from the drafts folder, OR Shaw to reply "send" to the cover note (vN — not in v0).
- **Never contact Gilberto directly.** Vendor dispatch only happens after Shaw approves a "dispatch Gilberto" reply (vN).
- **Never reference AB 1482, CPI, or state caps in tenant-facing drafts.** Enforced in pipeline, but worth restating.
- **Skip threads where Shaw is the sender.**
- **Errors fail loud.** Any exception in the loop emails Shaw with `[URGENT] Ari pipeline error` + traceback. Do NOT silent-fail.

## Scheduling — pick one

**Cowork scheduled task** (already exists, named `ari-inbox-poll`):
The task calls `python3 <ari root>/scripts/run_poll.py`. Tied to your Cowork session.

**Mac `launchd`** (true headless when Mac is awake):
See `docs/gmail_api_setup.md` § "Wiring the schedule" for the plist.

**GitHub Actions** (cloud, free): a 30-min cron workflow that runs `run_poll.py`. Token goes in repo secrets. Best fit if you want it running when your laptop's closed.

## Dry-run before going live

```bash
python3 scripts/run_poll.py --dry-run
```

Same thread-fetching and pipeline run, but no drafts created and no cover notes sent. Inspect `drafts/<timestamp>__*.json` packages to validate classification before flipping to live.
