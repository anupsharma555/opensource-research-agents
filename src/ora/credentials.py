"""Resolve nonsecret Groq credential profiles and their shared quota domains.

Profiles select fixed environment variable names; they never contain keys.
The default profiles share the existing quota ledger. A trusted local mapping
may declare verified quota domains, which each research attempt must pin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

GROQ_CREDENTIAL_ENV_NAMES = {
    "primary": "GROQ_API_KEY",
    "backup": "GROQ_API_KEY_BACKUP",
}
LEGACY_QUOTA_DOMAIN = "groq-selected"
PROFILE_CONFIG_PATH = ".local/groq-credential-profiles.json"


class CredentialProfileError(ValueError):
    """A nonsecret profile is unavailable or disagrees with its saved binding."""


def validate_selection(profile_id: object, quota_domain: object) -> None:
    if not isinstance(profile_id, str) or profile_id not in GROQ_CREDENTIAL_ENV_NAMES:
        raise CredentialProfileError("Choose the primary or backup Groq credential profile")
    if not isinstance(quota_domain, str) or not re.fullmatch(r"groq-[a-z0-9_-]{1,58}", quota_domain):
        raise CredentialProfileError("Groq quota domain must be a nonsecret groq-prefixed label")


class GroqCredentialProfiles:
    """Read trusted profile configuration once without reading any API keys."""

    def __init__(self, root: Path) -> None:
        self._domains = dict.fromkeys(GROQ_CREDENTIAL_ENV_NAMES, LEGACY_QUOTA_DOMAIN)
        self._verified_domains = dict.fromkeys(GROQ_CREDENTIAL_ENV_NAMES, False)
        self._enabled = dict.fromkeys(GROQ_CREDENTIAL_ENV_NAMES, True)
        self._allowed_runs: dict[str, frozenset[str] | None] = dict.fromkeys(
            GROQ_CREDENTIAL_ENV_NAMES, None
        )
        path = Path(root) / PROFILE_CONFIG_PATH
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as exc:
            raise CredentialProfileError("Cannot read local Groq profile configuration") from exc
        try:
            config = json.loads(raw)
            if (not isinstance(config, dict) or set(config) != {"version", "profiles"}
                    or type(config["version"]) is not int or config["version"] != 1
                    or not isinstance(config["profiles"], dict)):
                raise ValueError
            for profile_id, entry in config["profiles"].items():
                if (profile_id not in GROQ_CREDENTIAL_ENV_NAMES
                        or not isinstance(entry, dict) or not entry
                        or set(entry) - {"quota_domain", "quota_domain_verified", "enabled", "allowed_run_ids"}):
                    raise ValueError
                if "allowed_run_ids" in entry:
                    runs = entry["allowed_run_ids"]
                    if (not isinstance(runs, list)
                            or any(not isinstance(run, str) or not re.fullmatch(
                                r"run-[A-Za-z0-9_-]{1,156}", run
                            ) for run in runs)
                            or len(set(runs)) != len(runs)):
                        raise ValueError
                    self._allowed_runs[profile_id] = frozenset(runs)
                if "enabled" in entry:
                    if type(entry["enabled"]) is not bool:
                        raise ValueError
                    self._enabled[profile_id] = entry["enabled"]
                if "quota_domain" in entry or "quota_domain_verified" in entry:
                    if entry.get("quota_domain_verified") is not True or "quota_domain" not in entry:
                        raise ValueError
                    validate_selection(profile_id, entry["quota_domain"])
                    self._domains[profile_id] = entry["quota_domain"]
                    self._verified_domains[profile_id] = True
        except (ValueError, TypeError) as exc:
            raise CredentialProfileError(
                "Invalid Groq profile configuration; check enabled, allowed_run_ids and verified domains"
            ) from exc

    def selection(self, profile_id: str = "primary") -> dict[str, str]:
        if profile_id not in self._domains:
            raise CredentialProfileError("Choose the primary or backup Groq credential profile")
        return {"credential_profile": profile_id, "quota_domain": self._domains[profile_id]}

    def scopes(self) -> tuple[str, ...]:
        # Preserve the historical ledger even after an explicit mapping change.
        return tuple(sorted({LEGACY_QUOTA_DOMAIN, *self._domains.values()}))

    def verified_backup_transition(self) -> dict[str, str] | None:
        """Describe an explicitly declared split; never infer one from two keys."""
        backup = self._domains["backup"]
        if (
            self._domains["primary"] != LEGACY_QUOTA_DOMAIN
            or not self._verified_domains["backup"]
            or backup == LEGACY_QUOTA_DOMAIN
        ):
            return None
        return {"from_scope": LEGACY_QUOTA_DOMAIN, "to_scope": backup}

    def is_enabled(self, profile_id: str) -> bool:
        """Return the operator's startup selection without changing quota history."""
        if profile_id not in self._enabled:
            raise CredentialProfileError("Choose the primary or backup Groq credential profile")
        return self._enabled[profile_id]

    def validate_binding(self, profile_id: str, quota_domain: str) -> None:
        validate_selection(profile_id, quota_domain)
        if self.selection(profile_id)["quota_domain"] != quota_domain:
            raise CredentialProfileError(
                "Saved Groq credential profile and configured quota domain disagree"
            )

    def is_run_enabled(self, profile_id: str, run_id: str) -> bool:
        """Admit only operator-selected runs when a startup selection is present."""
        if not self.is_enabled(profile_id):
            return False
        allowed = self._allowed_runs[profile_id]
        return allowed is None or run_id in allowed
