FROM python:3.10-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure the instance folder exists for SQLite
RUN mkdir -p instance

EXPOSE 5000

ENV FLASK_DEBUG=false

CMD ["python", "app.py"]
