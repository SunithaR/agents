"""Anthropic credential introspection.

This module never authenticates anything itself -- the Anthropic SDK's
zero-arg `AsyncAnthropic()` client (used in llm_client.py) resolves real
credentials on its own at request time, in this order:

    1. ANTHROPIC_API_KEY env var
    2. ANTHROPIC_AUTH_TOKEN env var
    3. The active `ant auth login` profile (or the one named by
       ANTHROPIC_PROFILE)
    4. Workload Identity Federation env vars

So the platform never *requires* a static API key -- running `ant auth
login` once on the machine (or in CI) is sufficient, and nothing in this
codebase needs to change to support it.

What this module *does* do is answer "which of those will fire?" so we can
log something useful at startup and expose it via GET /health/llm, instead
of a developer discovering a missing credential only when an upgrade
request fails.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

_WIF_REQUIRED_VARS = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)


@dataclass
class CredentialStatus:
    source: str  # "api_key" | "auth_token" | "ant_cli" | "workload_identity_federation" | "none"
    detail: str  # human-readable, safe to log/expose -- never contains a secret value
    configured: bool


def _check_ant_cli_profile() -> CredentialStatus | None:
    """Ask the `ant` CLI which profile/credential source is active, if the
    CLI is installed. `ant auth status` reports status only -- it never
    prints the secret itself -- so its stdout is safe to surface directly."""
    ant_path = shutil.which("ant")
    if not ant_path:
        return None
    try:
        result = subprocess.run(
            [ant_path, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = (result.stdout or "").strip()
    if result.returncode == 0 and output and "no active" not in output.lower():
        return CredentialStatus(source="ant_cli", detail=output, configured=True)
    return None


def describe_credentials() -> CredentialStatus:
    """Best-effort, side-effect-free description of which Anthropic
    credential source is currently active. Safe to log or return from an
    API endpoint -- it reports *which mechanism* is configured, never the
    secret value itself."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CredentialStatus(
            source="api_key", detail="ANTHROPIC_API_KEY environment variable", configured=True
        )
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return CredentialStatus(
            source="auth_token", detail="ANTHROPIC_AUTH_TOKEN environment variable", configured=True
        )

    ant_status = _check_ant_cli_profile()
    if ant_status is not None:
        return ant_status

    has_wif = all(os.environ.get(var) for var in _WIF_REQUIRED_VARS) and (
        os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE") or os.environ.get("ANTHROPIC_IDENTITY_TOKEN")
    )
    if has_wif:
        return CredentialStatus(
            source="workload_identity_federation",
            detail="Workload Identity Federation environment variables",
            configured=True,
        )

    return CredentialStatus(
        source="none",
        detail=(
            "No Anthropic credentials detected. Run `ant auth login` (recommended), "
            "or set ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN."
        ),
        configured=False,
    )
