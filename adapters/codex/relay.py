"""Host subscription relay targeting Codex CLI 0.161.0.

The package target is declared and checked in adapters/codex/default.nix.

Depends on Codex internals, not a public API:
  - inference: POST https://chatgpt.com/backend-api/codex/responses with
    `Authorization: Bearer <access_token>`, `ChatGPT-Account-ID`, `originator`
  - refresh: POST https://auth.openai.com/oauth/token with Codex's public
    OAuth client id and the stored refresh token (tokens rotate)
  - login file: $CODEX_HOME/auth.json
    {auth_mode, OPENAI_API_KEY, tokens{id_token, access_token, refresh_token,
    account_id}, last_refresh}
Changed upstream paths return 404; refresh rejection returns 502 and requires
`koi login codex`. Vendor protocol handling belongs to this adapter.
"""

import base64
import json
import math
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

from .common import locked, read_private, token, write_private

NAME = "codex"
UPSTREAM = "chatgpt.com"
UPSTREAM_PREFIX = "/backend-api/codex"
PATHS = {"/responses"}
REFRESHABLE = True

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"


def _home(state):
    return state / "codex"


def _expiry(jwt):
    parts = jwt.split('.')
    if len(parts) != 3:
        raise ValueError('Invalid access token')
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    expiry = json.loads(base64.urlsafe_b64decode(payload))["exp"]
    if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
        raise ValueError('Invalid access token expiry')
    return expiry


def _refresh(path, auth):
    request = urllib.request.Request(
        TOKEN_URL,
        json.dumps(
            {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": auth["tokens"]["refresh_token"],
                "scope": "openid profile email",
            }
        ).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        fresh = json.load(response)
    for key in ("id_token", "access_token", "refresh_token"):
        if fresh.get(key):
            auth["tokens"][key] = fresh[key]
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    validate(auth)
    write_private(path, json.dumps(auth, indent=2) + '\n')


def validate(auth):
    if not isinstance(auth, dict) or not isinstance(auth.get('tokens'), dict):
        raise ValueError('Invalid Codex login')
    tokens = auth['tokens']
    for name in ('access_token', 'refresh_token', 'account_id'):
        token(tokens[name])
    _expiry(tokens['access_token'])
    return auth


def headers(state, incoming, force=False):
    path = _home(state) / "auth.json"
    with locked(state):
        try:
            auth = validate(json.loads(read_private(path)))
        except FileNotFoundError:
            raise RuntimeError("no host codex login; run `koi login codex`") from None
        tokens = auth["tokens"]
        if force or _expiry(tokens["access_token"]) - time.time() < 300:
            try:
                _refresh(path, auth)
            except OSError as error:
                raise RuntimeError(
                    "codex token refresh failed; run `koi login codex`"
                ) from None
    return {
        **incoming,
        "Authorization": "Bearer " + tokens["access_token"],
        "ChatGPT-Account-ID": tokens["account_id"],
        "originator": "codex_cli_rs",
    }


def login(state, options=()):
    home = _home(state)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {**os.environ, "CODEX_HOME": str(home)}
    subprocess.run(["codex", "login", *options, '-c', 'cli_auth_credentials_store="file"'], env=env, check=True)
