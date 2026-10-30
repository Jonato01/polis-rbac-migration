FROM python:3.11-slim

WORKDIR /app

# Installa mysql-client per mysqldump
RUN apt-get update && \
    apt-get install -y default-mysql-client && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY azure_clients.py ./
COPY db_backup_service.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "db_backup_service.py"]
