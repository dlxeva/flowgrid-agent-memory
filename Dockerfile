# Multi-architecture OCI index digest resolved from Docker Official Images.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/flowgrid-memory
COPY pyproject.toml README.md LICENSE /opt/flowgrid-memory/
COPY aml_retriever /opt/flowgrid-memory/aml_retriever
COPY flowgrid_memory /opt/flowgrid-memory/flowgrid_memory
RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 flowgrid

USER flowgrid

# Optional stdio target. It opens no TCP listener and installs the only optional
# runtime dependency group.
FROM runtime AS mcp
USER root
RUN python -m pip install --no-cache-dir '.[mcp]'
USER flowgrid
ENTRYPOINT ["flowgrid-memory-mcp"]

# Default image target: a non-networked, self-cleaning diagnostic. The verified
# REST boundary remains a literal host loopback service and intentionally has no
# container target or exposed port.
FROM runtime AS cli
ENTRYPOINT ["flowgrid-memory"]
CMD ["doctor", "--ephemeral"]
