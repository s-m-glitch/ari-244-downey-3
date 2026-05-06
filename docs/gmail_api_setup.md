# Gmail API Setup — One-Time

Goal: get a `credentials.json` from Google Cloud and run `auth_setup.py` once. After that, Ari runs headless against `244downeyapt3@gmail.com`.

## 1. Create a Google Cloud project (~2 min)

1. Go to https://console.cloud.google.com/projectcreate
2. Project name: `ari-244-downey` (or whatever)
3. Click **Create**.

## 2. Enable the Gmail API (~30s)

1. With your new project selected, go to https://console.cloud.google.com/apis/library/gmail.googleapis.com
2. Click **Enable**.

## 3. Configure the OAuth consent screen (~1 min)

1. Go to https://console.cloud.google.com/apis/credentials/consent
2. User Type: **External** → Create.
3. App information:
   - App name: `Ari` (or anything)
   - User support email: your email
   - Developer email: your email
   - Click Save and Continue through the rest with defaults.
4. **Scopes**: skip (the scopes are requested by the script at runtime).
5. **Test users**: click **Add Users** → add `244downeyapt3@gmail.com`. Save.

(You can leave the app in "Testing" mode forever for personal use — it's only an issue if you wanted to share it publicly.)

## 4. Create OAuth client credentials (~1 min)

1. Go to https://console.cloud.google.com/apis/credentials
2. **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: `Ari local`
5. Click **Create**.
6. In the popup, click **Download JSON**. Save it as `credentials.json`.

## 5. Drop the credentials in place + run auth setup

```bash
cd path/to/ari
mkdir -p secrets
mv ~/Downloads/credentials.json secrets/credentials.json

pip install -r requirements.txt --break-system-packages   # or use a venv
python3 scripts/auth_setup.py
```

The script will open a browser. **Sign in as `244downeyapt3@gmail.com`** (NOT your personal account) and click through the consent screen. You'll see a "Google hasn't verified this app" warning — click **Advanced** → **Go to Ari (unsafe)**. (It's safe; it's your own app.)

When the browser shows "The authentication flow has completed," close the tab.

## 6. Verify

```bash
python3 scripts/gmail_client.py whoami
```

You should see something like:

```json
{
  "emailAddress": "244downeyapt3@gmail.com",
  "messagesTotal": ...,
  "threadsTotal": ...,
  ...
}
```

If it says `244downeyapt3@gmail.com` you're good. If it says any other address, you signed into the wrong account during step 5 — delete `secrets/token.json` and re-run `auth_setup.py`.

## 7. First poll

```bash
python3 scripts/run_poll.py --dry-run
```

This pulls real threads but doesn't create any drafts or send cover notes. Once you're confident:

```bash
python3 scripts/run_poll.py
```

Drafts land in `244downeyapt3@`'s Drafts folder, cover notes to `mckean.shaw@gmail.com`.

## Wiring the schedule

You have three options once `run_poll.py` works manually:

**A. Cowork scheduled task** (already wired — just update its prompt to call this script). Easiest, but tied to your Cowork session.

**B. Mac launchd** (true headless, runs whenever your Mac is on):

```bash
cp infra/com.shaw.ari.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.shaw.ari.plist
```

(plist file isn't generated yet — say the word and I'll add it.)

**C. GitHub Actions on a 30-min cron** (truly headless, free for our usage). Requires putting the OAuth token in a GitHub secret, which is fine for a private repo.

**D. Cheap VPS / Cloud Run job**. Overkill for v0.

## Security notes

- `secrets/credentials.json` and `secrets/token.json` are gitignored — never commit them.
- The OAuth token grants read + compose + send for `244downeyapt3@gmail.com` only. If it leaks, revoke at https://myaccount.google.com/permissions.
- Scopes used: `gmail.readonly`, `gmail.compose`, `gmail.send`. Compose is for tenant drafts, send is for cover notes to Shaw.
