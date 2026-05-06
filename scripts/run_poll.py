#!/usr/bin/env python3
"""
Ari poll loop — production entrypoint.

Each invocation:
  1. Loads state/last_poll.json (creates if missing).
  2. Lists new tenant threads via Gmail API.
  3. For each thread: parses the latest inbound message, runs ari_pipeline.py,
     creates a Gmail draft on the thread (NEVER sends it), and emails Shaw the
     cover note with the drafted reply quoted.
  4. Updates state/last_poll.json with the latest seen message timestamp.

Wire this up however you like:
  - cron / launchd     (recommended for headless)
  - GitHub Actions     (cloud, free tier covers our cadence)
  - Cowork scheduled task (calls this script via bash)

Usage:
  python3 scripts/run_poll.py [--dry-run]
"""

from __future__ import annotations
import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import traceback

# Add scripts/ to path so we can import gmail_client and ari_pipeline
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import gmail_client  # noqa: E402
import ari_pipeline  # noqa: E402

ARI_ROOT = SCRIPTS_DIR.parent
STATE_DIR = ARI_ROOT / "state"
DRAFTS_DIR = ARI_ROOT / "drafts"
INBOUND_DIR = DRAFTS_DIR / "inbound"
LAST_POLL_PATH = STATE_DIR / "last_poll.json"
LOG_PATH = ARI_ROOT / "logs" / "poll.jsonl"

DRAFTS_DIR.mkdir(exist_ok=True, parents=True)
INBOUND_DIR.mkdir(exist_ok=True, parents=True)
LOG_PATH.parent.mkdir(exist_ok=True, parents=True)

OWNER_EMAIL = "mckean.shaw@gmail.com"
TENANT_INBOX = "244downeyapt3@gmail.com"

# Senders we should NEVER auto-respond to. Currently only the agent's own
# address — to break feedback loops where Ari sees its own draft as inbound.
# Shaw's address is intentionally NOT here: if he emails the tenant inbox
# (e.g., "got Mareika's check for May"), that's a real instruction to Ari, not
# something to ignore. v1 can add a smarter "Shaw is corresponding with the
# tenant directly in this thread" check.
SKIP_SENDERS = [
    TENANT_INBOX.lower(),
]
# Common automated-mail patterns we don't want to flag urgent to Shaw.
SYSTEM_SENDER_PATTERNS = [
    "no-reply@", "noreply@", "no_reply@",
    "notifications@", "notification@",
    "mailer-daemon@", "postmaster@",
    "google.com>",  # google account notifications
    "googlemail.com>",
]


def _load_last_poll() -> dict:
    if LAST_POLL_PATH.exists():
        return json.loads(LAST_POLL_PATH.read_text())
    return {"last_seen_iso": (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).isoformat() + "Z"}


def _save_last_poll(state: dict) -> None:
    LAST_POLL_PATH.write_text(json.dumps(state, indent=2))


def _build_cover_email(email: dict, package: dict) -> tuple[str, str]:
    cover = package["cover_note_to_shaw"]
    classif = package["classification"]
    draft = package["draft_to_tenant"]

    subject = cover["subject"]
    body_lines = [
        f"INBOUND from {email['from']}",
        f"Subject: {email['subject']}",
        f"Received: {email['date']}",
        "",
        f"CLASSIFICATION: {classif['category']} / {classif['subcategory']} ({classif['urgency']})",
        f"Rationale: {classif['rationale']}",
        "",
        "SUGGESTED ACTION:",
        f"  {cover['suggested_action']}",
        "",
        "DECISIONS NEEDED:",
        *[f"  - {d}" for d in cover["decisions_needed"]],
        "",
        "POLICY REFS: " + ", ".join(cover.get("policy_refs", []) or ["—"]),
        "",
        "─" * 60,
        f"DRAFT REPLY (created in {TENANT_INBOX} drafts, not sent):",
        f"To: {draft['to']}",
        f"Subject: {draft['subject']}",
        "",
        draft["body"],
        "─" * 60,
        "",
        "To send this draft:",
        f"  Open {TENANT_INBOX} in webmail/app, edit if needed, hit send.",
        "",
        "If you want Ari to dispatch Gilberto, reply here with 'dispatch Gilberto'.",
        "If you want to escalate, reply 'escalate' and I'll stand down on this thread.",
    ]
    return subject, "\n".join(body_lines)


def process_thread(thread_summary: dict, dry_run: bool = False) -> dict:
    thread = gmail_client.get_thread(thread_summary["id"])
    msg = gmail_client.latest_message(thread)
    email = gmail_client.parse_message(msg)
    sender_lc = email["from"].lower()

    # Break feedback loops: skip if latest message is from us or from Shaw.
    for skip in SKIP_SENDERS:
        if skip in sender_lc:
            _mark_thread_read(thread_summary["id"])
            return {"thread_id": email["thread_id"], "skipped": f"from_{skip}"}

    # Skip system / automated mail (Google security alerts, calendar invites,
    # mailer-daemon bounces, etc). They're not tenant correspondence and
    # shouldn't surface as urgent to Shaw.
    if any(p in sender_lc for p in SYSTEM_SENDER_PATTERNS):
        _mark_thread_read(thread_summary["id"])
        return {"thread_id": email["thread_id"], "skipped": "system_mail"}

    # Persist inbound for audit trail
    inbound_path = INBOUND_DIR / f"{email['thread_id']}.json"
    inbound_path.write_text(json.dumps(email, indent=2))

    # Run pipeline
    package = ari_pipeline.process_email(email)
    package_full = json.loads(pathlib.Path(package["files"]["package"]).read_text())

    if dry_run:
        return {
            "thread_id": email["thread_id"],
            "classification": package["classification"],
            "draft_path": package["files"]["package"],
            "dry_run": True,
        }

    # Create the draft in 244downeyapt3@'s drafts folder
    draft_id = gmail_client.create_draft(
        thread_id=email["thread_id"],
        to=package_full["draft_to_tenant"]["to"],
        subject=package_full["draft_to_tenant"]["subject"],
        body=package_full["draft_to_tenant"]["body"],
        in_reply_to=email.get("message_id"),
    )

    # Email Shaw the cover note + drafted reply
    cover_subject, cover_body = _build_cover_email(email, package_full)
    cover_msg_id = gmail_client.send_email(OWNER_EMAIL, cover_subject, cover_body)

    # Mark the inbound thread as read so we don't re-surface it next poll.
    _mark_thread_read(thread_summary["id"])

    return {
        "thread_id": email["thread_id"],
        "classification": package["classification"],
        "gmail_draft_id": draft_id,
        "cover_message_id": cover_msg_id,
        "package_path": package["files"]["package"],
    }


def _mark_thread_read(thread_id: str) -> None:
    """Remove UNREAD label so the same thread doesn't keep matching is:unread."""
    try:
        from googleapiclient.discovery import build
        from gmail_client import _load_credentials
        svc = build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)
        svc.users().threads().modify(
            userId="me", id=thread_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        print(f"warn: could not mark thread {thread_id} as read: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline but don't create drafts or send cover notes")
    parser.add_argument("--query", default="is:unread newer_than:1d -from:me",
                        help="Gmail search query. Default: unread, last 24h, not from self.")
    args = parser.parse_args()

    state = _load_last_poll()
    started_at = datetime.datetime.utcnow().isoformat() + "Z"

    try:
        threads = gmail_client.list_new_threads(args.query)
    except Exception as e:
        # Don't silent-fail; surface to Shaw immediately per runbook
        err = traceback.format_exc()
        try:
            gmail_client.send_email(
                OWNER_EMAIL,
                "[URGENT] Ari pipeline error — could not list threads",
                f"Ari poll failed at {started_at}.\n\nError:\n{err}",
            )
        except Exception:
            pass
        print(err, file=sys.stderr)
        return 2

    results = []
    errors = 0
    emergencies = 0
    for t in threads:
        try:
            r = process_thread(t, dry_run=args.dry_run)
            results.append(r)
            if r.get("classification", {}).get("urgency") == "emergency":
                emergencies += 1
        except Exception as e:
            errors += 1
            err = traceback.format_exc()
            try:
                gmail_client.send_email(
                    OWNER_EMAIL,
                    f"[URGENT] Ari pipeline error on thread {t.get('id')}",
                    f"Failed at {datetime.datetime.utcnow().isoformat()}Z:\n\n{err}",
                )
            except Exception:
                pass
            print(err, file=sys.stderr)

    state["last_seen_iso"] = datetime.datetime.utcnow().isoformat() + "Z"
    _save_last_poll(state)

    # Reconcile draft lifecycle (sent/edited/abandoned) — feedback signal for tuning
    try:
        import reconcile
        recon = reconcile.reconcile_all()
        print(f"reconciled: {recon['by_status']}")
    except Exception as e:
        print(f"warn: reconcile failed: {e}", file=sys.stderr)

    drafted = sum(1 for r in results if "gmail_draft_id" in r or r.get("dry_run"))
    skipped = sum(1 for r in results if "skipped" in r)
    log_entry = {
        "ts": started_at,
        "threads_seen": len(threads),
        "drafted": drafted,
        "skipped": skipped,
        "emergencies": emergencies,
        "errors": errors,
        "dry_run": args.dry_run,
        "results": results,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    print(f"{drafted} drafted, {skipped} skipped, {emergencies} emergency, {errors} errors. "
          f"{'(dry run)' if args.dry_run else 'drafts in 244downeyapt3@; cover notes sent to ' + OWNER_EMAIL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
