# Feedback Loop — How Ari Learns From You

The single most valuable input to graduating Ari toward auto-send is *what you actually do with the drafts*. The feedback loop captures that automatically.

## What gets captured

Every time `run_poll.py` runs, it now also calls `scripts/reconcile.py` which walks every Ari draft and asks Gmail:

- Is the draft still pending in `244downeyapt3@`'s drafts folder?
- Or did a sent message appear on the same thread, sent from `244downeyapt3@`, that wasn't there before?
- If sent: how does the body compare to what Ari wrote?

Each draft's lifecycle status lands in `state/draft_feedback.jsonl`:

| Status | Meaning |
|---|---|
| `pending` | Draft still sitting in Drafts, you haven't decided yet |
| `sent_unchanged` | You sent it as-is (≥92% similarity) |
| `sent_edited` | You sent it but edited the body |
| `abandoned` | Draft is gone (deleted) and never sent — you decided to handle differently |
| `bypassed` | A sent message exists on the thread but not from `244downeyapt3@` (rare) |
| `error` | Reconcile couldn't determine state |

Each entry includes the original Ari draft body, the sent body (if any), and a `unified_diff` of the changes — that's the gold for prompt tuning.

## How to use it

**Daily / when curious:**
```bash
python3 scripts/reconcile.py
# prints: {"checked": 12, "new": 0, "updated": 2, "by_status": {"sent_unchanged": 8, "sent_edited": 3, "abandoned": 1}}
```

**Weekly digest emailed to you:** A scheduled task `ari-weekly-digest` runs every Monday at 8am. You'll get an email at `mckean.shaw@gmail.com` with:

- Counts and rates (approval, unedited).
- Per-category breakdown.
- The diff of every edit you made — read these. They're literally training data.
- Graduation candidates: categories where you've sent ≥5 messages with ≥90% unedited rate. Those are ready to consider promoting to auto-send.
- Abandoned drafts: which ones you walked away from.

To preview the digest without sending:
```bash
python3 scripts/weekly_digest.py --dry-run
```

## What to do with the signal

Three feedback loops, in increasing leverage:

**1. (Quick) Tune prompts.** When you see a recurring edit pattern — say, you keep adding "no late fee this time" to payment-delay drafts — bake it into the system prompt or the category-specific guidance. Reduces edit work next time.

**2. (Medium) Update the KB.** When edits reflect things that should be facts (a new vendor, an exception you granted, a pattern Mareika prefers), add them to `state/permissions.json` or `state/tenant_context.json`. The LLM drafter pulls those in automatically.

**3. (Big lever) Promote categories to auto-send.** Once a category passes the graduation threshold (≥5 sends, ≥90% unedited, weeks of stability), flip it. The spec already calls out which categories should *never* graduate (rent changes, vendor dispatch, anything legal). For the rest — especially simple acknowledgments and HOA relays — auto-send is the goal.

## Recommended cadence

- **First 2 weeks:** Read every cover note carefully. Send/edit/abandon as you would normally. Don't try to "train" — just behave naturally.
- **Sundays, ~5 minutes:** Skim Monday's digest. Notice patterns in your edits.
- **Every 4 weeks:** If a category's unedited rate is climbing, consider a prompt tweak. If a category's approval rate is high and stable, consider promotion.

## Helping Ari learn faster

You said you'd respond to dummy emails to give Ari a sense of your tone. That's exactly the right input. Specifically valuable:

- **Send the dummy emails as if you were Mareika** — varied subjects, varied registers (urgent vs. casual, formal vs. chatty).
- **Edit Ari's drafts before sending in your own voice.** The diff between Ari's draft and your sent version becomes a high-signal training example.
- **Occasionally abandon a draft and write your own from scratch.** That tells Ari "this approach was wrong." Note the abandonment in `state/tenant_context.json` if it's a one-off context Ari should know.

After a couple of dozen of these, the digests will tell you exactly where Ari's voice has drifted from yours, and prompt-tuning becomes mechanical.
