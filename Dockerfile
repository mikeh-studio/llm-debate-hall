FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV MULTI_AGENT_COUNCIL_DB_PATH=""
ENV MULTI_AGENT_COUNCIL_PERSONAS_ROOT=""
ENV MULTI_AGENT_COUNCIL_ENABLE_MOCK_PRESET=""

WORKDIR /app

COPY pyproject.toml README.md ./
COPY llm_debate_hall ./llm_debate_hall
COPY scripts ./scripts

RUN mkdir -p /data/personas && pip install --no-cache-dir .

EXPOSE 8000

# NOTE: containers are reached through a bridge/proxy IP, never loopback, so
# the default local-only middleware will 403 every request through a published
# port. To serve traffic through the mapped port you must explicitly opt in:
#   docker run -e MULTI_AGENT_COUNCIL_ALLOW_REMOTE_ACCESS=true -p 8000:8000 ...
# Only do this behind your own authentication boundary.
CMD ["sh", "-c", "export MULTI_AGENT_COUNCIL_DB_PATH=\"${MULTI_AGENT_COUNCIL_DB_PATH:-${LLM_DEBATE_HALL_DB_PATH:-/data/multi_agent_council.db}}\"; export MULTI_AGENT_COUNCIL_PERSONAS_ROOT=\"${MULTI_AGENT_COUNCIL_PERSONAS_ROOT:-${LLM_DEBATE_HALL_PERSONAS_ROOT:-/data/personas}}\"; export MULTI_AGENT_COUNCIL_ENABLE_MOCK_PRESET=\"${MULTI_AGENT_COUNCIL_ENABLE_MOCK_PRESET:-${LLM_DEBATE_HALL_ENABLE_MOCK_PRESET:-true}}\"; exec uvicorn llm_debate_hall.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
