FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .
COPY config.yaml .
COPY crontab /etc/cron.d/frankfurt-radar
RUN chmod 0644 /etc/cron.d/frankfurt-radar

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["/app/entrypoint.sh"]
