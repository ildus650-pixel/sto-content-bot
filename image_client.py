"""Асинхронный клиент генерации изображений.

Поддерживаются два провайдера:
* ``openai``    — POST /images/generations (DALL-E 3, gpt-image-1 и совместимые);
* ``stability`` — POST /v2beta/stable-image/generate/core (Stable Diffusion).

Любая проблема приводит к исключению ImageError: бот обязан уметь
работать без картинки (fallback на текст).
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Final

import aiohttp

from config import get_settings

logger = logging.getLogger(__name__)

BASE_PROMPT_SUFFIX: Final[str] = (
    "realistic photo, professional car service garage, high quality, well lit"
)

_SERVICE_SCENES: Final[dict[str, str]] = {
    "Шиномонтаж": (
        "mechanic mounting a new winter tyre on an alloy wheel, stack of tyres nearby"
    ),
    "Диагностика": (
        "mechanic with diagnostic scanner checking a car engine bay, laptop with graphs"
    ),
    "Замена масла": (
        "fresh engine oil being poured into a car engine, oil filter and funnel"
    ),
    "Ремонт ходовой": (
        "car lifted on a hoist, mechanic inspecting suspension and shock absorbers"
    ),
    "Акция": (
        "clean modern car service reception with a car and tyre display, promotional mood"
    ),
}

_STABILITY_URL: Final[str] = (
    "https://api.stability.ai/v2beta/stable-image/generate/core"
)


class ImageError(Exception):
    """Не удалось сгенерировать или скачать изображение."""


def build_image_prompt(service: str) -> str:
    """Динамический промпт: сцена под услугу + обязательный суффикс."""
    scene = _SERVICE_SCENES.get(service, "mechanic working on a car in a garage")
    return f"{scene}, {BASE_PROMPT_SUFFIX}"


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as response:
        if response.status != 200:
            raise ImageError(f"Не удалось скачать изображение: HTTP {response.status}")
        payload = await response.read()
    if not payload:
        raise ImageError("Пустой файл изображения")
    return payload


async def _generate_openai(prompt: str) -> bytes:
    settings = get_settings()
    url = f"{settings.image_base_url}/images/generations"
    headers = {"Authorization": f"Bearer {settings.image_api_key}"}
    payload: dict[str, Any] = {
        "model": settings.image_model,
        "prompt": prompt,
        "n": 1,
        "size": settings.image_size,
    }

    async with (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.image_timeout)
        ) as session,
        session.post(url, json=payload, headers=headers) as response,
    ):
        body = await response.text()
        if response.status != 200:
            raise ImageError(f"Image API HTTP {response.status}: {body[:300]}")

        try:
            item = (await response.json(content_type=None))["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ImageError("Некорректный ответ Image API") from exc

        if item.get("b64_json"):
            try:
                return base64.b64decode(item["b64_json"])
            except (binascii.Error, ValueError) as exc:
                raise ImageError("Битый base64 в ответе Image API") from exc

        if item.get("url"):
            return await _download(session, item["url"])

    raise ImageError("Image API не вернул ни url, ни b64_json")


async def _generate_stability(prompt: str) -> bytes:
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.image_api_key}",
        "Accept": "image/*",
    }
    form = aiohttp.FormData()
    form.add_field("prompt", prompt)
    form.add_field("output_format", "jpeg")
    form.add_field("aspect_ratio", "1:1")

    async with (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.image_timeout)
        ) as session,
        session.post(_STABILITY_URL, data=form, headers=headers) as response,
    ):
        if response.status != 200:
            body = await response.text()
            raise ImageError(f"Stability API HTTP {response.status}: {body[:300]}")
        payload = await response.read()

    if not payload:
        raise ImageError("Stability API вернул пустой ответ")
    return payload


async def generate_image(service: str) -> bytes:
    """Генерирует картинку под услугу. Бросает ImageError при любой проблеме."""
    settings = get_settings()
    prompt = build_image_prompt(service)
    logger.info("Image generation started: provider=%s", settings.image_provider)

    try:
        if settings.image_provider == "stability":
            image = await _generate_stability(prompt)
        else:
            image = await _generate_openai(prompt)
    except ImageError:
        raise
    except TimeoutError as exc:
        raise ImageError("Image API: таймаут") from exc
    except aiohttp.ClientError as exc:
        raise ImageError(f"Image API: сетевая ошибка ({type(exc).__name__})") from exc

    logger.info("Image generated: %s bytes", len(image))
    return image
