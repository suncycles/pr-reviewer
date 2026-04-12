# pr_reviewer/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    github_token: str
    anthropic_api_key: str
    db_path: str = "pr_reviewer.db"
    model: str = "claude-opus-4-5"
    max_tokens: int = 4096
    # Cost per million tokens (input/output) for claude-opus-4-5
    cost_per_million_input: float = 15.0
    cost_per_million_output: float = 75.0


def get_settings() -> Settings:
    return Settings()