"""Settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    provider: str = field(default_factory=lambda: _env("AGENTKIT_PROVIDER", "auto"))
    model: str = field(default_factory=lambda: _env("AGENTKIT_MODEL", ""))
    effort: str = field(default_factory=lambda: _env("AGENTKIT_EFFORT", "medium"))
    fallbacks: bool = field(
        default_factory=lambda: _env("AGENTKIT_FALLBACKS", "0").lower() in {"1", "true", "yes"}
    )
    db_path: Path = field(default_factory=lambda: Path(_env("AGENTKIT_DB", "data/agentkit.db")))
    policy_path: Path = field(
        default_factory=lambda: Path(_env("AGENTKIT_POLICY", "policies/default.yaml"))
    )
    max_steps: int = field(default_factory=lambda: int(_env("AGENTKIT_MAX_STEPS", "8")))
    max_run_tokens: int = field(
        default_factory=lambda: int(_env("AGENTKIT_MAX_RUN_TOKENS", "60000"))
    )
    daily_user_tokens: int = field(
        default_factory=lambda: int(_env("AGENTKIT_DAILY_USER_TOKENS", "500000"))
    )


def get_settings() -> Settings:
    return Settings()
