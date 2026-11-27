FROM python:3.11-slim

# Set base working directory
WORKDIR /app

# Install system dependencies
#   postgresql-client  database tooling
#   ca-certificates    the CA bundle NES trusts by default, which holds the
#                      timestamp authority's root
#   fonts-liberation   fonts embedded in every archival (PDF/A) document;
#                      metrically identical to Helvetica, so nothing moves
#   ghostscript        converts third-party uploads whose fonts are missing
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    ca-certificates \
    fonts-liberation \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files (.dockerignore keeps .env and ./secrets out)
COPY . .

# Set working directory to where manage.py is (adjust if needed)
WORKDIR /app/un_security_system

# Collect static files
RUN python manage.py collectstatic --noinput

# Expose Django dev server port
EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
