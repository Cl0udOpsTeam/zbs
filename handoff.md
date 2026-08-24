# ZBS — Engineering Handoff

> ZooKeeper Backup System — web-based backup & restore for Apache ZooKeeper ensembles,
> deployed to Kubernetes/OpenShift via Helm. This document summarizes the project
> architecture, a full audit performed in August 2026, and the critical fixes applied
> during that audit. For usage/deployment instructions see `README.md`.

---

## 1. What ZBS is

ZBS periodically dumps a ZooKeeper znode tree to any S3-compatible bucket (AWS S3,
MinIO, RadosGW) and lets you browse and restore any snapshot with one click.

```
browser ──HTTP──> [your auth proxy] ──> zbs-ui (nginx, SPA + /api reverse proxy)
                                            └──/api──> zbs-api (FastAPI, ClusterIP)
                                                          ├── scheduler ──> ZooKeeper (kazoo)
                                                          ├── retention sweeper ──> S3 (boto3)
                                                          └── job engine (in-memory)
```

- **Two containers**: `zbs-api` (Python 3.12, FastAPI, single replica by design —
  job state lives in process memory) and `zbs-ui` (React 19 + TypeScript + Vite SPA
  served by nginx-unprivileged; stateless, freely scalable).
- **Multi-cluster buckets**: one folder per cluster
  (`ENVIRONMENT-NAMESPACE-ZOOKEEPER_NAME`); any folder is browsable/restorable;
  this instance writes only to its own `config.clusterFolder`.
- **Safety model** (the heart of the system):
  `download → gunzip+JSON → SHA-256 checksum verify → structural validation →
  only now connect & mutate ZooKeeper`. Corrupt artifacts fail before touching ZK.
  Retention only ever deletes inside explicitly-configured folders.
- **Backup format** (`zbs-backup-v1`): gzipped JSON envelope
  `{format, checksum: sha256(canonical JSON of document), document}`; document holds
  path/data_b64/acls/ephemeral per node. `/zookeeper` is never dumped, wiped or restored.
- **No RBAC needed** (never talks to the K8s API); no built-in auth — put your own
  auth proxy (e.g. oauth2-proxy sidecar) in front of the UI pod.

### Repository layout

```
backend/
  app/
    config.py      env-driven Settings (durations, clamping, secrets excluded from banner)
    zk.py          dump/restore tree walks, serialize/deserialize envelope, validation rails
    s3.py          boto3 client, error taxonomy translation, pagination, batched deletes
    jobs.py        in-memory job engine (single-flight lock, 100-entry history), backup/restore ops
    scheduler.py   interval-based backup thread (interval restarts after each job)
    retention.py   max-age sweeper with min-keep guarantee and folder scoping
    errors.py      typed exception hierarchy -> honest HTTP codes
    main.py        FastAPI app, endpoints, probes (/healthz /readyz), logging middleware
  tests/           ~193 offline tests (FakeZK/FakeS3 fakes, fault injection, API contracts)
frontend/
  src/
    App.tsx        single-screen dashboard: all state + polling lives here
    api.ts         fetch client over same-origin /api
    hooks.ts       useInterval (visibility-gated polling), isAbortError
    components/    Header (config chips), BackupsCard (table/filters/restore),
                   JobsCard (job history), StatusFooter (health/scheduler/retention line)
    toast.tsx      toast context stack
  nginx.conf.template  static SPA + /api proxy (envsubst at container start)
chart/zbs/         Helm chart (api/ui Deployments+Services, ConfigMap, Secret, Route,
                   ServiceAccount without token automount, hardened security contexts)
```

### Configuration

Everything comes from env vars injected by Helm (`config.*` → ConfigMap, `secret.*`
→ Secret). Full table in README.md. Highlights: `ZBS_ZK_HOSTS`, `ZBS_S3_BUCKET`,
`ZBS_BACKUP_INTERVAL_SECONDS`, `ZBS_RETENTION_*`, `ZBS_CLUSTER_FOLDER`,
restore rails `ZBS_RESTORE_MAX_DEPTH` (256), `ZBS_RESTORE_MAX_NODES` (0=unlimited),
and the new `ZBS_RESTORE_MAX_BYTES` (see §3).

---

## 2. Audit summary (August 2026)

A three-way review of backend, frontend, and chart identified issues grouped as:

| Severity | Theme | Examples |
|---|---|---|
| Critical | runtime correctness | RO rootfs without writable volumes; event-loop blocking; unbounded decompression; job-history leak; frontend stale-response races |
| Security | hardening | no nginx security headers/gzip; plain-value secrets; Route TLS off by default; silent ACL downgrade on restore; digest cred f-string |
| Ops | maturity | scheduler drift/silent 6h deadline expiry; no /metrics or request IDs; probes lack startupProbe; no PDB/HPA/Ingress; unpinned deps |
| Health | code/UX | no linters anywhere; scheduler module untested; window.confirm for destructive restore; toast a11y |

The **Critical track was fully treated** (§3). The remaining tracks are documented
as future work in §5.

---

## 3. Critical fixes applied (this handoff's change set)

### Fix 1 — Chart: writable volumes for read-only root filesystems
**Files:** `chart/zbs/templates/deployment-api.yaml`, `deployment-ui.yaml`

Both pods run with `readOnlyRootFilesystem: true` but previously mounted no writable
volumes — Python's `tempfile` (API) and nginx temp/cache dirs (UI) would hit
read-only errors at runtime.

- API container now mounts an `emptyDir` named `tmp` at `/tmp`.
- UI container mounts the same `tmp` emptyDir at `/tmp` plus an `nginx-cache`
  emptyDir at `/var/cache/nginx`.
- UI `extraVolumes` values are merged into the same `volumes:` list (no duplicate keys).
- Verified with `helm lint` (0 failures) and rendered output incl. user-supplied extraVolumes.

### Fix 2 — Backend: blocking calls off the asyncio event loop
**File:** `backend/app/main.py`

`GET /api/status` (live ZK connect + S3 head_bucket), `GET /api/clusters`
(discovery) and `GET /api/backups` (listing) executed blocking kazoo/boto3 calls
inside `async def` handlers — one slow dependency stalled *all* requests for up to
~120 s. Those three handlers are now plain `def`; FastAPI runs them on its
threadpool. Cheap handlers (`/healthz`, `/readyz`, `/api/config`, `/api/jobs`,
job submissions that return immediately) remain async. No contract changes.

### Fix 3 — Backend: decompression-bomb protection + leaner serialization
**Files:** `backend/app/config.py`, `backend/app/zk.py`, `backend/app/s3.py`, `README.md`

- New setting **`ZBS_RESTORE_MAX_BYTES`** — max uncompressed size of a restored
  backup, default `1073741824` (1 GiB), `0` disables, clamped like the other rails.
  Documented in the README config table and shown in the startup banner's
  "restore limits" line.
- `zk.deserialize()` now stream-decompresses in 1 MiB chunks via new helper
  `_gunzip_bounded()`; exceeding the cap raises typed `BackupValidationError`
  instead of risking OOM on hostile/corrupt artifacts.
- `s3.download_backup()` streams the object body with the same cap (+64 KiB gzip
  framing allowance) so oversized objects are refused mid-download.
- `zk.serialize()` builds the envelope by splicing the canonical document JSON into
  the wrapper bytes — the document text exists once in memory instead of twice.
  Output is byte-compatible with the previous dict-based format (verified by test).

### Fix 4 — Backend: job-history memory leak
**File:** `backend/app/jobs.py`

`_order` (deque, maxlen=100) forgot old ids but `_jobs` entries lived forever.
New `_prune_locked()` helper runs under the existing lock on every submit and drops
`_jobs` entries evicted from `_order`. `get_job()` now correctly returns None (404)
for pruned jobs.

### Fix 5 — Frontend: stale-response races + silent poll failures + wasted polling
**Files:** `frontend/src/api.ts`, `hooks.ts`, `App.tsx`, `components/JobsCard.tsx`,
`components/StatusFooter.tsx`

- **Race guard:** every backups load aborts the previous one (`AbortController` ref);
  aborted/stale responses can never overwrite the current folder's list. Unmount
  aborts in-flight loads. `api.listBackups()` accepts an `AbortSignal`.
- **Failure surfacing:** jobs/status polls no longer swallow errors. JobsCard shows
  "Job list unavailable (retrying): …"; StatusFooter shows "status refresh failed
  (retrying): …" and flips to warn styling; last-known-good data stays visible.
  Recovery clears the notices automatically.
- **Visibility gating:** `useInterval` pauses ticks while the tab is hidden and
  fires one immediate catch-up call when it becomes visible again (stops 2.5 s
  background-tab hammering).
- Minor hygiene: no `Content-Type` header on GET requests.

### Tests added

| Suite | Coverage |
|---|---|
| `backend/tests/test_integrity.py::TestSizeLimits` (5 tests) | bomb rejection under tiny cap, cap disabled, oversize never reaches ZK, spliced envelope matches reference format, legacy dict-built envelope still accepted |
| `backend/tests/test_s3_errors.py::TestDownloadCap` (3 tests) | oversized download rejected (incl. 64 KiB framing allowance semantics), within-cap download works, unlimited mode works |
| `backend/tests/test_jobs.py::TestHistoryPruning` (3 tests) | prune drops evicted ids, noop below capacity, 103 sequential live submissions leave exactly newest 100 |
| `frontend/src/App.test.tsx` (4 tests) | default-cluster selection + aborted mount-load can't paint late; fast dev→prod→dev switching ignores stale response; job-poll failure surfaces while backups still render; status-poll failure degrades footer then recovers (fake timers) |
| `frontend/src/hooks.test.tsx` (1 test) | interval ticks when visible, stops hidden, immediate catch-up on re-visible |

### Verification results

```
backend:    python -m pytest tests       -> 193 passed
frontend:   npm test                     -> 81 passed (6 files)
frontend:   npm run build                -> tsc clean, vite build OK (~205 kB js)
chart:      helm lint chart/zbs          -> 0 failed
            helm template ...            -> tmp/nginx-cache volumes render; extraVolumes merge OK
```

---

## 4. Known limitations after these fixes (by design / deferred)

- **Single API replica is still mandatory** — job state is in-memory and the busy
  lock is process-local. The chart pins replicas=1 with Recreate strategy.
- **Restores are not transactional** — wipe commits first; a mid-restore failure can
  leave a partially-restored tree (pre-existing behavior; fix requires journaling).
- **Integrity ≠ authenticity** — checksums protect against corruption, not tampering;
  legacy pre-checksum envelopes are still accepted (with a warning).
- Job history resets on pod restart (artifacts in S3 are durable).

## 5. Recommended next steps (prioritized backlog)

1. **Security hardening (high value, cheap):**
   - nginx: add `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
     `frame-ancestors 'none'`, `Referrer-Policy`; enable gzip/brotli (bundle is
     ~205 kB raw / 65 kB gzipped); cache hashed `/assets/*` immutable, keep
     `no-store` only for `index.html`.
   - Chart: NetworkPolicy restricting traffic to the internal API; default Route TLS
     to edge+Redirect; consider external-secrets support.
   - Optional bearer-token auth on mutating endpoints (`POST /api/backups`, `/api/restore`).
   - Escape `:` in ZK digest credentials (`zk.py` f-string); reconsider default
     OPEN_ACL_UNSAFE restore behavior.
2. **Ops maturity:** Prometheus `/metrics` + request IDs; startupProbe + probe tuning;
   HPA/PDB for UI (replicas ≥ 2); optional Ingress for vanilla K8s; pin deps
   (lock file/hashes) and base image digests; `helm test` hook + `values.schema.json`.
3. **Reliability:** make restore resumable/journaled; conditional S3 puts
   (`IfNoneMatch`) against second-granularity key collisions; scheduler deadline
   telemetry; retry-with-backoff in frontend polls.
4. **Code health:** ruff+mypy (backend), ESLint typescript-eslint+react-hooks and
   Prettier (frontend); dedicated scheduler tests; replace hand-rolled polling with
   TanStack Query if scope grows; accessible modal replacing `window.confirm`;
   aria-live toasts; list virtualization for very long bucket histories.
