# LLM-Backed Drafting Setup

By default, Ari uses Claude to write tenant-facing drafts. Classification stays deterministic (fast, predictable); only the reply body comes from the model now. If the API call fails for any reason, the pipeline falls back to the v0 template — so this is non-breaking.

## One-time setup

### 1. Get an API key

1. Go to https://console.anthropic.com.
2. Sign in (or sign up).
3. **Settings → API Keys → Create Key**. Copy the key (starts with `sk-ant-...`).
4. Save it somewhere safe — you can't view it again after closing the dialog.

### 2. Set it as an environment variable

For one-off runs:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 scripts/run_poll.py
```

For persistent (recommended), add to your shell profile:
```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
source ~/.zshrc
```

### 3. Install the SDK if you haven't already

```bash
pip3 install -r requirements.txt
```

### 4. Test

```bash
python3 scripts/llm_drafter.py tests/fixtures/email_blinds.json
```

Should print a JSON object with `classification` (deterministic) and `draft` (Claude-generated). Compare the body against the §8 voice anchor — if it reads close to "thanks for flagging this and for the photos, looks like a clean failure..." you're set.

## Cost expectations

- Each tenant email = ~1 API call. Input is ~5K tokens (system prompt + KB + email), output ~300 tokens.
- With **Claude Sonnet** (default, best quality): ~$0.02 per email.
- With **Claude Haiku** (cheaper, slightly flatter voice): ~$0.005 per email.
- Mareika's traffic is roughly 2-10 emails/month → effectively free. Even a chattier tenant tops out at a few dollars/month.

To switch models, change `DEFAULT_MODEL` in `scripts/llm_drafter.py`.

## Falling back to template mode

If you want to test offline or compare voices, force the deterministic path:

```python
from ari_pipeline import process_email
result = process_email(email, force_template=True)
```

Or just unset `ANTHROPIC_API_KEY` — the pipeline will warn once and use templates for the rest of the run.

## How it works

`scripts/llm_drafter.py` builds a single user-turn prompt containing:
- The pre-determined classification (so Claude doesn't re-classify; consistency).
- The inbound email (from, subject, body, attachments-by-filename).
- A compact JSON of the KB: property facts, tenant context, rent calendar, open tickets, granted permissions.

System prompt is `prompts/system_prompt.md` verbatim — same persona/policy contract that defines Ari. Claude responds with `{subject, body}`; the pipeline wraps it with the standard `to`/`send_only_after` fields.

Hard rules (no AB 1482 references, no nonexistent attachment mentions, etc.) are enforced in the user prompt's "Hard rules" section. Empirically Claude follows these reliably. If you want belt-and-suspenders, add post-generation regex checks in `llm_drafter.py`.

## Troubleshooting

- **`anthropic SDK not installed`** → `pip3 install -r requirements.txt`
- **`ANTHROPIC_API_KEY not set`** → export the env var as above
- **`anthropic API call failed`** → check your API key is valid and you have credits at console.anthropic.com
- **`could not parse JSON from Claude response`** → rare; pipeline falls back to template. If it persists, check `logs/poll.jsonl` for the raw response.
