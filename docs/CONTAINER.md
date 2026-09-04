# Container contract

The OCI image is a local execution package. It is not a hosted REST deployment.

## Default CLI target

```bash
docker build --target cli -t flowgrid-agent-memory:0.1.0 .
docker run --rm flowgrid-agent-memory:0.1.0
```

The default invocation runs `flowgrid-memory doctor --ephemeral`, opens no
network listener, writes no persistent database, and exits after the diagnostic.
Run another CLI command by appending its arguments:

```bash
docker run --rm flowgrid-agent-memory:0.1.0 demo --ephemeral
```

For a persistent database, mount a directory and pass an explicit absolute path
inside the container. The image runs as UID/GID 10001; the mounted directory
must be writable by that identity.

## MCP stdio target

```bash
docker build --target mcp -t flowgrid-agent-memory:mcp-0.1.0 .
docker run --rm -i \
  -v "$PWD/data:/data" \
  -v "$PWD/config:/config:ro" \
  flowgrid-agent-memory:mcp-0.1.0 \
  --db /data/memory.db \
  --principal-config /config/mcp-principal.json
```

The MCP target communicates over stdin/stdout and publishes no TCP port.

For a synthetic end-to-end check, install the optional MCP SDK on the host and
run the official-client probe against the built image:

```bash
python scripts/smoke_mcp.py --container-image flowgrid-agent-memory:mcp-0.1.0
```

This starts an isolated container with networking disabled, an in-memory
database, and only a temporary synthetic principal file mounted read-only. It
checks tool discovery, ingestion, candidate extraction, the owner gate,
cross-user/cross-scope denial, and stderr privacy. No candidate is confirmed.
The named test container is removed on success or failure; cleanup errors fail
the probe. CI and release verification use this session, not just `--help`.

## REST boundary

The verified REST adapter accepts only the literal `127.0.0.1` bind address. In
a normal bridged container, that address belongs to the container namespace and
is not a reliable host-published service. The Dockerfile therefore provides no
REST target and declares no exposed port. Run `flowgrid-memory-rest` directly on
the trusted host with the documented configuration. A future container-network
mode requires a separate threat model and release contract.
