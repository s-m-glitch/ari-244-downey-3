#!/usr/bin/env python3
"""
Reconcile Ari's drafts against what Shaw actually sent.

For every draft package in drafts/*.json, determine the lifecycle status:
  - pending       : draft still exists in Gmail, not yet sent
  - sent_unchanged: a sent message exists on the thread matching the draft body
  - sent_edited   : a sent message exists but the body differs from Ari's draft
  - abandoned     : draft is gone (deleted) and no matching sent message was found
  - bypassed      : sent from a different account (e.g. mckean.shaw@) on the same thread

Writes results to state/draft_feedback.jsonl (one line per draft, append-only —
later runs add updates as the lifecycle progresses, e.g. pending → sent_edited).
"""

from __future__ import annotations
import datetime
import difflib
import json
import pathlib
import sys
from email.utils import parsedate_to_datetime
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import gmail_client  # noqa: E402

ARI_ROOT = SCRIPTS_DIR.parent
DRAFTS_DIR = ARI_ROOT / "drafts"
STATE_DIR = ARI_ROOT / "state"
FEEDBACK_LOG = STATE_DIR / "draft_feedback.jsonl"

TENANT_INBOX = "244downeyapt3@gmail.com"
ABANDONED_AFTER_DAYS = 7


def _load_existing_feedback() -> dict[str, dict]:
    """Return latest entry per package_path from the append-only log."""
    if not FEEDBACK_LOG.exists():
        return {}
    out: dict[str, dict] = {}
    for line in FEEDBACK_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            out[entry["package_path"]] = entry
        except json.JSONDecodeError:
            continue
    return out


def _append_feedback(entry: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    with open(FEEDBACK_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _normalize(text: str) -> str:
    """Normalize a body for comparison: collapse whitespace, drop quoted reply tail."""
    if not text:
        return ""
    # Drop quoted reply chains (lines starting with > or "On <date> wrote:")
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">"):
            break
        if s.startswith("On ") and "wrote:" in s:
            break
        lines.append(line)
    return " ".join(" ".join(lines).split()).strip()


def _diff_summary(a: str, b: str) -> dict:
    """Return a small dict describing the diff between two bodies."""
    na, nb = _normalize(a), _normalize(b)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    char_distance = abs(len(na) - len(nb))
    # Generate a readable unified diff (small, capped)
    diff = list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile="ari_draft", tofile="sent",
        lineterm="", n=1,
    ))[:80]
    return {
        "similarity": round(ratio, 3),
        "char_length_delta": char_distance,
        "draft_chars": len(na),
        "sent_chars": len(nb),
        "unified_diff": "\n".join(diff),
    }


def reconcile_one(package_path: pathlib.Path, prior: dict | None = None) -> dict:
    """Determine the current status of a single draft package."""
    pkg = json.loads(package_path.read_text())
    thread_id = pkg["thread_id"]
    ari_body = pkg["draft_to_tenant"]["body"]
    ari_draft_id = pkg.get("gmail_draft_id") or (prior or {}).get("ari_draft_id")
    created_at = pkg.get("inbound_email", {}).get("date")
    classification = pkg.get("classification", {})

    # If we already finalized this entry, don't re-check
    if prior and prior.get("status") in ("sent_unchanged", "sent_edited", "abandoned", "bypassed"):
        return prior

    now = datetime.datetime.now(datetime.timezone.utc)
    status: str = "pending"
    sent_msg_id: str | None = None
    sent_body: str | None = None
    diff: dict | None = None

    # Did Mareika get a message from us on this thread?
    try:
        messages = gmail_client.list_thread_messages(thread_id)
    except Exception as e:
        return {
            **(prior or {}),
            "package_path": str(package_path),
            "thread_id": thread_id,
            "category": f"{classification.get('category')}/{classification.get('subcategory')}",
            "ari_draft_id": ari_draft_id,
            "ari_draft_body": ari_body,
            "status": "error",
            "error": str(e),
            "checked_at": now.isoformat(),
        }

    # Find the latest message from us that's NOT the draft itself
    for msg in reversed(messages):
        sender = msg["from"].lower()
        if TENANT_INBOX not in sender:
            continue
        # Skip the draft we created (drafts appear as messages but with DRAFT label)
        # The cleanest signal is comparing message_id to ari_draft_id; but draft
        # IDs and message IDs differ. So we instead infer: if a draft still
        # exists in Gmail, this message IS the draft, otherwise it's the sent.
        if ari_draft_id:
            try:
                if gmail_client.draft_exists(ari_draft_id):
                    # Still pending — the only "us" message in thread is the draft
                    status = "pending"
                    break
            except Exception:
                pass
        # If we get here, the draft was sent or deleted. The message we see is the sent.
        sent_msg_id = msg.get("message_id")
        sent_body = msg["body"]
        diff = _diff_summary(ari_body, sent_body)
        # Threshold: 0.92 similarity = essentially unchanged
        status = "sent_unchanged" if diff["similarity"] >= 0.92 else "sent_edited"
        break

    if status == "pending" and ari_draft_id:
        # Draft might also be deleted with no send — that's "abandoned"
        try:
            if not gmail_client.draft_exists(ari_draft_id) and sent_msg_id is None:
                # Check if it's been long enough to mark abandoned
                inbound_dt = None
                if created_at:
                    try:
                        inbound_dt = parsedate_to_datetime(created_at)
                    except Exception:
                        pass
                age_days = (now - inbound_dt).days if inbound_dt else 999
                status = "abandoned" if age_days >= ABANDONED_AFTER_DAYS else "deleted_recently"
        except Exception:
            pass

    return {
        "package_path": str(package_path),
        "thread_id": thread_id,
        "category": f"{classification.get('category')}/{classification.get('subcategory')}",
        "ari_draft_id": ari_draft_id,
        "ari_draft_body": ari_body,
        "status": status,
        "sent_message_id": sent_msg_id,
        "sent_body": sent_body,
        "diff": diff,
        "checked_at": now.isoformat(),
    }


def reconcile_all() -> dict:
    """Walk every package, update lifecycle status, append to feedback log."""
    prior = _load_existing_feedback()
    counts: dict[str, int] = {}
    updated = 0
    new = 0

    for pkg_path in sorted(DRAFTS_DIR.glob("*.json")):
        prior_entry = prior.get(str(pkg_path))
        entry = reconcile_one(pkg_path, prior_entry)

        # Only log if status changed (avoid duplicate noise)
        prior_status = (prior_entry or {}).get("status")
        if entry["status"] != prior_status:
            _append_feedback(entry)
            if prior_entry:
                updated += 1
            else:
                new += 1

        counts[entry["status"]] = counts.get(entry["status"], 0) + 1

    return {
        "checked": len(list(DRAFTS_DIR.glob("*.json"))),
        "new": new,
        "updated": updated,
        "by_status": counts,
    }


if __name__ == "__main__":
    summary = reconcile_all()
    print(json.dumps(summary, indent=2))
