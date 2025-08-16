FROM python:3.11-slim

# Systempakete inkl. Chromium & Chromedriver
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver ca-certificates wget curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    OPEN_BROWSER=0 \
    PORT=8000

WORKDIR /app

# Dependencies zuerst kopieren (Build-Cache nutzen)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY app ./app
COPY assets ./assets

# Gunicorn startet die Dash-App über die WSGI-Variable `server`
# 1 Worker, mehrere Threads → verhindert doppelte Scraper-Threads
CMD gunicorn app.hebelwatch:server -b 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180
