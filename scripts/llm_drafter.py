"""
LLM-backed draft generation.

The pipeline calls `draft_with_claude(email, classification, kb)` which:
  1. Loads the system prompt from prompts/system_prompt.md (the persona/policy contract).
  2. Loads structured KB context (property facts, tenant context, current state).
  3. Sends a single user message containing classification + inbound email + ticket history.
  4. Returns a dict matching the {to, subject, body, send_only_after} shape.

Falls back to None on any error so the caller can use the deterministic template.

Requires: ANTHROPIC_API_KEY env var. Get one at https://console.anthropic.com.
"""

from __future__ import annotations
import json
import os
import pathlib
import re
import sys
from typing import Any

try:
    import anthropic
except ImportError:
    anthropic = None  # gracefully degrade if not installed

ARI_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPTS_DIR = ARI_ROOT / "prompts"
KB_DIR = ARI_ROOT / "kb"
STATE_DIR = ARI_ROOT / "state"

# Default model. Sonnet for production quality. Haiku is cheaper but the
# voice tends to read flatter. Override per call with `model=`.
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 1024


def _system_prompt() -> str:
    return (PROMPTS_DIR / "system_prompt.md").read_text()


def _kb_blob() -> str:
    """Compact JSON of property facts + tenant context + relevant state for the prompt."""
    blob = {
        "property": json.loads((KB_DIR / "property.json").read_text()),
        "tenant_context": json.loads((STATE_DIR / "tenant_context.json").read_text()),
        "rent_calendar": json.loads((STATE_DIR / "rent_calendar.json").read_text()),
        "open_tickets": _open_tickets(),
        "permissions": json.loads((STATE_DIR / "permissions.json").read_text())["permissions"],
    }
    return json.dumps(blob, indent=2, default=str)


def _open_tickets() -> list:
    data = json.loads((STATE_DIR / "tickets.json").read_text())
    return [t for t in data.get("tickets", []) if t.get("status") not in ("Resolved",)]


def _build_user_message(email: dict, classification: dict) -> str:
    """The single user-turn prompt: classification + inbound email + KB."""
    return f"""You're drafting Ari's reply to a tenant email.

CLASSIFICATION (already determined by deterministic rules):
{json.dumps(classification, indent=2)}

INBOUND EMAIL:
From: {email['from']}
Date: {email.get('date', '')}
Subject: {email['subject']}
Attachments: {[a['filename'] for a in email.get('attachments', [])] or 'none'}

Body:
\"\"\"
{email['body']}
\"\"\"

KNOWLEDGE BASE (current ground truth):
{_kb_blob()}

YOUR TASK:
Write the tenant-facing reply body following the persona, voice, and §6/§8 guidance in the system prompt. Match the §8 voice anchors closely — warm, plain, slightly understated. Sign as "— Ari" on a new line. Cite policy conversationally if needed; never quote section numbers adversarially.

Return a JSON object EXACTLY in this shape, with no commentary outside the JSON:
{{
  "subject": "Re: <original subject>",
  "body": "<the reply, ending with — Ari>"
}}

Hard rules (never violate):
- Never reference AB 1482, CPI, or state rent caps in tenant-facing copy.
- Never mention attachments/photos that don't actually exist (check the Attachments list above).
- Never re-raise the support-animal/Bones issue; it is settled.
- Never commit to specific timelines or expenses without Shaw's approval; use "I'll get this lined up" / "I'll circle back with a timeline" framing.
- Never quote lease section numbers as adversarial leverage.
- For escalation_only category: return body that says you didn't draft a tenant-facing reply per §6.7.
"""


def draft_with_claude(
    email: dict,
    classification: dict,
    model: str = DEFAULT_MODEL,
) -> dict | None:
    """Generate a tenant-facing draft using Claude. Returns the draft dict or None on failure."""
    if anthropic is None:
        print("warn: anthropic SDK not installed; falling back to template", file=sys.stderr)
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("warn: ANTHROPIC_API_KEY not set; falling back to template", file=sys.stderr)
        return None

    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=_system_prompt(),
            messages=[{"role": "user", "content": _build_user_message(email, classification)}],
        )
    except Exception as e:
        print(f"warn: anthropic API call failed: {e}", file=sys.stderr)
        return None

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    parsed = _extract_json(text)
    if not parsed or "body" not in parsed:
        print(f"warn: could not parse JSON from Claude response: {text[:200]}", file=sys.stderr)
        return None

    return {
        "to": _to_address(email["from"]),
        "subject": parsed.get("subject", f"Re: {email['subject']}"),
        "body": parsed["body"].strip(),
        "send_only_after": "shaw_approval",
    }


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a possibly-noisy response."""
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a balanced {...} block
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i, c in enumerate(text[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None


def _to_address(email_from: str) -> str:
    m = re.search(r"<([^>]+)>", email_from)
    return m.group(1) if m else email_from


# ---------------------------------------------------------------------------
# CLI for one-off testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: llm_drafter.py <email_fixture.json>")
        sys.exit(2)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import ari_pipeline
    email = json.loads(pathlib.Path(sys.argv[1]).read_text())
    classification = ari_pipeline.classify(email)
    draft = draft_with_claude(email, classification)
    if draft is None:
        print("LLM draft failed; would fall back to template", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"classification": classification, "draft": draft}, indent=2))
