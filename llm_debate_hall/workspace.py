from __future__ import annotations

from dataclasses import dataclass


DEBATE_MODES = {"conversational", "theater", "serious"}
DEFAULT_DEBATE_MODE = "serious"

TOPIC_TYPES = [
    "AI & Technology",
    "Policy",
    "Ethics",
    "Product",
    "Science",
    "Culture",
    "Other",
]
DEFAULT_TOPIC_TYPE = "Other"

SENTIMENTS = [
    "affirming",
    "opposing",
    "skeptical",
    "exploratory",
    "mediating",
]
DEFAULT_SENTIMENT = "exploratory"


@dataclass(frozen=True)
class WorkspaceAgentSuggestion:
    display_name: str
    sentiment: str


def normalize_debate_mode(value: str | None) -> str:
    normalized = (value or DEFAULT_DEBATE_MODE).strip().lower()
    return normalized if normalized in DEBATE_MODES else DEFAULT_DEBATE_MODE


def normalize_topic_type(value: str | None) -> str:
    normalized = (value or DEFAULT_TOPIC_TYPE).strip()
    by_lower = {item.lower(): item for item in TOPIC_TYPES}
    return by_lower.get(normalized.lower(), DEFAULT_TOPIC_TYPE)


def normalize_topic_tags(values: list[str] | None) -> list[str]:
    if not values:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = str(value).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag[:48])
    return tags[:8]


def normalize_sentiment(value: str | None) -> str:
    normalized = (value or DEFAULT_SENTIMENT).strip().lower()
    return normalized if normalized in SENTIMENTS else DEFAULT_SENTIMENT


def suggest_topic_type(topic: str) -> str:
    text = topic.lower()
    keyword_map = [
        ("AI & Technology", ["ai", "agent", "llm", "model", "software", "coding", "automation", "robot"]),
        ("Policy", ["law", "regulation", "government", "policy", "election", "tax", "public"]),
        ("Ethics", ["should", "moral", "ethical", "rights", "fair", "harm", "safety", "deception"]),
        ("Product", ["product", "startup", "market", "customer", "feature", "pricing", "growth"]),
        ("Science", ["science", "research", "study", "climate", "biology", "physics", "medicine"]),
        ("Culture", ["culture", "art", "media", "school", "education", "society", "work"]),
    ]
    for topic_type, keywords in keyword_map:
        if any(keyword in text for keyword in keywords):
            return topic_type
    return DEFAULT_TOPIC_TYPE


def suggest_sentiment(*, side: str | None = None, persona_id: str | None = None, index: int = 0) -> str:
    side_text = (side or "").lower()
    persona_text = (persona_id or "").lower()
    if side_text in {"pro", "for", "affirmative"}:
        return "affirming"
    if side_text in {"con", "against", "negative"}:
        return "opposing"
    if any(token in persona_text for token in ("skeptic", "historian", "iconoclast")):
        return "skeptical"
    if any(token in persona_text for token in ("mediator", "humanist")):
        return "mediating"
    if index == 0:
        return "affirming"
    if index == 1:
        return "opposing"
    return DEFAULT_SENTIMENT


def suggest_workspace_metadata(topic: str, agents: list[dict] | None = None) -> dict:
    agent_suggestions = []
    for index, agent in enumerate(agents or []):
        agent_suggestions.append(
            WorkspaceAgentSuggestion(
                display_name=str(agent.get("display_name") or f"Debater {index + 1}"),
                sentiment=suggest_sentiment(
                    side=agent.get("side"),
                    persona_id=agent.get("persona_id") or agent.get("persona_choice"),
                    index=index,
                ),
            )
        )
    return {
        "debate_mode": DEFAULT_DEBATE_MODE,
        "topic_type": suggest_topic_type(topic),
        "topic_tags": [],
        "agent_sentiments": [item.__dict__ for item in agent_suggestions],
    }
