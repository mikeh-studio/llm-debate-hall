from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm_debate_hall.models import PersonaModel


PACKAGE_ROOT = Path(__file__).resolve().parent
BUILTIN_DIR_NAME = "builtin"
CUSTOM_DIR_NAME = "custom"


def personas_root(root: str | Path | None = None) -> Path:
    return Path(root) if root is not None else PACKAGE_ROOT


def builtin_personas_dir(root: str | Path | None = None) -> Path:
    return personas_root(root) / BUILTIN_DIR_NAME


def custom_personas_dir(root: str | Path | None = None) -> Path:
    return personas_root(root) / CUSTOM_DIR_NAME


def load_persona_payload(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_builtin_personas(root: str | Path | None = None) -> list[PersonaModel]:
    personas = [
        PersonaModel.model_validate(load_persona_payload(path))
        for path in sorted(builtin_personas_dir(root).glob("*.json"))
    ]
    return sorted(personas, key=lambda persona: persona.name.lower())


BUILTIN_PERSONAS = load_builtin_personas()
