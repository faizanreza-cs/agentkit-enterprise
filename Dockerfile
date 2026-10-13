FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENTKIT_DB=/data/agentkit.db AGENTKIT_POLICY=/app/policies/default.yaml
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
COPY pyproject.toml README.md LICENSE ./
COPY agentkit ./agentkit
RUN uv pip install --system --no-cache .
COPY policies ./policies
COPY ui ./ui
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "agentkit.api:app", "--host", "0.0.0.0", "--port", "8000"]
