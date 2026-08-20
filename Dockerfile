FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
# Render/Railway/Fly all inject $PORT — default to 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

# Runs DB migrations implicitly via Base.metadata.create_all() on startup (see app/main.py).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
