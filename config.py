"""Конфигурация приложения.

Все значения читаются из переменных окружения (или из файла .env).
Валидация и приведение типов выполняются pydantic-settings.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(override=False)


class Settings(BaseSettings):
    """Настройки бота, загружаемые из .env / окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(min_length=20)

    # --- LLM ---
    llm_api_key: str = Field(min_length=1)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=600, ge=64, le=4096)
    llm_timeout: float = Field(default=60.0, gt=0)

    # --- Image ---
    image_provider: Literal["openai", "stability"] = "openai"
    image_api_key: str = ""
    image_base_url: str = "https://api.openai.com/v1"
    image_model: str = "dall-e-3"
    image_size: str = "1024x1024"
    image_timeout: float = Field(default=120.0, gt=0)

    # --- Прочее ---
    log_level: str = "INFO"

    @field_validator("llm_base_url", "image_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _infer_image_api_key(self) -> Settings:
        """Если ключ картинок не задан, для OpenAI-совместимого провайдера
        используем ключ LLM (обычно это один и тот же аккаунт)."""
        if not self.image_api_key.strip():
            if self.image_provider == "openai":
                self.image_api_key = self.llm_api_key
            else:
                raise ValueError(
                    "IMAGE_API_KEY обязателен при IMAGE_PROVIDER=stability"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (кэшируется)."""
    return Settings()  # type: ignore[call-arg]
