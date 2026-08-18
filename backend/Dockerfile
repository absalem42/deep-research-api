# Multi-stage so the heavy build toolchain never reaches the runtime image.
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies are their own layer: application edits do not reinstall the
# LangChain stack. The reference Dockerfile copied source before installing,
# so every code change rebuilt everything.
COPY requirements.txt .
RUN pip install -r requirements.txt


FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# Run as a non-root user. The reference image ran everything as root.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app/ ./app/
COPY --chown=app:app vendor/ ./vendor/
COPY --chown=app:app mcp_server/ ./mcp_server/

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=4).status==200 else 1)"

# One uvicorn worker per container. Scale by adding containers, not workers --
# job state is in-process, so a second worker would answer polls for jobs it
# does not have. See README "Scaling".
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 65"]
