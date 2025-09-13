# Includes browsers & OS deps; we explicitly install the Python package too.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Ensure the Python package is present in this image
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir "playwright==1.47.0"

# App code
COPY main.py /app/main.py

# Output dir for screenshots/logs
RUN mkdir -p /app/out

CMD ["python", "/app/main.py"]
