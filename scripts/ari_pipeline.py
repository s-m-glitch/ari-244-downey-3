#!/usr/bin/env python3
"""
Ari — Tenant Inbox Agent: v0 deterministic pipeline.

Takes an inbound email JSON, returns a Draft Package: classification + draft to tenant
+ cover note to Shaw + state updates. Persists state changes and writes the outbound
draft as a .eml-style file to /drafts/.

The classification + draft generation here are rule + template based, calibrated to
the §8 voice anchors. In vN this same orchestration shape can be backed by a Claude
call using prompts/system_prompt.md — the pipeline boundary is unchanged.
"""

from __future__ import annotations
import json, os, re, sys, uuid, datetime, pathlib
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ARI_ROOT = pathlib.Path(__file__).resolve().parents[1]
KB_DIR = ARI_ROOT / "kb"
STATE_DIR = ARI_ROOT / "state"
DRAFTS_DIR = ARI_ROOT / "drafts"
LOG_DIR = ARI_ROOT / "logs"
DRAFTS_DIR.mkdir(exist_ok=True, parents=True)
LOG_DIR.mkdir(exist_ok=True, parents=True)


def _load_json(path: pathlib.Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _save_json(path: pathlib.Path, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

EMERGENCY_PATTERNS = [
    r"\b(fire|smoke|gas leak|gas smell|sewage|sewer back\s*up|flooding?)\b",
    r"\b(no working lock|locked? out|broken lock)\b",
    r"\bwater (?:is )?(?:actively )?(?:dripping|leaking|pouring)\b",
    r"\bceiling\b.*\b(leak|drip)\b",
    r"\bcarbon monoxide\b",
    r"\b911\b",
]
URGENT_PATTERNS = [
    r"\bno hot water\b",
    r"\bfridge (?:is )?out\b|\brefrigerator (?:is )?(?:not working|broken|out)\b",
    r"\bheat (?:is )?out\b|\bno heat\b",
    r"\bstove (?:is )?(?:not working|broken)\b",
    r"\boven (?:is )?(?:not working|broken)\b",
    r"\bsmoke detector\b.*\b(beep|chirp|alarm)\b",
]
MAINTENANCE_PATTERNS = [
    r"\b(broken|broke|leak(?:ing)?|drip|cracked|won'?t close|won'?t open|not working|busted|stuck)\b",
    r"\b(blind|curtain|faucet|sink|toilet|shower|tub|disposal|outlet|switch|pest|rodent|mouse|mice|rat|ant|roach|termite|drain|hvac|heater|window|door)\b",
    r"\b(repair|fix|maintenance)\b",
]
PAYMENT_PATTERNS = [
    r"\brent\b",
    r"\b(payment|paying|pay|paid|check)\b.*\b(rent|late|delay|few days|next week)\b",
    r"\b(late|delay|behind on)\b.*\b(rent|payment)\b",
]
PAYMENT_DELAY_PATTERNS = [
    r"\b(late|delay(?:ed)?|behind|few days late|will be late|going to be late)\b",
    r"\b(payroll)\b.*\b(delay|late)\b",
]
POLICY_PATTERNS = [
    r"\b(can i|is it ok|am i allowed|allowed to|permission|permit|ok to|alright if|okay if)\b",
    r"\b(install|paint|sublet|guest|stay|window ac|air conditioner|satellite|dish|antenna|second pet|another pet)\b",
    # Building rules / CC&R-relevant questions tenants ask:
    r"\b(quiet hours?|noise|loud|music|party|gathering|dinner party|hosting)\b",
    r"\bwhat (?:are )?the rules?\b",
    r"\b(rules?|policy|policies)\b.*\b(building|hoa|noise|parking|guest|pet)\b",
    r"\b(parking|garage|spot|space)\b.*\b(use|borrow|park|cousin|friend|guest|visitor)\b",
]
HOA_BOARD_HINTS = [
    r"\b(hoa|home\s*owners?|board|condo association|special assessment|election notice)\b",
    r"\bbuilding\b.*\b(water|electricity|maintenance)\b",
    r"\briser\b",
    r"\bcommon area\b",
]
LEGAL_THREAT_PATTERNS = [
    r"\b(habitability|tenant rights|tenants? union|sf rent board|attorney|lawyer|sue|lawsuit|legal action)\b",
    r"\b(harassment|discriminat|retaliation)\b",
    r"\b(move out|moving out|breaking the lease|break the lease|30[- ]?day notice|terminate)\b",
]
GENERAL_PATTERNS = [
    r"\b(mail|package|delivery)\b.*\b(downstairs|left|here|for you|came)\b",
    r"\bjust (?:a heads? up|wanted to let you know|fyi)\b",
]


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, flags=re.I) for p in patterns)


def _hoa_member_emails() -> set[str]:
    """Load the set of HOA member email addresses from the KB."""
    try:
        data = json.loads((KB_DIR / "hoa_board.json").read_text())
        return {m["email"].lower() for m in data.get("members", [])}
    except FileNotFoundError:
        return set()


def classify(email: dict) -> dict:
    """Return {category, subcategory, urgency, rationale}.

    Decision order matters — escalation > emergency > urgent > category-specific."""
    text = f"{email.get('subject', '')}\n{email.get('body', '')}"
    sender_lc = email.get("from", "").lower()

    # HOA board member email addresses are the most reliable HOA signal.
    # Recognize these BEFORE any other category check.
    hoa_emails = _hoa_member_emails()
    sender_is_hoa = any(e in sender_lc for e in hoa_emails)
    if sender_is_hoa:
        # Sub-classify: is this informational, needs-a-reply, or a board decision?
        is_board_decision = bool(re.search(
            r"\b(special assessment|assessment|dues increase|raise (?:the )?dues|"
            r"vote|voting|proposal|approve|approval|election|budget|insurance renewal|"
            r"meeting agenda|board meeting|next meeting)\b",
            text, flags=re.I,
        ))
        if is_board_decision:
            return {
                "category": "hoa_correspondence",
                "subcategory": "board_decision",
                "urgency": "routine",
                "rationale": "From HOA board member, content includes board-decision keywords (assessment / vote / dues / budget / meeting agenda). Per §6.5, flag without drafting a substantive reply; surface for Shaw's review.",
            }
        return {
            "category": "hoa_correspondence",
            "subcategory": "board_correspondence",
            "urgency": "routine",
            "rationale": "From HOA board member; routine board-to-board correspondence. Draft a peer-voice reply for Shaw's review.",
        }

    # Highest priority: anything legal/move-out → escalation_only (§6.7)
    if _matches(LEGAL_THREAT_PATTERNS, text):
        return {
            "category": "escalation_only",
            "subcategory": "legal_or_move_out",
            "urgency": "urgent",
            "rationale": "Legal-threat / move-out / tenant-rights language detected. Per §6.7 do not auto-respond; escalate to Shaw immediately.",
        }

    # Emergency maintenance (§6.1)
    if _matches(EMERGENCY_PATTERNS, text):
        return {
            "category": "maintenance",
            "subcategory": "emergency",
            "urgency": "emergency",
            "rationale": "Life/safety or major property damage signal (water/fire/sewage/gas/lock). 24/7 ping per §7.",
        }

    # HOA correspondence (§6.5) — likely from HOA sender
    sender = email.get("from", "").lower()
    if "hoa" in sender or "board" in sender or _matches(HOA_BOARD_HINTS, text) and "tenant" not in sender:
        return {
            "category": "hoa_correspondence",
            "subcategory": "building_notice",
            "urgency": "routine",
            "rationale": "HOA notice signal — track impact on tenant, draft Shaw's response if needed, summarize for Mareika if she's affected.",
        }

    # Urgent maintenance
    if _matches(URGENT_PATTERNS, text):
        return {
            "category": "maintenance",
            "subcategory": "urgent",
            "urgency": "urgent",
            "rationale": "Single-point failure of daily-use system (hot water / fridge / heat / range). Ack within the hour per §6.1.",
        }

    # Payment-related
    if _matches(PAYMENT_PATTERNS, text):
        if _matches(PAYMENT_DELAY_PATTERNS, text):
            return {
                "category": "payment",
                "subcategory": "tenant_initiated_delay",
                "urgency": "urgent",
                "rationale": "Tenant signaling late payment. Empathetic ack, ask for expected date, surface to Shaw same-day per §6.3.",
            }
        return {
            "category": "payment",
            "subcategory": "general",
            "urgency": "routine",
            "rationale": "Rent / payment related but not a delay flag.",
        }

    # Policy / lease question (§6.4)
    if _matches(POLICY_PATTERNS, text):
        return {
            "category": "policy_question",
            "subcategory": "consent_required",
            "urgency": "routine",
            "rationale": "Tenant asking permission for something covered by lease/CC&Rs — likely needs owner consent (§32 alterations, §17 subletting, §39 satellite) and/or HOA board (exterior/common area).",
        }

    # General maintenance (the broken-blinds case lands here)
    if _matches(MAINTENANCE_PATTERNS, text):
        return {
            "category": "maintenance",
            "subcategory": "routine",
            "urgency": "routine",
            "rationale": "Maintenance signal without emergency/urgency markers. Routine triage per §6.1.",
        }

    # General / informational (the mail case lands here)
    if _matches(GENERAL_PATTERNS, text) or len(text.split()) < 30:
        return {
            "category": "general",
            "subcategory": "informational",
            "urgency": "n/a",
            "rationale": "Short informational message. Brief warm ack per §6.2.",
        }

    # Default: route to Shaw, flag uncertainty
    return {
        "category": "escalation_only",
        "subcategory": "unclassified",
        "urgency": "urgent",
        "rationale": "Could not confidently classify. Per §7, anything Ari isn't confident classifying gets routed urgent to Shaw.",
    }


# ---------------------------------------------------------------------------
# Draft generation (templates calibrated to §8 voice anchors)
# ---------------------------------------------------------------------------


def _first_name(email_from: str) -> str:
    m = re.match(r"\s*([^<]+?)\s*<", email_from)
    full = m.group(1) if m else email_from.split("@")[0]
    return full.split()[0].strip().rstrip(",")


def _to_address(email_from: str) -> str:
    m = re.search(r"<([^>]+)>", email_from)
    return m.group(1) if m else email_from


SIG = "— Ari"


def draft_maintenance_emergency(email: dict, kb: dict) -> dict:
    name = _first_name(email["from"])
    has_phone = bool(re.search(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", email.get("body", "")))
    contact_clause = (
        "I'll call you at the number you sent in the next few minutes."
        if has_phone
        else "I'll get someone in touch with you as soon as possible — please reply with a phone number if you have one handy."
    )
    body = (
        f"Hi {name} — got it, I'm on this right now. Looping in Shaw immediately and "
        f"working to get someone over there. {contact_clause} "
        f"In the meantime, if anything escalates (water spreading, electrical near the leak, ceiling sagging) "
        f"please shut off the unit's water at the main valve if you can safely reach it, and step out if you feel unsafe.\n\n"
        f"{SIG}"
    )
    return {
        "to": _to_address(email["from"]),
        "subject": f"Re: {email['subject']}",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_maintenance_urgent(email: dict, kb: dict) -> dict:
    name = _first_name(email["from"])
    body = (
        f"Hi {name} — thanks for letting me know. I want to make sure I've got this right: "
        f"sounds like a single-point failure on something you use daily. I'll get this in front of Shaw within the hour "
        f"and line up a fix. I'll circle back today with a timeline.\n\n"
        f"{SIG}"
    )
    return {
        "to": _to_address(email["from"]),
        "subject": f"Re: {email['subject']}",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_maintenance_routine(email: dict, kb: dict) -> dict:
    """Calibrated to the broken-blinds voice anchor."""
    name = _first_name(email["from"])
    has_photos = any(a.get("mime_type", "").startswith("image/") for a in email.get("attachments", []))
    photo_clause = " and for the photos" if has_photos else ""
    body = (
        f"Hi {name} — thanks for flagging this{photo_clause}. "
        f"Looks like a clean failure that should be a straightforward fix. "
        f"I'll get it lined up and circle back with a timeline. Let me know if anything changes in the meantime.\n\n"
        f"{SIG}"
    )
    return {
        "to": _to_address(email["from"]),
        "subject": f"Re: {email['subject']}",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_general(email: dict, kb: dict) -> dict:
    """Calibrated to the mail voice anchor."""
    name = _first_name(email["from"])
    body = (
        f"Thanks {name}, appreciate the heads up — I'll let Shaw know so he can grab it next time he's by.\n\n"
        f"{SIG}"
    )
    return {
        "to": _to_address(email["from"]),
        "subject": f"Re: {email['subject']}",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_payment_delay(email: dict, kb: dict) -> dict:
    name = _first_name(email["from"])
    body = (
        f"Hi {name} — thanks for the heads up, that's helpful to know in advance. "
        f"Let me confirm with Shaw and circle back. If you can give me a firm date you'd expect the check to clear, that'll make this easy. "
        f"No need to worry — appreciate you flagging it early.\n\n"
        f"{SIG}"
    )
    return {
        "to": _to_address(email["from"]),
        "subject": f"Re: {email['subject']}",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_policy_question(email: dict, kb: dict) -> dict:
    name = _first_name(email["from"])
    body = (
        f"Hi {name} — good question. Let me run this by Shaw quickly since it's the kind of thing he likes to weigh in on, "
        f"and depending on the specifics there may also be an HOA piece. I'll come back to you within a day or so with a clear answer. "
        f"If you want, send a quick note about what you have in mind (model, where it'd go, etc.) and I'll include that when I check in with him.\n\n"
        f"{SIG}"
    )
    return {
        "to": _to_address(email["from"]),
        "subject": f"Re: {email['subject']}",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_hoa_relayable_summary(email: dict, kb: dict) -> dict:
    """When HOA notice affects tenant, draft a relayable summary to Mareika."""
    body_in = email.get("body", "")
    # Try to extract a date/time from the notice
    timing = re.search(r"\b((?:mon|tue|tues|wed|thu|thur|fri|sat|sun)\w*\s+\w+\s+\d+(?:[,]\s*\d{4})?)\b.*?(\d{1,2}\s*[ap]m)\s*(?:to|–|-)\s*(\d{1,2}\s*[ap]m)",
                       body_in, flags=re.I)
    when = ""
    if timing:
        when = f" {timing.group(1).strip()} from {timing.group(2)} to {timing.group(3)}"
    body = (
        f"Hi Mareika — quick heads up from the HOA: building water will be off{when}. "
        f"You may want to fill a pitcher beforehand. Let me know if that timing is a problem and I can flag it.\n\n"
        f"{SIG}"
    )
    return {
        "to": kb["tenant"]["email"],
        "subject": "Heads up: building water shutoff",
        "body": body,
        "send_only_after": "shaw_approval",
    }


def draft_escalation(email: dict, kb: dict) -> dict:
    """Per §6.7: do not respond. Return an empty tenant draft and surface internally."""
    return {
        "to": _to_address(email["from"]),
        "subject": f"(no auto-reply) Re: {email['subject']}",
        "body": "(Ari did not draft a tenant-facing reply. Per §6.7 of the spec, this category is escalation-only — Shaw should respond directly.)",
        "send_only_after": "shaw_approval",
    }


# ---------------------------------------------------------------------------
# Cover note + state updates
# ---------------------------------------------------------------------------


def cover_note(email: dict, classif: dict, kb: dict) -> dict:
    cat = classif["category"]
    sub = classif["subcategory"]
    urg = classif["urgency"]

    if urg == "emergency":
        flag = "[URGENT — emergency]"
    elif urg == "urgent" or cat == "escalation_only":
        flag = "[URGENT]"
    else:
        flag = "[normal]"

    short = email["subject"][:60]
    subject = f"{flag} {short}"

    if cat == "maintenance":
        suggested = (
            "Approve the draft → I'll send. If you want Gilberto dispatched, reply 'dispatch Gilberto' "
            "and I'll coordinate scheduling and send 24-hr entry notice (lease §40)."
        )
        decisions = ["Approve draft as-is?", "Dispatch Gilberto?"]
        refs = ["lease §35 (maintenance)", "lease §40 (entry)", "CC&Rs §III.N (owner unit interior)"]
    elif cat == "general":
        suggested = "Approve the draft → I'll send. Will log informally; nothing else needed."
        decisions = ["Approve draft as-is?"]
        refs = []
    elif cat == "payment":
        if sub == "tenant_initiated_delay":
            suggested = (
                "Approve the draft → I'll send. Per §6.3 do NOT autonomously assess late fees or send pay-or-quit; "
                "let me know if/when the check clears so I can update the ledger."
            )
            decisions = ["Approve draft as-is?", "Late-fee waiver this time, or apply per lease §10?"]
            refs = ["lease §10 (late fee: greater of $50 or 5% if 3+ days late)"]
        else:
            suggested = "Approve the draft → I'll send."
            decisions = ["Approve draft as-is?"]
            refs = []
    elif cat == "policy_question":
        suggested = (
            "Approve the draft → I'll send the holding reply. Substantive answer needs your call on whether to "
            "grant consent, plus an HOA check if it touches common area / exterior. I'll draft the substantive reply once you decide."
        )
        decisions = ["Grant consent? Conditions? HOA approval needed first?"]
        refs = ["lease §32 (alterations)", "lease §17 (subletting)", "lease §39 (satellite)", "CC&Rs (board consent for exterior/common area)"]
    elif cat == "hoa_correspondence":
        suggested = (
            "Approve the draft summary to Mareika → I'll send. No reply to HOA needed unless you want one — "
            "let me know and I'll draft it. I've logged the notice and any deadline."
        )
        decisions = ["Approve relay summary to Mareika?", "Want to reply to HOA?"]
        refs = ["CC&Rs (HOA notice obligations)"]
    elif cat == "escalation_only":
        suggested = (
            "I did not draft a tenant-facing reply. Per §6.7 this category is escalation-only — recommend you respond "
            "directly. Happy to help compose once you've decided how to handle."
        )
        decisions = ["You handle this directly — confirm I should stand down?"]
        refs = []
    else:
        suggested = "Approve the draft → I'll send."
        decisions = ["Approve draft as-is?"]
        refs = []

    return {
        "subject": subject,
        "summary": _build_summary(email, classif),
        "suggested_action": suggested,
        "policy_refs": refs,
        "decisions_needed": decisions,
        "rationale": classif["rationale"],
    }


def _build_summary(email: dict, classif: dict) -> str:
    snippet = email["body"][:200].strip().replace("\n", " ")
    if len(email["body"]) > 200:
        snippet += "..."
    return f"From {email['from']} — {classif['category']}/{classif['subcategory']} ({classif['urgency']}). Snippet: \"{snippet}\""


def state_updates(email: dict, classif: dict) -> dict:
    cat = classif["category"]
    updates = {
        "ticket_action": "none",
        "ledger_action": "none",
        "hoa_log_action": "none",
        "permission_grant": None,
    }
    if cat == "maintenance":
        updates["ticket_action"] = "create"
    elif cat == "payment" and classif["subcategory"] == "tenant_initiated_delay":
        updates["ledger_action"] = "annotate_delay_flag"
    elif cat == "hoa_correspondence":
        updates["hoa_log_action"] = "create"
    return updates


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def apply_state_updates(email: dict, classif: dict, updates: dict, kb: dict) -> dict:
    """Persist state changes to the JSON files. Returns a dict of what was written."""
    written = {}

    if updates["ticket_action"] == "create":
        path = STATE_DIR / "tickets.json"
        data = _load_json(path)
        tid = data["next_id"]
        ticket = {
            "id": tid,
            "opened_on": email["date"][:10],
            "issue_summary": email["subject"],
            "status": "Open",
            "category": classif["subcategory"],
            "urgency": classif["urgency"],
            "owner_action_needed": True,
            "vendor": None,
            "photos": [a["filename"] for a in email.get("attachments", []) if a.get("mime_type", "").startswith("image/")],
            "thread_id": email["thread_id"],
            "resolved_on": None,
            "notes": classif["rationale"],
        }
        data["tickets"].append(ticket)
        data["next_id"] = tid + 1
        _save_json(path, data)
        written["ticket"] = ticket

    if updates["hoa_log_action"] == "create":
        path = STATE_DIR / "hoa_log.json"
        data = _load_json(path)
        nid = data["next_id"]
        # Try to find a deadline in the body
        deadline = None
        m = re.search(r"\b(?:by|before)\s+([A-Z][a-z]+\s+\d+,?\s*\d{4})", email["body"])
        if m:
            deadline = m.group(1)
        notice = {
            "id": nid,
            "received_on": email["date"][:10],
            "from": email["from"],
            "subject": email["subject"],
            "summary": email["body"][:240],
            "deadline": deadline,
            "tenant_impact": "water" in email["body"].lower() or "tenant" in email["body"].lower(),
            "tenant_relayable_summary": True,
            "shaw_response_required": False,
            "status": "logged",
        }
        data["notices"].append(notice)
        data["next_id"] = nid + 1
        _save_json(path, data)
        written["hoa_notice"] = notice

    if updates["ledger_action"] == "annotate_delay_flag":
        path = STATE_DIR / "payment_ledger.json"
        data = _load_json(path)
        # Annotate the most recent unconfirmed entry
        for entry in reversed(data["ledger"]):
            if entry["received_on"] is None:
                entry["notes"] = (entry.get("notes") or "") + (
                    f" [Tenant flagged delay on {email['date'][:10]} — body: \"{email['body'][:140]}\"]"
                )
                written["ledger_entry_annotated"] = entry
                break
        _save_json(path, data)

    return written


def write_draft_files(email: dict, classif: dict, draft: dict, cover: dict, state_written: dict) -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    base = f"{ts}__{classif['category']}__{email['thread_id']}"
    pkg_path = DRAFTS_DIR / f"{base}.json"
    eml_path = DRAFTS_DIR / f"{base}.eml.txt"

    package = {
        "thread_id": email["thread_id"],
        "inbound_email": {k: v for k, v in email.items() if k != "_note"},
        "classification": classif,
        "draft_to_tenant": draft,
        "cover_note_to_shaw": cover,
        "state_changes": state_written,
        "ai_disclosure_triggered": _disclosure_check(email),
        "ari_version": "v0",
    }
    _save_json(pkg_path, package)

    # Pseudo-RFC822 of the draft for human review
    eml = (
        f"From: Ari <244downeyapt3@gmail.com>\n"
        f"To: {draft['to']}\n"
        f"Subject: {draft['subject']}\n"
        f"In-Reply-To: {email['thread_id']}\n"
        f"X-Ari-Send-Only-After: {draft['send_only_after']}\n"
        f"X-Ari-Cover-Subject: {cover['subject']}\n"
        f"\n"
        f"{draft['body']}\n"
    )
    eml_path.write_text(eml)
    return {"package": str(pkg_path), "eml": str(eml_path)}


def _disclosure_check(email: dict) -> bool:
    """Trigger AI disclosure logic if the tenant directly asks if Ari is human/AI."""
    text = f"{email.get('subject', '')}\n{email.get('body', '')}"
    triggers = [
        r"\bare you (?:a )?(?:real|human|person|robot|bot|ai)\b",
        r"\bam i talking to (?:a )?(?:human|person|bot|ai|robot)\b",
        r"\bis this an ai\b",
        r"\bare you using ai\b",
    ]
    return any(re.search(p, text, flags=re.I) for p in triggers)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

DRAFT_DISPATCH = {
    ("maintenance", "emergency"): draft_maintenance_emergency,
    ("maintenance", "urgent"): draft_maintenance_urgent,
    ("maintenance", "routine"): draft_maintenance_routine,
    ("general", "informational"): draft_general,
    ("payment", "tenant_initiated_delay"): draft_payment_delay,
    ("policy_question", "consent_required"): draft_policy_question,
    ("hoa_correspondence", "building_notice"): draft_hoa_relayable_summary,
    ("escalation_only", "legal_or_move_out"): draft_escalation,
    ("escalation_only", "unclassified"): draft_escalation,
}


def process_email(email: dict, *, force_template: bool = False) -> dict:
    """Run the pipeline. By default uses LLM-backed drafting with template fallback.

    Set force_template=True to bypass the LLM (useful for offline testing).
    """
    kb = _load_json(KB_DIR / "property.json")

    classif = classify(email)

    # AI-disclosure short-circuit (overrides per-category draft)
    if _disclosure_check(email):
        name = _first_name(email["from"])
        draft = {
            "to": _to_address(email["from"]),
            "subject": f"Re: {email['subject']}",
            "body": (
                f"Good question — I'm an AI assistant working with Shaw to manage the property. "
                f"He's still the decision-maker on anything substantive; I just help keep things organized "
                f"and make sure nothing falls through the cracks. Happy to loop him in directly anytime.\n\n"
                f"{SIG}"
            ),
            "send_only_after": "shaw_approval",
        }
    else:
        # Try LLM drafting first, fall back to template on failure
        draft = None
        if not force_template:
            try:
                from llm_drafter import draft_with_claude
                draft = draft_with_claude(email, classif)
            except Exception as e:
                print(f"warn: LLM draft failed, using template: {e}", file=sys.stderr)
        if draft is None:
            key = (classif["category"], classif["subcategory"])
            gen = DRAFT_DISPATCH.get(key) or DRAFT_DISPATCH[("escalation_only", "unclassified")]
            draft = gen(email, kb)

    cover = cover_note(email, classif, kb)
    updates = state_updates(email, classif)
    state_written = apply_state_updates(email, classif, updates, kb)
    files = write_draft_files(email, classif, draft, cover, state_written)

    log = {
        "ts": datetime.datetime.now().isoformat(),
        "thread_id": email["thread_id"],
        "classification": classif,
        "files": files,
        "ai_disclosure_triggered": _disclosure_check(email),
    }
    log_path = LOG_DIR / "pipeline.log.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(log) + "\n")

    return {
        "classification": classif,
        "draft_to_tenant": draft,
        "cover_note_to_shaw": cover,
        "state_changes": state_written,
        "files": files,
        "ai_disclosure_triggered": _disclosure_check(email),
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: ari_pipeline.py <fixture.json> [<fixture.json> ...]", file=sys.stderr)
        sys.exit(2)
    for arg in sys.argv[1:]:
        email = _load_json(pathlib.Path(arg))
        result = process_email(email)
        print(f"\n===== {arg} =====")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
