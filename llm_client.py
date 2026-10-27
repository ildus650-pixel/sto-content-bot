"""Асинхронный клиент текстовой нейросети (LLM).

Реализован поверх OpenAI-совместимого эндпоинта `/chat/completions`,
поэтому подходит для OpenAI, OpenRouter, Yandex Cloud (OpenAI-compatible),
локального Ollama и любого другого совместимого провайдера.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import aiohttp

from config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT: Final[str] = (
    "Ты профессиональный SMM-щик для автосервисов. Твоя задача - писать короткие, "
    "цепляющие посты для ВКонтакте/Telegram. Стиль: мужской, экспертный, но простой. "
    "Обязательно добавь призыв к действию (записаться по телефону/адресу). "
    "Не используй сложные деепричастные обороты. Длина: до 800 символов."
)

_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """Любая проблема при генерации текста (сеть, лимиты, пустой ответ)."""


def build_user_prompt(city: str, sto_name: str, service: str) -> str:
    """Собирает пользовательский промпт из данных, введённых в боте."""
    return (
        f"Город: {city or 'не указан'}\n"
        f"Автосервис: {sto_name or 'автосервис'}\n"
        f"Тема поста / услуга: {service}\n\n"
        "Напиши один готовый пост для соцсетей.\n"
        "Требования:\n"
        "- до 800 символов, живой разговорный русский язык;\n"
        "- без markdown-разметки, без кавычек вокруг всего текста;\n"
        "- 1-2 эмодзи по делу, не больше;\n"
        "- в конце призыв к действию (записаться по телефону или приехать в сервис);\n"
        "- не придумывай конкретные номера телефонов и адреса;\n"
        "- последняя строка — 3-5 хэштегов через пробел, начинающихся с #."
    )


async def _post_chat_completion(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
) -> dict[str, Any]:
    settings = get_settings()
    url = f"{settings.llm_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    last_error: str = "unknown error"
    for attempt in range(1, 3):
        try:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.text()
                if response.status == 200:
                    return await response.json(content_type=None)

                last_error = f"HTTP {response.status}: {body[:300]}"
                logger.error(
                    "LLM request failed (attempt %s/2): %s", attempt, last_error
                )
                if response.status not in _RETRY_STATUSES:
                    raise LLMError(last_error)
        except (TimeoutError, aiohttp.ClientError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.error("LLM transport error (attempt %s/2): %s", attempt, last_error)

        if attempt == 1:
            await asyncio.sleep(1.5)

    raise LLMError(last_error)


async def generate_post(city: str, sto_name: str, service: str) -> str:
    """Генерирует текст поста. Бросает LLMError при любой проблеме."""
    settings = get_settings()
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(city, sto_name, service)},
        ],
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "stream": False,
    }

    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _post_chat_completion(session, payload)

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Unexpected LLM response shape: %s", str(data)[:300])
        raise LLMError("Некорректный ответ LLM") from exc

    if isinstance(content, list):
        # Некоторые провайдеры возвращают content как список частей.
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )

    text = (content or "").strip()
    if not text:
        raise LLMError("LLM вернула пустой ответ")

    logger.info(
        "LLM post generated: service=%s, chars=%s, model=%s",
        service,
        len(text),
        settings.llm_model,
    )
    return text
