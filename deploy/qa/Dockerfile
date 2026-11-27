# Captain QA image: every backend process plus the console, in one image.
# Build from the repository root:
#   docker build -f deploy/qa/Dockerfile -t stixis/captain:qa .

# ---- console (React) --------------------------------------------------------------------
FROM node:22-alpine AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console/ ./
RUN npm run build

# ---- backend (Python) -------------------------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /src
COPY backend/pyproject.toml ./
COPY backend/captain ./captain
RUN pip install --no-cache-dir --prefix=/install .

# ---- runtime ------------------------------------------------------------------------------
FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home captain \
 && mkdir -p /data/keys /data/anchors /app \
 && chown -R 10001:10001 /data
COPY --from=build /install /usr/local
WORKDIR /app
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY --from=console /console/dist /app/console
COPY docs/guides /app/guides
COPY deploy/qa/entrypoint.sh /app/entrypoint.sh
# Even if the checkout used Windows line endings, the script must not.
RUN sed -i "s/\r$//" /app/entrypoint.sh
ENV CAPTAIN_CONSOLE_DIR=/app/console \
    CAPTAIN_GUIDES_DIR=/app/guides \
    CAPTAIN_ARTIFACTS_DIR=/app/artifacts \
    PYTHONUNBUFFERED=1
USER 10001
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["worker"]
