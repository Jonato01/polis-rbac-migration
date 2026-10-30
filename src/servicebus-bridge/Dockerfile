# Dockerfile per Service Bus HTTP Bridge
FROM python:3.9-slim

# Imposta directory di lavoro
WORKDIR /app

# Copia requirements
COPY requirements.txt .

# Installa dipendenze Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia il codice dell'applicazione (la configurazione arriva dalle env della Container App)
COPY azure_clients.py .
COPY servicebus_bridge.py .

# Esponi porta (opzionale - per health check)
EXPOSE 8080

# Comando di avvio
CMD ["python", "servicebus_bridge.py"]
