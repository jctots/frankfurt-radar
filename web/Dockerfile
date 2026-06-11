FROM python:3.12-slim

WORKDIR /app

# Install all deps (poller + web) so /api/poll can run main.py
COPY requirements.txt .
COPY web/requirements.txt ./web-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r web-requirements.txt

COPY *.py .
COPY config.yaml .
COPY web/app.py .
COPY web/templates/ templates/
COPY web/static/ static/

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}
ENV DATA_DIR=/app/data

EXPOSE 8080
CMD ["sh", "-c", "[ -f /app/data/config.yaml ] || cp /app/config.yaml /app/data/config.yaml; exec gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 120 app:app"]
