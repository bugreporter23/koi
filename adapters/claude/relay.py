"""Host subscription relay targeting Claude Code 2.1.292.

The package target is declared and checked in adapters/claude/default.nix.

Depends on:
  - Claude Code honoring ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN from its
    settings.json `env` (documented)
  - `claude setup-token` subscription tokens (sk-ant-oat...) being accepted as
    `Authorization: Bearer` on /v1/messages when `anthropic-beta` includes
    `oauth-2025-04-20` (undocumented)
  - setup-token lifetime of about a year: no refresh logic here
Upstream 401/400 returns 502 and requires `koi login claude`. Vendor protocol
handling belongs to this adapter.
"""

import re
import subprocess

from .common import read_private, token, write_private

NAME = "claude"
UPSTREAM = "api.anthropic.com"
UPSTREAM_PREFIX = ""
PATHS = {"/v1/messages", "/v1/messages/count_tokens"}
REFRESHABLE = False
OAUTH_BETA = "oauth-2025-04-20"


def headers(state, incoming, force=False):
    try:
        credential = token(read_private(state / "claude-token").strip())
    except FileNotFoundError:
        raise RuntimeError("no host claude token; run `koi login claude`") from None
    outgoing = dict(incoming)
    if credential.startswith("sk-ant-api"):
        outgoing["x-api-key"] = credential
        return outgoing
    betas = [b.strip() for b in outgoing.pop("anthropic-beta", "").split(",") if b.strip()]
    if OAUTH_BETA not in betas:
        betas.append(OAUTH_BETA)
    outgoing["anthropic-beta"] = ",".join(betas)
    outgoing["Authorization"] = "Bearer " + credential
    return outgoing


def login(state, options=()):
    print("Generating a long-lived subscription token on the host...")
    import os
    home = state / 'claude'
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(["claude", "setup-token", *options], env={**os.environ, 'CLAUDE_CONFIG_DIR': str(home)}, check=True)
    print("Paste the token printed above (it may span lines), then an empty line:")
    lines = []
    while (line := input()).strip():
        lines.append(line)
    joined = "".join(re.sub(r"[^A-Za-z0-9_-]", "", line) for line in lines)
    match = re.search(r"sk-ant-[A-Za-z0-9_-]{80,}", joined)
    if not match:
        raise RuntimeError("no complete Claude token found in the paste")
    credential = token(match.group(0))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "claude-token"
    write_private(path, credential + "\n")
