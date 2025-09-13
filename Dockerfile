FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Ensure Python deps are available
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir "playwright==1.47.0" "flask==3.0.3" "jinja2==3.1.4"

# App code
COPY app.py nucore_client.py /app/
COPY templates /app/templates

# Artifacts
RUN mkdir -p /app/out /app/data

EXPOSE 8000
CMD ["python", "/app/app.py"]
