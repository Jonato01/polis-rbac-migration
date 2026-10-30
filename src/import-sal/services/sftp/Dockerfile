FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
# Installa le dipendenze
RUN pip install --no-cache-dir -r requirements.txt
# Copia lo script
COPY azure_clients.py ./
COPY sftp_service.py .

# Output non bufferizzato
ENV PYTHONUNBUFFERED=1

# Esegui
CMD ["python", "-u", "sftp_service.py"]
