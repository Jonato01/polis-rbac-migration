FROM python:3.11-slim

WORKDIR /app

# Copia tutto il progetto (script esistenti + container_app)
COPY . .

# Installa dipendenze
RUN pip install --no-cache-dir -r container_app/requirements.txt

# Configura PYTHONPATH per gli import relativi degli script esistenti
ENV PYTHONPATH=/app

# Crea directory per file temporanei e logs
RUN mkdir -p /tmp/imports /app/logs

CMD ["python", "container_app/main.py"]
