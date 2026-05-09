FROM python:3.12-slim

WORKDIR /app

# Copy requirements separately so this layer is cached between code-only rebuilds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# /watch is the default mount point for the torrent inbox
VOLUME ["/watch"]

CMD ["python", "src/monitor.py"]
