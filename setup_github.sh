#!/usr/bin/env bash
#
# One-shot script to push Ari to a private GitHub repo.
# Run this from your local terminal — not from inside the Claude session.
#
# Usage:
#   cd "/Users/shawmckean/Library/Application Support/Claude/local-agent-mode-sessions/c2b52b70-07ff-4e51-bbe9-06d85fbb65aa/bbccdb81-323d-4804-8bf4-8c153348d986/local_9a736697-c39a-4bd5-a778-5dee8c3fd04e/outputs/ari"
#   chmod +x setup_github.sh
#   ./setup_github.sh
#
# Requires: git. Optional: gh (GitHub CLI) — if installed, the repo is created
# automatically; otherwise you'll get instructions to create it manually.

set -euo pipefail

REPO_NAME="ari-244-downey-3"
REPO_DESC="Ari — Tenant Inbox Agent for 244 Downey St #3 (private)"

# 1. Clean up any half-finished git state from the sandbox
if [ -d .git ]; then
  echo "Found existing .git/ — removing to start clean..."
  rm -rf .git
fi

# 2. Init fresh
git init -b main
git config user.name "Shaw McKean"
git config user.email "sm@getbixby.com"

# 3. First commit
git add -A
git commit -m "Initial commit: Ari v0 — Tenant Inbox Agent for 244 Downey #3

- System prompt: persona, voice, §6 categories, §7 escalation, §8 voice anchors,
  §9 rent framing, hard guardrails.
- Knowledge base: structured property facts plus extracted lease text and
  partial CC&R OCR.
- State files: tickets, payment ledger, permissions, rent calendar, HOA log,
  tenant context — schema-only baseline.
- Pipeline (scripts/ari_pipeline.py): deterministic v0 — classify → draft →
  cover note → state update. LLM-backed v1 swaps the same boundary.
- Runbook: poll loop the scheduled task executes every 30 min.
- Test fixtures: seven emails covering every §6 category."

# 4. Create the private GitHub repo + push
if command -v gh >/dev/null 2>&1; then
  if ! gh auth status >/dev/null 2>&1; then
    echo ""
    echo "GitHub CLI is installed but not authenticated. Run:"
    echo "  gh auth login"
    echo "Then re-run this script."
    exit 1
  fi
  echo ""
  echo "Creating private GitHub repo via gh..."
  gh repo create "$REPO_NAME" --private --description "$REPO_DESC" --source=. --remote=origin --push
  echo ""
  echo "✓ Pushed to: $(gh repo view --json url -q .url)"
else
  echo ""
  echo "GitHub CLI (gh) not installed. Two options to finish:"
  echo ""
  echo "OPTION A — Install gh, then re-run this script:"
  echo "  brew install gh && gh auth login"
  echo ""
  echo "OPTION B — Create the repo manually at https://github.com/new"
  echo "  - Name: $REPO_NAME"
  echo "  - Visibility: Private"
  echo "  - Don't initialize with README, .gitignore, or license (we have these)"
  echo "  Then run:"
  echo "    git remote add origin git@github.com:<your-username>/$REPO_NAME.git"
  echo "    git push -u origin main"
fi
