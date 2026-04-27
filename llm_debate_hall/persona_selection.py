from __future__ import annotations

import asyncio
from typing import Any, Callable

from llm_debate_hall.adapters.base import AdapterRequest, DebateAdapter
from llm_debate_hall.events import EventBroker
from llm_debate_hall.payloads import required_json
from llm_debate_hall.prompts import build_persona_prompt
from llm_debate_hall.storage import Storage


class PersonaSelectionService:
    def __init__(
        self,
        *,
        storage: Storage,
        broker: EventBroker,
        adapter_factory: Callable[[dict[str, Any]], DebateAdapter],
    ) -> None:
        self.storage = storage
        self.broker = broker
        self.adapter_factory = adapter_factory

    async def select_personas(
        self,
        session: dict[str, Any],
        agents: list[dict[str, Any]],
        selectable_personas: list[dict[str, Any]],
    ) -> None:
        auto_agents = [agent for agent in agents if not agent.get("persona_id")]
        if not auto_agents:
            return

        results = await asyncio.gather(
            *[
                self._select_persona_for_agent(session, agent, selectable_personas)
                for agent in auto_agents
            ]
        )

        for agent, persona_id, justification in results:
            self.storage.update_agent_persona(agent["id"], persona_id)
            agent["persona_id"] = persona_id
            await self.broker.publish(
                session["id"],
                {
                    "type": "persona_selected",
                    "agent_id": agent["id"],
                    "agent_name": agent["display_name"],
                    "persona_id": persona_id,
                    "justification": justification,
                },
            )

    async def _select_persona_for_agent(
        self,
        session: dict[str, Any],
        agent: dict[str, Any],
        selectable_personas: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str, str]:
        adapter = self.adapter_factory(agent)
        prompt = build_persona_prompt(session["topic"], agent, selectable_personas)
        request = AdapterRequest(
            session_id=session["id"],
            agent_id=agent["id"],
            agent_name=agent["display_name"],
            preset_id=agent["preset_id"],
            role=agent["role"],
            side=agent["side"],
            topic=session["topic"],
            prompt=prompt,
            output_mode="persona",
            model_name=agent["model_name"],
            command=agent["command"],
            args_template=agent["args_template"],
            env=agent["env"],
        )
        response = await adapter.generate(request, _noop)
        payload = required_json(
            response.raw_text,
            context=f"{agent['display_name']} persona selection",
            preset_id=agent["preset_id"],
            model_name=agent["model_name"],
        )
        persona_id = payload.get("persona_id")
        if not any(persona["id"] == persona_id for persona in selectable_personas):
            persona_id = selectable_personas[0]["id"]
        return agent, persona_id, payload.get("justification", "")


async def _noop(_: str) -> None:
    return None
