# Dockerfile per DB Rollback SAL Service
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service script
COPY azure_clients.py ./
COPY db_rollback_sal_service.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the service
CMD ["python", "-u", "db_rollback_sal_service.py"]

