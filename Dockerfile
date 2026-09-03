# Multi-architecture OCI index digest resolved from Docker Official Images.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/flowgrid-memory
COPY pyproject.toml README.md LICENSE /opt/flowgrid-memory/
COPY aml_retriever /opt/flowgrid-memory/aml_retriever
RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 flowgrid

USER flowgrid

# No config, database, or credential is baked into this image.  The default
# command exits at the required --config gate.  Even with a config, REST v1
# accepts only the literal 127.0.0.1 bind and is not a hosted/multitenant API.
ENTRYPOINT ["flowgrid-memory-rest"]
