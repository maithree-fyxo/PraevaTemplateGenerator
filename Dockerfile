# Multi-stage build: compile the Angular frontend, then serve it from FastAPI.
# Works on Railway, Fly.io, Cloud Run, or Render (Docker runtime).

# --- stage 1: build the Angular app ---
FROM node:22-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 2: python runtime ---
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# overwrite any committed build with the freshly compiled one
COPY --from=frontend /fe/dist/frontend/browser ./frontend/dist/frontend/browser

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
