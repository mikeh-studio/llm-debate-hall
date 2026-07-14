from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from llm_debate_hall.config import custom_commands_enabled
from llm_debate_hall.models import BackendPresetModel


LOCAL_CLIENT_NAMES = {"localhost", "testclient"}


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() in LOCAL_CLIENT_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalOnlyMiddleware:
    """Reject non-loopback HTTP and WebSocket clients in the default local mode."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        host = client[0] if client else None
        if is_loopback_client(host):
            await self.app(scope, receive, send)
            return

        detail = (
            "Multi-Agent Council only accepts loopback clients by default. "
            "Set MULTI_AGENT_COUNCIL_ALLOW_REMOTE_ACCESS=true only behind your own authentication boundary."
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": detail})
            return
        await PlainTextResponse(detail, status_code=403)(scope, receive, send)


def backend_overrides_requested(
    preset: BackendPresetModel,
    *,
    command: list[str],
    args_template: list[str],
    env: dict[str, str],
) -> bool:
    return command != preset.command or args_template != preset.args_template or bool(env)


def assert_backend_configuration_is_safe(
    preset: BackendPresetModel,
    *,
    command: list[str],
    args_template: list[str],
    env: dict[str, str],
) -> None:
    if not backend_overrides_requested(
        preset,
        command=command,
        args_template=args_template,
        env=env,
    ):
        return
    if custom_commands_enabled():
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Custom command, argument, and environment overrides are disabled. "
            "Use a built-in provider configuration and set credentials in the server shell, or explicitly set "
            "MULTI_AGENT_COUNCIL_ENABLE_CUSTOM_COMMANDS=true for trusted local development."
        ),
    )


def redact_agent_environment(agent: dict[str, Any]) -> dict[str, Any]:
    public_agent = {**agent}
    environment = public_agent.get("env") or {}
    public_agent["env"] = {}
    public_agent["env_keys"] = sorted(str(name) for name in environment)
    return public_agent
