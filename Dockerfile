# ss-to-wp-worker — Dockerfile (Nixpacks vietā, jo WeasyPrint prasa
# system bibliotēkas, ko Nixpacks vide neredz — libgobject/pango).
# Tīra Debian: apt libs + system Python vienā vidē, ctypes.find_library strādā.
FROM python:3.12-slim-bookworm

# WeasyPrint system atkarības (Debian bookworm). libpango pievelk
# libglib2.0-0 (= libgobject) kā Depends. fonts-dejavu = fallback fonti.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libffi8 \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway padod $PORT; shell forma, lai mainīgais izvēršas.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
