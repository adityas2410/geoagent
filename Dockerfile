# Build the React command center, then include it in the FastAPI runtime image.
FROM node:24-bookworm-slim AS frontend-build

WORKDIR /build/frontend
ARG VITE_API_BASE_URL=""
ARG VITE_GOOGLE_MAPS_API_KEY=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_GOOGLE_MAPS_API_KEY=${VITE_GOOGLE_MAPS_API_KEY}
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=frontend-build /build/frontend/dist ./frontend_dist

CMD ["sh", "-c", "uvicorn geoagent.app:app --host 0.0.0.0 --port ${PORT}"]
