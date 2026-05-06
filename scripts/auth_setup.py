#!/usr/bin/env python3
"""
One-time OAuth setup. Run once after dropping credentials.json into ari/secrets/.

  python3 scripts/auth_setup.py

This opens a browser, you sign in as 244downeyapt3@gmail.com, authorize the
requested scopes, and the refresh token gets saved to secrets/token.json.

After this, the rest of Ari runs headless.
"""

from gmail_client import run_oauth_flow

if __name__ == "__main__":
    run_oauth_flow()
    print("Done. Test with:  python3 scripts/gmail_client.py whoami")
