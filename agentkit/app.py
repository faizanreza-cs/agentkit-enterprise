"""Wire the demo: registry + policy + store + provider."""

from __future__ import annotations

from functools import lru_cache

from agentkit.agent import Agent
from agentkit.config import get_settings
from agentkit.examples.support import registry
from agentkit.policy import Policy
from agentkit.store import Store


@lru_cache(maxsize=1)
def agent() -> Agent:
    s = get_settings()
    return Agent(registry, Policy.from_yaml(s.policy_path), Store(s.db_path), settings=s)
