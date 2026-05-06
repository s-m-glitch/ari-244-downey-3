#!/usr/bin/env python3
"""
Weekly digest of Ari's drafts and Shaw's responses to them.

Reads state/draft_feedback.jsonl, summarizes the past 7 days, emails Shaw at
mckean.shaw@gmail.com with per-category metrics and the most informative edits.

Run from cron / launchd / Cowork scheduler weekly:

  python3 scripts/weekly_digest.py             # default 7 days
  python3 scripts/weekly_digest.py --days 14   # longer window
  python3 scripts/weekly_digest.py --dry-run   # print, don't send
"""

from __future__ import annotations
import argparse
import collections
import datetime
import json
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import gmail_client  # noqa: E402

ARI_ROOT = SCRIPTS_DIR.parent
FEEDBACK_LOG = ARI_ROOT / "state" / "draft_feedback.jsonl"
OWNER_EMAIL = "mckean.shaw@gmail.com"


def _load_latest_per_package() -> list[dict]:
    """Latest entry per package_path (since the log is append-only)."""
    if not FEEDBACK_LOG.exists():
        return []
    latest: dict[str, dict] = {}
    for line in FEEDBACK_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            latest[e["package_path"]] = e
        except json.JSONDecodeError:
            continue
    return list(latest.values())


def _entries_in_window(entries: list[dict], days: int) -> list[dict]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    in_window = []
    for e in entries:
        ts = e.get("checked_at")
        if not ts:
            continue
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cutoff:
            in_window.append(e)
    return in_window


def build_digest(entries: list[dict], days: int) -> tuple[str, str]:
    by_status = collections.Counter(e["status"] for e in entries)
    by_category: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    edited_examples: list[dict] = []
    abandoned_examples: list[dict] = []
    bypassed_examples: list[dict] = []

    for e in entries:
        cat = e.get("category", "unknown")
        by_category[cat][e["status"]] += 1
        if e["status"] == "sent_edited" and e.get("diff"):
            edited_examples.append(e)
        elif e["status"] == "abandoned":
            abandoned_examples.append(e)
        elif e["status"] == "bypassed":
            bypassed_examples.append(e)

    total = sum(by_status.values())
    sent = by_status.get("sent_unchanged", 0) + by_status.get("sent_edited", 0)
    approval_rate = (sent / total) if total else 0.0
    unedited_rate = (by_status.get("sent_unchanged", 0) / sent) if sent else 0.0

    subject = f"Ari weekly digest — {sent}/{total} drafts sent ({int(approval_rate*100)}% approval, {int(unedited_rate*100)}% unedited)"

    lines = [
        f"Window: last {days} days",
        f"Total drafts checked: {total}",
        f"  sent unchanged   : {by_status.get('sent_unchanged', 0)}",
        f"  sent with edits  : {by_status.get('sent_edited', 0)}",
        f"  abandoned        : {by_status.get('abandoned', 0)}",
        f"  pending          : {by_status.get('pending', 0)}",
        f"  bypassed         : {by_status.get('bypassed', 0)}",
        f"  errors           : {by_status.get('error', 0)}",
        "",
        f"Approval rate (sent / total non-pending): {approval_rate:.0%}",
        f"Unedited send rate (clean / sent)        : {unedited_rate:.0%}",
        "",
    ]

    # Per-category breakdown
    if by_category:
        lines.append("BY CATEGORY")
        for cat, counts in sorted(by_category.items()):
            cat_total = sum(counts.values())
            cat_sent = counts.get("sent_unchanged", 0) + counts.get("sent_edited", 0)
            cat_unedited = counts.get("sent_unchanged", 0)
            unedited_rate_cat = (cat_unedited / cat_sent) if cat_sent else 0.0
            lines.append(
                f"  {cat:<40} {cat_total:>3} total | "
                f"{counts.get('sent_unchanged', 0):>2} clean | "
                f"{counts.get('sent_edited', 0):>2} edited | "
                f"{counts.get('abandoned', 0):>2} abandoned | "
                f"unedited rate {int(unedited_rate_cat*100)}%"
            )
        lines.append("")

    # Notable edits — Shaw's actual changes are the highest-signal training data
    if edited_examples:
        lines.append("NOTABLE EDITS (Shaw's tone/changes — highest-signal feedback)")
        lines.append("─" * 70)
        for ex in edited_examples[:10]:
            lines.append(f"\n[{ex.get('category')}]")
            diff = ex.get("diff", {})
            lines.append(f"  similarity: {diff.get('similarity')}, char delta: {diff.get('char_length_delta')}")
            if diff.get("unified_diff"):
                lines.append("  diff:")
                for d in diff["unified_diff"].splitlines()[:30]:
                    lines.append(f"    {d}")
            lines.append("")

    # Graduation candidates — categories with high unedited rate over enough volume
    candidates = []
    for cat, counts in by_category.items():
        cat_sent = counts.get("sent_unchanged", 0) + counts.get("sent_edited", 0)
        cat_unedited = counts.get("sent_unchanged", 0)
        if cat_sent >= 5 and (cat_unedited / cat_sent) >= 0.9:
            candidates.append((cat, cat_unedited, cat_sent))
    if candidates:
        lines.append("GRADUATION CANDIDATES (≥5 sent, ≥90% unedited)")
        for cat, clean, total_sent in candidates:
            lines.append(f"  {cat}: {clean}/{total_sent} unedited — consider promoting to auto-send")
        lines.append("")

    if abandoned_examples:
        lines.append(f"ABANDONED DRAFTS ({len(abandoned_examples)}) — drafts you chose not to send")
        for ex in abandoned_examples[:5]:
            lines.append(f"  [{ex.get('category')}] {pathlib.Path(ex['package_path']).name}")
        lines.append("")

    body = "\n".join(lines)
    return subject, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    entries = _entries_in_window(_load_latest_per_package(), args.days)
    if not entries:
        print(f"no feedback entries in the last {args.days} days; skipping digest")
        return 0

    subject, body = build_digest(entries, args.days)
    if args.dry_run:
        print("=" * 70)
        print(f"To: {OWNER_EMAIL}")
        print(f"Subject: {subject}")
        print("=" * 70)
        print(body)
        return 0

    msg_id = gmail_client.send_email(OWNER_EMAIL, subject, body)
    print(f"sent digest: {msg_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
