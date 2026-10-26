FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENTKIT_DB=/data/agentkit.db AGENTKIT_POLICY=/app/policies/default.yaml
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY agentkit ./agentkit
RUN uv sync --locked --no-editable --no-cache
ENV PATH="/app/.venv/bin:$PATH"
COPY policies ./policies
COPY ui ./ui
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "agentkit.api:app", "--host", "0.0.0.0", "--port", "8000"]
