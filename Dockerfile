FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Зависимости отдельным слоем — кэш не инвалидируется при правках кода
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения (.env намеренно не копируем — передаём через env_file / -e)
COPY *.py ./

# Непривилегированный пользователь
RUN useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
