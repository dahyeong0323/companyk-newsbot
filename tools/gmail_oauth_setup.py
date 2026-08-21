"""One-time local OAuth helper for the Gmail delivery adapter.

Run this only from a trusted local machine while signed in as the dedicated
newsbot Gmail account.  It never writes a token inside this repository.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a local Gmail send refresh-token file.")
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="An explicit path outside this repository for the sensitive refresh-token JSON.",
    )
    return parser.parse_args()


def _environment_client_config() -> dict[str, dict[str, object]]:
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in the local environment")
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1"],
        }
    }


def main() -> int:
    args = _parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    output = args.output.expanduser().resolve()
    if output == repository_root or repository_root in output.parents:
        raise RuntimeError("--output must be outside the repository to prevent accidental commits")
    if output.exists():
        raise RuntimeError("refusing to overwrite an existing sensitive token file")
    output.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_config(_environment_client_config(), scopes=[GMAIL_SEND_SCOPE])
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    if not credentials.refresh_token:
        raise RuntimeError("OAuth consent did not return a refresh token; revoke prior consent and try again")
    output.write_text(json.dumps({"refresh_token": credentials.refresh_token}) + "\n", encoding="utf-8")
    print(f"Refresh token saved to sensitive local file: {output}")
    print("Do not commit this file. Transfer its value only through approved secret storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
