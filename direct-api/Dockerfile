FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extractor.py rsc.py api.py ./
COPY static ./static

EXPOSE 8000

# Most PaaS platforms (Render included) inject $PORT and expect the app to bind to it,
# rather than a fixed port -- fall back to 8000 for plain `docker run`/compose use.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
