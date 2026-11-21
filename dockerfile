FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

# Встановлення Poetry
RUN pip install --no-cache-dir poetry

# Копіюємо файли залежностей для кешування шару
COPY pyproject.toml poetry.lock ./

# Встановлюємо продакшн-залежності (включно з gunicorn та whitenoise)
RUN poetry install --no-root

# Копіюємо код проєкту
COPY . .

# Збираємо статичні файли для WhiteNoise (передаємо тимчасовий ключ для білду)
RUN SECRET_KEY=build-placeholder-key python manage.py collectstatic --noinput
