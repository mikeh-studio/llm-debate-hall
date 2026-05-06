from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Multi-Agent Council"
APP_SLUG = "multi-agent-council"
DEFAULT_DB_FILENAME = "multi_agent_council.db"
LEGACY_DB_FILENAME = "llm_debate_hall.db"
ENV_PREFIX = "MULTI_AGENT_COUNCIL"
LEGACY_ENV_PREFIX = "LLM_DEBATE_HALL"


def env_value(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}_{name}") or os.environ.get(f"{LEGACY_ENV_PREFIX}_{name}") or default


def env_flag(name: str) -> bool:
    return str(env_value(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def default_db_path(project_root: Path) -> str:
    preferred = project_root / DEFAULT_DB_FILENAME
    legacy = project_root / LEGACY_DB_FILENAME
    if not preferred.exists() and legacy.exists():
        return str(legacy)
    return str(preferred)
