FROM python:3.12-slim

WORKDIR /app

COPY mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py models.py ./
COPY mcp/server.py mcp/auth.py ./

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 8811

CMD ["python", "server.py"]
