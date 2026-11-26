# Backend image (all Captain processes use it; the Helm chart sets the command).
# Written for the pilot cluster; not built or run in this repo's development flow.
FROM python:3.12-slim AS build
# Cloud libraries for the chosen cloud: --build-arg EXTRAS=azure (Key Vault signing,
# immutable Blob anchors, workload identity) or EXTRAS=aws. Empty = no cloud SDKs.
ARG EXTRAS=""
WORKDIR /src
COPY pyproject.toml ./
COPY captain ./captain
RUN if [ -n "$EXTRAS" ]; then pip install --no-cache-dir --prefix=/install ".[$EXTRAS]";     else pip install --no-cache-dir --prefix=/install .; fi

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home captain
COPY --from=build /install /usr/local
COPY alembic.ini ./
COPY migrations ./migrations
USER 10001
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "captain.workers"]
