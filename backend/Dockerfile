# Village Pond Planning System — Backend Dockerfile
# Uses Docker (rather than a buildpack) because rasterio/GDAL need system-level
# libraries that buildpack-based free hosts (Render's native Python runtime,
# etc.) frequently fail to install correctly. Docker sidesteps that entirely.

FROM python:3.11-slim

# System dependencies required by rasterio (GDAL) and scikit-image
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render (and most free hosts) inject the port to bind via $PORT
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
