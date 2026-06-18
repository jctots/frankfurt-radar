FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py models.py notifications.py translation.py ./
COPY notifier/ notifier/

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

COPY notifier/entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["/app/entrypoint.sh"]
