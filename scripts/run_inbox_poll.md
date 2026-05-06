# Ari — Inbox Poll Runbook

This is the prompt the scheduled task uses each time it wakes up. It assumes:

- The Gmail MCP is authenticated for `244downeyapt3@gmail.com` (the tenant inbox).
- The Ari project is at `/Users/shawmckean/Library/Application Support/Claude/local-agent-mode-sessions/c2b52b70-07ff-4e51-bbe9-06d85fbb65aa/bbccdb81-323d-4804-8bf4-8c153348d986/local_9a736697-c39a-4bd5-a778-5dee8c3fd04e/outputs/ari` (replace once stable).
- A `last_poll.json` file at `ari/state/last_poll.json` tracks the last seen thread/timestamp.

## What the task does each run

1. **Read state.** Load `ari/state/last_poll.json` (or create with `last_seen_iso` = now if missing).
2. **Pull new threads.** `search_threads` with query `newer_than:1d AND is:unread`, filter to threads where the latest message arrived after `last_seen_iso`.
3. **For each new thread:**
   - Call `get_thread` to fetch the full message body.
   - Build the email JSON (thread_id, from, to, date, subject, body, attachments).
   - Run `python3 ari/scripts/ari_pipeline.py <email.json>`.
   - Take the resulting draft package and create a Gmail draft on the original thread (so Shaw sees it threaded), using `create_draft`.
   - Send Shaw a cover-note email at `mckean.shaw@gmail.com` with the cover note's subject (which carries the urgency flag) and a body containing: the cover-note summary, suggested action, decisions needed, and a link / quote of the drafted reply.
4. **Update state.** Write the latest seen timestamp into `ari/state/last_poll.json`.
5. **Emergency path.** If any classification returns `urgency == "emergency"`, also send Shaw an SMS/push (TBD — for v0, the `[URGENT — emergency]` subject prefix on the cover note is the signal; phone path is a later enhancement).

## Hard rules every run

- **Never `send_message`** on the tenant inbox. Only `create_draft`. Sending requires Shaw's explicit chat approval.
- **Never contact Gilberto directly.** Vendor dispatch only happens after Shaw approves a "dispatch Gilberto" reply.
- **Never reference AB 1482, CPI, or state caps in a tenant-facing draft.**
- **Skip threads where Shaw is the sender** (those are owner-initiated; let them be unless the tenant replies).
- **If the pipeline raises** for any reason: do NOT silent-fail. Email Shaw with subject `[URGENT] Ari pipeline error` and the traceback.

## Output for the run

End the run with a one-line summary: `<n> new threads processed, <m> emergency, <k> errors. Drafts written to ari/drafts/.`
