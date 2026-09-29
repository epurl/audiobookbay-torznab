FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app/app

# Library and settings live here; mount a volume so they survive rebuilds
ENV BAYARR_CONFIG_DIR=/config
VOLUME /config

# Run uvicorn server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
