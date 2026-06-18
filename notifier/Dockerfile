FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py models.py notifications.py translation.py ./
COPY notifier/ notifier/

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

CMD ["python", "-m", "notifier.main"]
