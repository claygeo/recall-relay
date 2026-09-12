"""Runtime configuration. One env var switches the model provider; identical agent code runs locally,
on Render, and on AgentCore Runtime."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # model
    model_provider: str = field(default_factory=lambda: _env("MODEL_PROVIDER", "openrouter"))  # openrouter|anthropic|bedrock
    orchestrator_model: str = field(default_factory=lambda: _env("ORCHESTRATOR_MODEL", ""))
    matcher_model: str = field(default_factory=lambda: _env("MATCHER_MODEL", ""))
    writer_model: str = field(default_factory=lambda: _env("WRITER_MODEL", ""))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    # food bank
    food_bank_name: str = field(default_factory=lambda: _env("FOOD_BANK_NAME", "South Dade Community Food Bank"))
    food_bank_state: str = field(default_factory=lambda: _env("FOOD_BANK_STATE", "FL"))
    coordinator_email: str = field(default_factory=lambda: _env("COORDINATOR_EMAIL", "coordinator@example.org"))
    # storage
    db_path: Path = field(default_factory=lambda: Path(_env("RECALL_RELAY_DB", str(ROOT / "data" / "runtime" / "recall_relay.db"))))
    fixtures_dir: Path = field(default_factory=lambda: ROOT / "data" / "fixtures")
    # email
    email_backend: str = field(default_factory=lambda: _env("EMAIL_BACKEND", "mirror"))  # mirror|ses|resend
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM", "recall-relay@example.org"))
    resend_api_key: str = field(default_factory=lambda: _env("RESEND_API_KEY"))
    # web
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://127.0.0.1:8000"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me"))
    judge_password: str = field(default_factory=lambda: _env("JUDGE_PASSWORD", ""))
    # agent backend for the dashboard: inprocess | agentcore
    agent_backend: str = field(default_factory=lambda: _env("AGENT_BACKEND", "inprocess"))
    agentcore_runtime_arn: str = field(default_factory=lambda: _env("AGENTCORE_RUNTIME_ARN"))
    data_secret: str = field(default_factory=lambda: _env("AGENT_DATA_SECRET"))
    max_agent_runs_per_day: int = field(default_factory=lambda: int(_env("MAX_AGENT_RUNS_PER_DAY", "60") or 60))
    cache_dir: Path = field(default_factory=lambda: Path(_env("RECALL_RELAY_CACHE", str(ROOT / "data" / "runtime" / "cache"))))
    # fetch
    user_agent: str = field(default_factory=lambda: _env(
        "FETCH_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    ))

    def resolved_models(self) -> tuple[str, str, str]:
        """Default model ids per provider: (orchestrator, matcher, writer)."""
        if self.model_provider == "bedrock":
            d = ("global.anthropic.claude-sonnet-4-6", "global.anthropic.claude-sonnet-4-6", "global.anthropic.claude-sonnet-4-6")
        elif self.model_provider == "anthropic":
            d = ("claude-sonnet-4-6", "claude-sonnet-4-6", "claude-sonnet-4-6")
        else:  # openrouter
            d = ("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6")
        return (self.orchestrator_model or d[0], self.matcher_model or d[1], self.writer_model or d[2])


settings = Settings()
