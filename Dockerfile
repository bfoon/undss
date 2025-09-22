FROM python:3.11-slim

# Set base working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Set working directory to where manage.py is (adjust if needed)
WORKDIR /app/un_security_system

# Collect static files
RUN python manage.py collectstatic --noinput

# Expose Django dev server port
EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
