# AML dedicated service deployment

本包准备 AML 第二届 Textual/Open-source 的公网服务交付。当前只有本机合成 HTTP 验证；公网部署、持续运行和官方 Smoke/Full 尚未发生。安装和部署使用已核对的固定版本 wheel，凭据与参赛数据保存在独立主机，绝不放进源码包。

## Security and compatibility boundary

The dedicated command is `flowgrid-memory-aml` (or
`python -m aml_retriever.aml_hosted`). It exposes exactly:

| Method and route | Authentication | Response |
| --- | --- | --- |
| `GET /health` | public | transport liveness and honest code/build identity |
| `POST /add` | mandatory external credential | official synchronous Add response |
| `POST /search` | mandatory external credential | official ranked evidence `data` array |

`/stats`, `/admin/delete_user`, governed REST routes and host adapters are not
registered. The legacy `aml_retriever.server` remains a compatibility/testing
tool and must not be used as the public listener. The new server binds only
to loopback; a reverse TLS proxy owns the public interface.

Add/Search continue to call `MemoryService.official_add` and
`MemoryService.official_search`. Ranking, first-write-wins retry semantics,
user isolation and Top K 100 are unchanged. No extractor runs and no derived
candidate becomes confirmed. The evaluator's credential grants access to the
dedicated competition database across its supplied user IDs; it is not a
multi-tenant credential for general product users. Keep this database isolated
from personal/project memory.

Hosted startup requires exactly one of `AML_HOSTED_KEY` or
`AML_HOSTED_KEY_FILE`, plus an absolute `AML_HOSTED_DB_PATH`. Blank keys,
insecure auth modes, ambiguous key sources and invalid bounds fail startup.
A key file must be a regular absolute-path file with no group/other permission
bits on a POSIX host. Key-file configuration fails closed on Windows because
mode bits cannot attest Windows ACLs; Windows local rehearsals use the
environment credential. Bearer is the default; `token` and `x-api-key` are supported via
`AML_HOSTED_AUTH_MODE`. Supply a high-entropy key out of band. The CLI does not
accept a secret argument. Legacy `AML_CONFIG`, `AML_AUTH_MODE` and `AML_API_KEY`
cannot disable hosted authentication.

Errors use fixed JSON `{"detail":{"reason":"..."}}`, including parser and
validation failures. Runtime failure returns 500; it never masquerades as a
successful empty Search. Logs contain only a normalized route, status and
elapsed time. Request bodies, raw URLs, user/request/session IDs, credentials,
exception text and database paths are absent.

## Resource and retry behavior

Default bounds: 32 MiB body, 32 active adapter operations, 96 accepted
connections, 10 seconds idle socket read timeout, 60 seconds absolute
connection lifetime and 45 seconds adapter-operation wait. All are validated
and configurable within finite limits. Saturation returns 429 with
`Retry-After: 1`; concurrency rejection happens before buffering the body.
Oversize requests return 413. Conflicting Content-Length, Transfer-Encoding,
duplicate auth headers, duplicate JSON keys and NaN/Infinity are rejected.

The operation pool retains its occupied slot until work actually ends, even
when the client receives 504 or disconnects. Python threads cannot safely
cancel an already-running database operation. An Add that times out may
finish and commit; retry the identical `(request_id, user_id)` to learn its
result without duplicating memory. Do not change IDs on timeout. A stuck
operation remains bounded by the pool and requires operator investigation or
service restart. The template restarts crashed processes, bounds RAM/tasks,
and allows 90 seconds for graceful stop; forced termination can roll back a
SQLite transaction. Stable storage and SQLite WAL recovery remain required.

These are bounded transport and admission controls, not measured public
capacity. The earlier legacy loopback 64 Add / 32 Search observation does not
establish hosted-wrapper throughput. Configure organizer concurrency within
the measured capacity and include 429 retries in hosted rehearsals.

## Release identity and offline installation

The service reports the package product version and separate AML adapter
version. A release wheel embeds `aml_retriever/release_identity.json`:

```json
{
  "schema": "flowgrid.agent-memory.release-identity/v1",
  "product_version": "0.1.1",
  "adapter_version": "1.1.0",
  "source_digest_sha256": "<64 lowercase hex characters>",
  "runtime_digest_sha256": "<64 lowercase hex characters>",
  "runtime_files": ["<reviewed runtime package paths in sorted order>"],
  "source_commit": "<40 lowercase hex characters or null>",
  "source_state": "committed"
}
```

The release builder owns the source allowlist and digest computation. An
uncommitted snapshot reports `source_state=uncommitted` and a null commit;
it cannot impersonate the old HEAD. Environment variables cannot override the
reported version or commit. A checkout without a release manifest remains a
development instance. `AML_HOSTED_REQUIRE_BUILD_IDENTITY=1` requires a committed
release manifest and validated version/schema/hash formats. Artifact authenticity
comes from the reviewed wheel SHA-256 and corresponding public source commit,
not from trusting an arbitrary JSON file.

At startup the service recomputes `runtime_digest_sha256` from installed
Python source, `py.typed`, the fixed governance fixture and lexical baseline.
The file set must exactly match the reviewed runtime allowlist and manifest;
modified, added, missing or symlinked runtime files/resources fail startup.
The manifest is excluded from its own digest. Runtime digest input is a sorted
list of `{path, sha256, size_bytes}` records, serialized using sorted-key,
indented UTF-8 JSON plus one trailing newline. An installed package with no
manifest or runtime attestation fails closed. This detects drift after the
verified wheel is installed; it does not replace the external artifact hash
or protect against an administrator modifying both the verifier and manifest.

The deployment unit enables this strict release mode. Before deployment,
freeze/publish the source, build/verify the release artifacts, independently
record the wheel SHA-256 and install it offline:

```sh
bash deploy/aml/install-wheel.sh \
  /absolute/path/flowgrid_agent_memory-0.1.1-py3-none-any.whl \
  REVIEWED_WHEEL_SHA256 \
  /opt/flowgrid-memory-aml/venv
```

`AML_INSTALL_PYTHON` may select an absolute Python 3.11+ executable. The
installer checks the wheel digest, uses `--no-index --no-deps --force-reinstall`, validates
committed provenance, and does not configure credentials or start a service.
When an existing virtual environment already contains the same product
version, the reviewed wheel is still reinstalled; an older build with the same
version number cannot satisfy this install step by remaining in place.
The default build is zero-dependency lexical retrieval; no embedding weights,
remote model calls or downloads are implied by Health.

## Linux TLS deployment template

The files in `deploy/aml/` target a dedicated Linux host with Python 3.11+,
systemd supporting `LoadCredential` (for example Ubuntu 24.04), nginx and a
valid TLS certificate. An operator must review/install them for the chosen
host; this repository does not execute those steps automatically.

1. Create a dedicated unprivileged `flowgrid-memory-aml` account. Install the
   hash-pinned wheel under `/opt/flowgrid-memory-aml/venv`.
2. Create `/etc/flowgrid-memory-aml/` and a root-owned mode-0600 `aml.key` using
   the approved external credential. Keep it outside Git, logs and chat.
3. Copy the reviewed `service.env.example` as
   `/etc/flowgrid-memory-aml/service.env`; it contains only non-secret bounds.
4. Install `flowgrid-memory-aml.service`. `StateDirectory` persists the SQLite
   database in `/var/lib/flowgrid-memory-aml`; `LoadCredential` supplies a
   protected per-service key file. Restart preserves accepted memory.
5. Replace domain/certificate placeholders in `nginx.conf.example`. Expose
   only TCP 443 publicly; keep port 8081 loopback-only. Verify nginx syntax,
   certificates and firewall before starting/reloading the proxy. Access logs
   are disabled; error logging is limited to critical failures. Review
   host/proxy journaling and monitoring to avoid request/payload retention.
6. Run external synthetic smoke, concurrent retry rehearsal and a sustained
   soak. Check restart persistence, TLS, 429 behavior, resource ceilings,
   isolated storage, actual installed identity and live endpoint availability.
   Review real host metrics before declaring capacity to AML.
7. Submit the approved endpoint/credential to official Smoke. Record platform
   acceptance separately from local tests. Consume Full only after this gate.

The example does not request certificates, open firewall ports, provision a
paid host, submit organizer forms or use an Evaluation Key.

## Local verification and external probes

```sh
python3.11 -m unittest tests.test_aml_hosted -v
python3.11 scripts/smoke_aml_hosted.py
python3.11 scripts/rehearse_aml_cycle2.py --hosted \
  --users 64 --checkpoints 10 --add-concurrency 64 \
  --search-concurrency 32 --search-rounds 10
```

The default smoke starts a real loopback HTTP service, generates an ephemeral
credential, exercises synthetic Add/Search/auth/isolation/retry/parser/error
boundaries and deletes the temporary database. Its aggregate report contains
no endpoint URL, credential, memory content or identifiers.

The concurrency rehearsal defaults to the legacy adapter unless `--hosted`
is supplied. Hosted mode uses a fresh in-memory credential and the dedicated
32-operation admission limit. A 429 retries the same IDs and payload after
the integer `Retry-After` delay, with at most six attempts, a 60-second logical
deadline and a maximum five-second accepted delay. Invalid headers or
exhausted bounds fail the local gate. Reports distinguish duplicate logical
Add deliveries from physical HTTP attempts and 429 counts. Logical latency
includes retry waits; per-attempt latency is reported separately. Public HTTPS
and official acceptance remain explicitly false.

On 2026-10-03, a real hosted-loopback run with 64 synthetic users, ten
checkpoints, 64 Add workers and 32 Search workers completed 640 unique writes.
Its 1,280 logical Add deliveries required 1,335 attempts, including 55 handled
429 responses; 1,344 logical Search requests used 1,344 attempts. Maximum
attempts per logical request was two. There were zero logical failures,
transport errors, unexpected statuses or exhausted retries. Immediate
visibility, idempotency, Top K 100, user isolation and temporary database
cleanup all passed. Logical Add p95 was 345.126 ms (maximum 1,220.269 ms),
Search p95 was 406.416 ms (maximum 646.569 ms), and the run took 17.705 seconds.
These observations describe this short local synthetic run; they do not
establish sustained or public-host capacity.

For an explicitly chosen candidate HTTPS endpoint, place its Bearer
credential in the selected environment variable and run:

```sh
python3.11 scripts/smoke_aml_hosted.py \
  --base-url https://memory.example.org \
  --credential-env AML_HOSTED_KEY
```

The smoke refuses HTTP redirects so its credential cannot follow a redirect
to a different destination. The external smoke retains its synthetic records because the public service
has no deletion endpoint. Cleanup is a local operator action on the dedicated
competition database. Never point this probe at a personal/project memory
database. Non-HTTPS remote targets and URL-embedded credentials are rejected.

## Remaining owner inputs and evidence

Required inputs are the deployment host, domain/TLS, resource budget, permitted
network access, public frozen source commit, service credential and organizer
Evaluation Key. Host-specific deployment and official submission remain
external gates. Public HTTPS, multi-hour soak, official Smoke/Full, and an AML
score are currently unverified. Health proves HTTP liveness and build identity;
retrieval quality requires separate source-backed evaluation.
