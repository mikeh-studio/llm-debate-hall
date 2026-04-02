from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PACKAGE_ROOT / "static"
BUILTIN_ICON_ROUTE_PREFIX = "/static/assets/persona-icons/builtin"
CUSTOM_ICON_ROUTE_PREFIX = "/persona-icons"
GENERATED_ICON_DIR_NAME = "_persona_icons"

BUILTIN_ICON_PATHS = {
    "humanist_mediator": f"{BUILTIN_ICON_ROUTE_PREFIX}/humanist-mediator.svg",
    "nietzschean_iconoclast": f"{BUILTIN_ICON_ROUTE_PREFIX}/nietzschean-iconoclast.svg",
    "pragmatic_engineer": f"{BUILTIN_ICON_ROUTE_PREFIX}/pragmatic-engineer.svg",
    "skeptical_historian": f"{BUILTIN_ICON_ROUTE_PREFIX}/skeptical-historian.svg",
    "stoic_rationalist": f"{BUILTIN_ICON_ROUTE_PREFIX}/stoic-rationalist.svg",
    "utilitarian_analyst": f"{BUILTIN_ICON_ROUTE_PREFIX}/utilitarian-analyst.svg",
}
FALLBACK_ICON_PATH = f"{BUILTIN_ICON_ROUTE_PREFIX}/fallback.svg"

PALETTES = [
    ("#f4e7bf", "#d96c3f", "#6e2f2a", "#22151c"),
    ("#d8f2ea", "#4f9f8c", "#245d56", "#162124"),
    ("#f0d4f9", "#9155b8", "#54306d", "#21172f"),
    ("#ffe2c0", "#dd8e34", "#8a4f18", "#2c1a10"),
    ("#d7e5ff", "#4c73cc", "#243d82", "#172033"),
    ("#f5d2d2", "#bb4f66", "#6d2434", "#26151c"),
]


def generated_persona_icons_dir(root: str | Path) -> Path:
    return Path(root) / GENERATED_ICON_DIR_NAME


def builtin_persona_icon_path(persona_id: str) -> str:
    return BUILTIN_ICON_PATHS.get(persona_id, FALLBACK_ICON_PATH)


def _rect(x: int, y: int, fill: str, *, size: int = 10) -> str:
    return f"<rect x='{x * size}' y='{y * size}' width='{size}' height='{size}' fill='{fill}'/>"


def _pixel_grid(points: list[tuple[int, int]], fill: str, *, size: int = 10) -> str:
    return "".join(_rect(x, y, fill, size=size) for x, y in points)


def _mirror(points: list[tuple[int, int]], axis: int = 7) -> list[tuple[int, int]]:
    mirrored = set(points)
    for x, y in points:
        mirrored.add((axis * 2 - x, y))
    return sorted(mirrored)


def _hash_persona_seed(persona: dict[str, Any]) -> bytes:
    material = "|".join(
        [
            str(persona.get("id", "")),
            str(persona.get("name", "")),
            str(persona.get("philosophy_family", "")),
            str(persona.get("style", "")),
            ",".join(persona.get("core_values", [])),
            ",".join(persona.get("debate_rules", [])),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def _custom_icon_svg(persona: dict[str, Any]) -> str:
    seed = _hash_persona_seed(persona)
    cream, primary, accent, ink = PALETTES[seed[0] % len(PALETTES)]
    horn_styles = [
        [(5, 2), (6, 1), (7, 2)],
        [(4, 2), (5, 1), (6, 2)],
        [(5, 3), (6, 2), (7, 1)],
    ]
    ear_styles = [
        [(4, 4), (3, 5), (4, 5)],
        [(5, 4), (4, 5)],
        [(4, 4), (3, 4), (4, 5)],
    ]
    tail_styles = [
        [(10, 10), (11, 11), (12, 12)],
        [(11, 10), (12, 10), (13, 11)],
        [(10, 11), (11, 12), (12, 12)],
    ]
    eye_styles = [
        [(6, 7), (8, 7)],
        [(6, 7), (7, 7), (8, 7)],
        [(6, 7), (8, 7), (7, 8)],
    ]
    crest_styles = [
        [(7, 3), (7, 4)],
        [(6, 3), (7, 3), (8, 3)],
        [(7, 3), (6, 4), (8, 4)],
    ]
    horn = horn_styles[seed[1] % len(horn_styles)]
    ears = ear_styles[seed[2] % len(ear_styles)]
    tail = tail_styles[seed[3] % len(tail_styles)]
    eyes = eye_styles[seed[4] % len(eye_styles)]
    crest = crest_styles[seed[5] % len(crest_styles)]

    body_left = [
        (5, 5),
        (6, 5),
        (5, 6),
        (6, 6),
        (4, 7),
        (5, 7),
        (6, 7),
        (4, 8),
        (5, 8),
        (6, 8),
        (4, 9),
        (5, 9),
        (6, 9),
        (5, 10),
        (6, 10),
        (5, 11),
    ]
    body = _mirror(body_left)
    outline_left = [
        (5, 4),
        (4, 5),
        (4, 6),
        (3, 7),
        (3, 8),
        (3, 9),
        (4, 10),
        (4, 11),
        (5, 12),
        (6, 12),
    ]
    outline = _mirror(outline_left)
    belly = _mirror([(6, 8), (6, 9), (6, 10)])
    feet = _mirror([(5, 12), (6, 12)])

    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 160 160' shape-rendering='crispEdges'>
  <rect width='160' height='160' rx='16' fill='{cream}'/>
  <rect x='10' y='10' width='140' height='140' rx='12' fill='rgba(255,255,255,0.16)' stroke='{ink}' stroke-width='4'/>
  {_pixel_grid([(3, 2), (12, 3), (2, 12), (13, 11)], 'rgba(255,255,255,0.25)')}
  {_pixel_grid(outline, ink)}
  {_pixel_grid(body, primary)}
  {_pixel_grid(belly, cream)}
  {_pixel_grid(_mirror(horn), accent)}
  {_pixel_grid(_mirror(ears), accent)}
  {_pixel_grid(_mirror(tail), accent)}
  {_pixel_grid(crest, accent)}
  {_pixel_grid(eyes, ink)}
  {_pixel_grid([(7, 9), (8, 9)], accent)}
  {_pixel_grid(feet, ink)}
  <rect x='25' y='132' width='110' height='8' fill='rgba(34,21,28,0.12)'/>
</svg>"""
    return svg


def ensure_persona_icon(persona: dict[str, Any], icon_dir: Path) -> tuple[str, str]:
    existing_path = str(persona.get("icon_path") or "").strip()
    existing_style = str(persona.get("icon_style_tag") or "").strip()

    if persona.get("is_builtin"):
        return builtin_persona_icon_path(persona["id"]), existing_style or "builtin-pixel-creature"

    if existing_path.startswith(CUSTOM_ICON_ROUTE_PREFIX):
        icon_file = icon_dir / Path(existing_path).name
        if icon_file.exists():
            return existing_path, existing_style or "generated-pixel-creature"

    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_file = icon_dir / f"{persona['id']}.svg"
    try:
        icon_file.write_text(_custom_icon_svg(persona), encoding="utf-8")
        return f"{CUSTOM_ICON_ROUTE_PREFIX}/{icon_file.name}", existing_style or "generated-pixel-creature"
    except OSError:
        return FALLBACK_ICON_PATH, "fallback-pixel-creature"
