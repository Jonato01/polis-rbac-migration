FROM python:3.11-slim

WORKDIR /app

# Installa dipendenze sistema per PDF e P7M
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
    openssl \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Aggiorna pip e installa dipendenze
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    python -c "import autogen; print('AutoGen installato correttamente:', autogen.__version__)"

COPY . .

# Entry point
CMD ["python", "main.py"]

