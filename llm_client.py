"""Асинхронный клиент текстовой нейросети (LLM).

Реализован поверх OpenAI-совместимого эндпоинта `/chat/completions`,
поэтому подходит для OpenAI, OpenRouter, Yandex Cloud (OpenAI-compatible),
локального Ollama и любого другого совместимого провайдера.
"""

from __future__ import annotations

import asyncio
import logging
import time
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

_RETRY_STATUSES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})


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
    for attempt in (1, 2):
        try:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.text()
                if response.status == 200:
                    data = await response.json(content_type=None)
                    # OpenRouter умеет вернуть 200 с телом {"error": {...}} —
                    # это отказ провайдера, а не валидный ответ.
                    if isinstance(data, dict) and data.get("error"):
                        error = data["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        message = (
                            error.get("message")
                            if isinstance(error, dict)
                            else str(error)
                        )
                        raise LLMError(f"provider error {code}: {message}")
                    return data

                last_error = f"HTTP {response.status}: {body[:300]}"
                logger.error(
                    "LLM request failed (attempt %s/2): %s", attempt, last_error
                )
                # 429 — исчерпана бесплатная квота провайдера: сразу к следующей модели
                if response.status not in _RETRY_STATUSES:
                    raise LLMError(last_error)
        except (TimeoutError, aiohttp.ClientError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.error("LLM transport error (attempt %s/2): %s", attempt, last_error)

        if attempt == 1:
            await asyncio.sleep(1.5)

    raise LLMError(last_error)


def candidate_models() -> list[str]:
    """Основная модель + резервные из LLM_FALLBACK_MODELS (без дублей)."""
    settings = get_settings()
    ordered = [settings.llm_model]
    ordered += [
        model.strip()
        for model in settings.llm_fallback_models.split(",")
        if model.strip()
    ]

    unique: list[str] = []
    for model in ordered:
        if model not in unique:
            unique.append(model)
    return unique


def _extract_text(data: dict[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Unexpected LLM response shape: %s", str(data)[:300])
        raise LLMError("Некорректный ответ LLM") from exc

    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )

    text = (content or "").strip()
    if not text:
        raise LLMError("LLM вернула пустой ответ")
    return text


async def generate_post(city: str, sto_name: str, service: str) -> str:
    """Генерирует текст поста, перебирая модели в пределах общего бюджета времени.

    Бесплатные модели регулярно отдают 429/503 или пустой ответ, поэтому отказ
    одной модели не роняет генерацию: идём по цепочке дальше, но не дольше
    LLM_TOTAL_TIMEOUT секунд суммарно — иначе пользователь ждёт вечно.
    """
    settings = get_settings()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(city, sto_name, service)},
    ]

    deadline = time.monotonic() + settings.llm_total_timeout
    failures: list[str] = []

    for model in candidate_models():
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            failures.append(f"{model}: пропущена, бюджет времени исчерпан")
            logger.warning("Time budget exhausted before model %s", model)
            break

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "stream": False,
        }
        timeout = aiohttp.ClientTimeout(total=min(settings.llm_timeout, remaining))

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                data = await _post_chat_completion(session, payload)
            text = _extract_text(data)
        except LLMError as exc:
            failures.append(f"{model}: {exc}")
            logger.warning("Model %s failed, trying next: %s", model, exc)
            continue
        except (TimeoutError, aiohttp.ClientError) as exc:
            failures.append(f"{model}: {type(exc).__name__}")
            logger.warning("Model %s transport failure: %s", model, exc)
            continue

        logger.info(
            "LLM post generated: service=%s, chars=%s, model=%s",
            service,
            len(text),
            model,
        )
        return text

    raise LLMError("Все модели недоступны: " + "; ".join(failures)[:400])
