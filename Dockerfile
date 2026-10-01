FROM python:3.11-slim

WORKDIR /app

# ffmpeg converts books to M4B (Convert to M4B in a book's details)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg 7zip && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app/app

# Library and settings live here; mount a volume so they survive rebuilds
ENV BAYARR_CONFIG_DIR=/config
VOLUME /config

# Run uvicorn server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
