FROM python:3.12-slim

WORKDIR /app

# Install all deps (poller + web) so /api/poll can run main.py
COPY requirements.txt .
COPY web/requirements.txt ./web-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r web-requirements.txt

COPY *.py .
COPY web/app.py .
COPY web/templates/ templates/

ENV DATA_DIR=/app/data

EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "app:app"]
