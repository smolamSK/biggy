# Production image for Biggy. Serves with gunicorn (run.py exposes `app`).
# The scheduler is run as a separate service (see docker-compose.yml) via
# `flask --app run run-jobs`; multiple gunicorn workers are safe because
# scheduled jobs are claimed atomically in the database.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=run

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 4 workers; the in-process scheduler ticker stays OFF here (the `jobs` service
# in docker-compose runs run-jobs). Override the command to taste.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "60", "run:app"]
