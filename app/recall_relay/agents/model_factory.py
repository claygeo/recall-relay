"""One switch, three providers. `MODEL_PROVIDER` decides which Strands model class the whole agent
layer runs on; nothing else in the codebase knows the difference.

Roles map onto `settings.resolved_models()` -> (orchestrator, matcher, writer). The extractor shares the
matcher's id because both are verbatim-copy jobs that must not be creative. Temperature is pinned low
everywhere: this agent is a clerk, not a writer of fiction.
"""
from __future__ import annotations

from typing import Any

from ..core.config import settings

# role -> index into settings.resolved_models()
_ROLE_INDEX: dict[str, int] = {
    "orchestrator": 0,
    "matcher": 1,
    "extractor": 1,
    "writer": 2,
}

TEMPERATURE = 0.1
MAX_TOKENS = 2048

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def model_id_for(role: str) -> str:
    """The configured model id for a role."""
    return settings.resolved_models()[_ROLE_INDEX.get(role, 0)]


def get_model(role: str = "orchestrator", *, max_tokens: int = MAX_TOKENS) -> Any:
    """Build the Strands model object for `role` under the configured provider.

    Args:
        role: orchestrator | matcher | extractor | writer.
        max_tokens: per-call output cap.

    Returns:
        A strands model instance (OpenAIModel, AnthropicModel or BedrockModel).
    """
    model_id = model_id_for(role)
    provider = (settings.model_provider or "openrouter").strip().lower()

    if provider == "bedrock":
        from strands.models.bedrock import BedrockModel

        return BedrockModel(
            model_id=model_id,
            region_name=settings.aws_region,
            temperature=TEMPERATURE,
            max_tokens=max_tokens,
        )

    if provider == "anthropic":
        from strands.models.anthropic import AnthropicModel

        if not settings.anthropic_api_key:
            raise RuntimeError("MODEL_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset")
        return AnthropicModel(
            client_args={"api_key": settings.anthropic_api_key},
            model_id=model_id,
            max_tokens=max_tokens,
            params={"temperature": TEMPERATURE},
        )

    if provider == "openrouter":
        from strands.models.openai import OpenAIModel

        if not settings.openrouter_api_key:
            raise RuntimeError("MODEL_PROVIDER=openrouter but OPENROUTER_API_KEY is unset")
        return OpenAIModel(
            client_args={"api_key": settings.openrouter_api_key, "base_url": OPENROUTER_BASE_URL},
            model_id=model_id,
            params={"max_tokens": max_tokens, "temperature": TEMPERATURE},
        )

    raise ValueError(f"unknown MODEL_PROVIDER {settings.model_provider!r} (expected openrouter|anthropic|bedrock)")
