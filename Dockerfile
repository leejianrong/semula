# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# Add Postgres support with: --build-arg UV_EXTRAS="--extra postgres"
ARG UV_EXTRAS=""
RUN uv sync --frozen --no-dev --no-editable $UV_EXTRAS

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 semula && mkdir /data && chown semula /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" SEMULA_DATA_DIR=/data SEMULA_HOST=0.0.0.0 SEMULA_PORT=8080
USER semula
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=2)"
CMD ["semula", "serve"]
