# Alternative deploy path: builds the frontend itself, so frontend/dist does
# not need to be committed. Use this if you prefer a clean repo over a
# pip-only build. Render, Fly and Railway all accept it.
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY data/sample_raw.csv ./data/
COPY --from=frontend /app/frontend/dist ./frontend/dist
ENV RM_DB_PATH=/tmp/app.db
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
