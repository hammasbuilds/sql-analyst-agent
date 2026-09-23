"""Configuration. Two DSNs, because the privilege split is the whole safety story."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    admin_dsn: str = "postgresql://analyst:analyst@localhost:5434/retail"
    agent_dsn: str = "postgresql://agent_ro:agent_ro@localhost:5434/retail"

    llm_backend: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:14b"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    hf_token: str = ""
    hf_model: str = "Qwen/Qwen2.5-7B-Instruct"
    hf_base_url: str = "https://router.huggingface.co/v1"

    max_rows: int = 500
    statement_timeout_ms: int = 10_000
    max_repair_attempts: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
