# СТО-Контент — Telegram-бот генерации постов для автосервисов

MVP B2B-бота: владелец СТО за 30 секунд получает готовый пост (текст + картинка + хэштеги) для ВКонтакте или Telegram.

Стек: Python 3.11+, aiogram 3.x, asyncio, aiohttp, pydantic-settings.

## Что умеет

- Пошаговый сценарий: город → название СТО (можно пропустить) → услуга.
- Статус «печатает…» на всё время генерации (15–30 сек).
- Текст генерирует LLM, картинку — Image API.
- Если Image API упал — приходит только текст с пометкой «⚠️ Изображение временно недоступно…».
- Если LLM упала — «Сервер перегружен, попробуйте через минуту» и кнопка повтора.
- Кнопка «Сгенерировать ещё вариант?» — новый вариант с теми же данными.
- Логирование в stdout (INFO/ERROR), глобальный обработчик ошибок aiogram.
- HTTP `/healthz` на `$PORT` — чтобы жить на Render/Railway как web service.

## Структура

```
sto_bot_mvp/
├── bot.py              # aiogram 3: роутеры, FSM, хендлеры, health-сервер
├── config.py           # настройки из .env (pydantic-settings)
├── llm_client.py       # асинхронный клиент LLM (OpenAI-совместимый)
├── image_client.py     # клиент картинок (OpenAI / Stability) + fallback
├── requirements.txt
├── .env.example
├── render.yaml         # Blueprint для Render (1 клик)
├── railway.json        # конфиг сборки для Railway
├── docker-compose.yml  # запуск на VPS
├── Dockerfile
└── README.md
```

## 1. Подготовка ключей

| Что | Где взять | Переменная |
|---|---|---|
| Токен бота | `@BotFather` → `/newbot` | `TELEGRAM_BOT_TOKEN` |
| Ключ LLM | кабинет провайдера (OpenAI, OpenRouter, Yandex Cloud и т.п.) | `LLM_API_KEY` |
| Модель LLM | название модели у провайдера | `LLM_MODEL` |
| Base URL LLM | эндпоинт вида `.../v1` | `LLM_BASE_URL` |
| Ключ картинок | OpenAI или Stability AI | `IMAGE_API_KEY` |

`llm_client.py` использует OpenAI-совместимый формат `/chat/completions`, поэтому подходит любой провайдер с таким API (OpenAI, OpenRouter, Yandex Cloud в OpenAI-совместимом режиме, локальный Ollama). Точный `LLM_BASE_URL` и имя модели берите из документации провайдера — они меняются.

GigaChat имеет собственный OAuth-протокол и не совместим с этим клиентом «из коробки»: нужен либо прокси-шлюз с OpenAI-совместимым API, либо отдельный адаптер.

## 2. Запуск локально

```bash
cd sto_bot_mvp
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # Windows: copy .env.example .env
# откройте .env и впишите реальные ключи

python bot.py
```

В логах должно появиться:

```
2026-01-01 12:00:00 | INFO | bot | Bot starting: llm=gpt-4o-mini, image=openai
```

Дальше открывайте бота в Telegram и жмите `/start`. Остановка: `Ctrl+C`.

## 3. Деплой 24/7

### Вариант A. Render (бесплатно, с оговоркой)

1. Запушьте каталог `sto_bot_mvp` в GitHub. Репозиторий может быть приватным — Render видит приватные репозитории после установки GitHub App. Каталог уже инициализирован как git-репозиторий с первым коммитом:

   ```bash
   gh auth login
   gh repo create sto-content-bot --private --source=. --push
   ```

2. Render Dashboard → **New** → **Blueprint** → выберите репозиторий → **Apply**.
3. Render прочитает `render.yaml` и попросит значения для `sync: false`: `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `IMAGE_API_KEY`. Вставляйте прямо в форму Render — не в чат и не в файлы репозитория. Если ключа картинок пока нет, впишите прочерк `-`: бот запустится, картинки будут отключены (штатный fallback на текст), ключ замените позже в **Environment**.
4. Дождитесь статуса **Live**. Проверка: `https://<имя-сервиса>.onrender.com/healthz` → `{"status":"ok",...}`.
5. Откройте `t.me/<username_вашего_бота>` — бот отвечает на `/start`.

**Ограничение бесплатного тарифа, о котором нужно знать заранее:** Render выключает free web service после 15 минут без входящего трафика, а холодный старт занимает около минуты. Телеграм-поллинг входящим трафиком для Render не считается, поэтому без «пинга» бот будет спать.

Лечится бесплатно: зарегистрируйте `https://<имя-сервиса>.onrender.com/healthz` в любом бесплатном аптайм-мониторе (UptimeRobot, cron-job.org) с интервалом 5–10 минут. Пока пинги идут — сервис не засыпает. Если монитор отключится, бот уснёт и проснётся только от следующего запроса к URL.

Почему бот вообще поднимает HTTP-порт: на Render «background worker» — платный тип сервиса, а web service — нет. Поэтому `bot.py` при наличии `PORT` поднимает `/healthz` и остаётся web service'ом.

Если 24/7 без пингов критично — переходите на платный инстанс ($7/мес по прайсу Render на момент написания) или на VPS.

### Вариант B. Railway (быстрый старт, но не бесплатный вдолгую)

1. Railway → **New Project** → **Deploy from GitHub repo** → выберите репозиторий. `railway.json` подхватит Dockerfile.
2. Вкладка **Variables** → добавьте `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `IMAGE_API_KEY`, `IMAGE_PROVIDER`, `IMAGE_MODEL`.
3. **Settings → Networking → Generate Domain**, порт — тот, что Railway подставит в `PORT`.

По состоянию на октябрь 2026 новый аккаунт получает разовый trial-кредит около $5 на 30 дней, дальше free plan даёт порядка $1 кредита в месяц — на круглосуточный контейнер этого, как правило, не хватает, поэтому считайте это способом погонять MVP месяц, а не постоянным хостингом. Актуальные цифры проверяйте на странице тарифов Railway.

### Вариант C. VPS (самый предсказуемый)

```bash
git clone <ваш-репозиторий> && cd sto_bot_mvp
cp .env.example .env && nano .env      # вписать ключи
docker compose up -d --build
docker compose logs -f
```

`restart: unless-stopped` в `docker-compose.yml` поднимает бота после перезагрузки сервера. Подойдёт любой VPS за 200–400 ₽/мес (Timeweb, Beget, Hetzner).

### Чистый Docker (без compose)

```bash
docker build -t sto-bot .
docker run -d --name sto-bot --env-file .env --restart unless-stopped sto-bot
docker logs -f sto-bot
```

`.env` намеренно не копируется в образ — секреты передаются через `--env-file`.

## 4. Настройка под себя

- Города: `CITIES` в `bot.py`.
- Услуги: `SERVICES` в `bot.py`.
- Сцены для картинок: `_SERVICE_SCENES` в `image_client.py`.
- Системный промпт: `SYSTEM_PROMPT` в `llm_client.py`.
- Размер картинки: `IMAGE_SIZE` (для DALL-E 3 допустимы `1024x1024`, `1792x1024`, `1024x1792`).

## 5. Типовые проблемы

| Симптом | Причина / решение |
|---|---|
| `pydantic ValidationError` при старте | не заполнены `TELEGRAM_BOT_TOKEN` / `LLM_API_KEY` |
| Бот молчит, в логах `Unauthorized` | неверный токен бота, перевыпустите у `@BotFather` |
| «Сервер перегружен…» на каждый запрос | проверьте `LLM_BASE_URL`, `LLM_MODEL`, баланс ключа; тело ошибки — в логах |
| Текст есть, картинки нет | лимиты/кредиты Image API или неверный `IMAGE_MODEL`; это штатный fallback |
| `409 Conflict` | запущен второй экземпляр бота с этим токеном (локально + на хостинге) |
| Render: `no open ports detected` | не задан `PORT`; на Render он приходит автоматически, проверьте `healthCheckPath` |
| Render: бот отвечает через минуту | free-инстанс спал, нужен аптайм-монитор на `/healthz` |
| Долгий первый ответ | холодный старт провайдера; таймауты — `LLM_TIMEOUT` / `IMAGE_TIMEOUT` |

## 6. Безопасность

- Токен бота — это полный доступ к боту. Не коммитьте `.env`, не вставляйте токен в переписку и тикеты.
- Если токен всё же засветился — `@BotFather` → `/revoke` и замените переменную в хостинге.
- Ключи LLM/Image лучше держать отдельными от личных: отдельный проект у провайдера + лимит расходов.

## 7. Что дальше (за рамками MVP)

- Хранение пользователей и истории постов (SQLite/Postgres + middleware).
- Платные тарифы и лимиты генераций (Telegram Stars / ЮKassa).
- Публикация в ВК по API и отложенный постинг.
- Redis вместо `MemoryStorage`, чтобы не терять состояния при рестарте.
