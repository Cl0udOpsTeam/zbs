# ZBS — ZooKeeper Backup System

Web-based backup & restore for Apache ZooKeeper ensembles. ZBS periodically dumps the
full znode tree to any S3-compatible bucket and lets you restore any snapshot with one
click. It is Kubernetes-native: the app reads its entire runtime configuration —
ZooKeeper service address, S3 credentials, backup interval, ZK password, … — from a
mounted **ConfigMap** and **Secret**.

The front end and back end are **separate applications/containers**, so you can put
your own auth proxy in front of the UI.

## How it works

```
                    only public entry point
                              |
 browser --HTTP--> [ your auth proxy ] --> zbs-ui (nginx) --+--> SPA static files
                                                           |
                                                           +--> /api/* proxied to
                                                                  zbs-api (internal)
                                                                    |- scheduler
                                                                    |---> ZooKeeper (kazoo)
                                                                    +----> AWS S3 / MinIO (boto3)
```

- **zbs-ui** (front end) – React + TypeScript (Vite) single-page app, served by nginx
  which also reverse-proxies `/api` to the API service, so the browser stays
  same-origin (no CORS). This is the pod you shield with your auth proxy; the UI and
  all of its API calls are then protected by a single gate.
- **zbs-api** (back end) – FastAPI serving only the JSON API. Its Service is internal
  (ClusterIP); browsers never talk to it directly.
- **Multi-cluster folders** – the bucket holds one folder per ZooKeeper cluster,
  conventionally named `ENVIRONMENT-NAMESPACE-ZOOKEEPER_NAME` (e.g.
  `dev-test-my-zookeeper`), producing keys like
  `dev-test-my-zookeeper/zbs-20240501T120000Z.json.gz`. Folders are auto-discovered;
  the UI lets you browse and restore **any** cluster's backups, while new backups of
  this instance's ZooKeeper always land in its own folder (`config.clusterFolder`).
- **Backup** – recursively walks the znode tree from `ZBS_ZK_ROOT`, base64-encodes
  payloads, captures ACLs, and uploads a gzipped JSON snapshot
  (`<prefix>/zbs-20240501T120000Z.json.gz`).
- **Restore** – downloads the chosen snapshot and replays it: missing znodes are
  created, existing ones are overwritten. An optional *wipe* deletes the current
  subtree first. `/zookeeper` is never touched.
- **Scheduler** – takes a backup every `ZBS_BACKUP_INTERVAL_SECONDS` (`0` disables).
  The cadence is fixed: ticks fire every interval of wall-clock time regardless of
  how long the previous backup ran, and an overrun job triggers an immediate
  make-up tick.
- **Retention** – a sweeper deletes backups older than `ZBS_RETENTION_MAX_AGE`
  (e.g. one day, one week, …), running every `ZBS_RETENTION_INTERVAL`. The newest
  `ZBS_RETENTION_MIN_KEEP` backups are always kept. Retention is off unless you set a
  max age. **Safety**: only this instance's own folder is ever cleaned — other
  clusters' folders (visible via browsing) are never touched unless you explicitly
  list them in `config.retentionFolders`.

## Repository layout

```
frontend/
  index.html            Vite entry point
  src/                  React + TypeScript app (components, api client, formatting)
  nginx.conf.template   Static server for dist/ + /api reverse proxy
  Dockerfile            Multi-stage: node build -> nginx runtime
backend/
  app/                  FastAPI application (config, zk, s3, jobs, scheduler, main)
  tests/                Offline round-trip test
  Dockerfile
chart/zbs/              Helm chart: api + ui Deployments/Services,
                        ConfigMap, Secret, OpenShift Route, sidecar hooks
```

## Running locally

API:

```powershell
cd backend
pip install -r requirements.txt

$env:ZBS_ZK_HOSTS = "localhost:2181"
$env:ZBS_S3_BUCKET = "my-bucket"
$env:ZBS_S3_ENDPOINT = "http://localhost:9000"   # e.g. MinIO; omit for AWS S3
$env:ZBS_S3_ACCESS_KEY_ID = "minioadmin"
$env:ZBS_S3_SECRET_ACCESS_KEY = "minioadmin"
$env:ZBS_BACKUP_INTERVAL_SECONDS = "3600"        # 0 disables the scheduler

uvicorn app.main:app --reload --port 8080
# API docs: http://localhost:8080/api/docs
```

UI (Vite dev server or a preview of the production bundle, both proxying `/api`
to the API):

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, hot reload
# or: npm run build && npm run preview   # http://localhost:4173
```

## Building the images

```bash
docker build -t ghcr.io/example/zbs-api:v0.2.0 ./backend
docker build -t ghcr.io/example/zbs-ui:v0.2.0 ./frontend
docker push ghcr.io/example/zbs-api:v0.2.0
docker push ghcr.io/example/zbs-ui:v0.2.0
```

## Installing with Helm

```bash
helm upgrade --install zbs chart/zbs -n zbs --create-namespace \
  --set api.image.repository=ghcr.io/example/zbs-api \
  --set ui.image.repository=ghcr.io/example/zbs-ui \
  --set config.zkHosts=zookeeper-client.default.svc.cluster.local:2181 \
  --set config.s3Bucket=my-zk-backups \
  --set config.backupIntervalSeconds=3600 \
  --set route.enabled=true \
  --set secret.s3AccessKeyId=AKIAXXXX \
  --set secret.s3SecretAccessKey=XXXXXXXX
```

> Note: chart 0.2.x renamed/reorganized resources (separate api/ui deployments and
> services with component selectors). Upgrading directly from chart 0.1.x will fail on
> the immutable Service selector — uninstall the old release first.

To bring your own Secret instead of letting the chart create one:

```bash
kubectl -n zbs create secret generic my-zbs-secrets \
  --from-literal=ZBS_S3_ACCESS_KEY_ID=AKIAXXXX \
  --from-literal=ZBS_S3_SECRET_ACCESS_KEY=XXXXXXXX \
  --from-literal=ZBS_ZK_USERNAME=admin \
  --from-literal=ZBS_ZK_PASSWORD=s3cret

helm upgrade --install zbs chart/zbs -n zbs --create-namespace \
  --set secret.existingSecret=my-zbs-secrets ...
```

Then open the UI:

```bash
kubectl -n zbs port-forward svc/zbs-zbs-ui 8080:80
# http://localhost:8080
# or, with route.enabled=true: oc get route zbs-zbs-ui
```

## Adding your own auth proxy

All browser traffic enters through `zbs-ui`, so gating it protects both the UI pages
and every API call they make. Two common patterns:

**1. Sidecar auth proxy in the UI pod** — the proxy receives traffic first and
forwards to nginx on localhost after authentication:

```yaml
ui:
  # The Service sends traffic to the proxy's port...
  targetPort: "4180"
  sidecars:
    - name: auth-proxy
      image: quay.io/oauth2-proxy/oauth2-proxy:v7.6.0
      args:
        - --http-address=0.0.0.0:4180
        - --upstream=http://127.0.0.1:8080     # nginx container port
        - --provider=...
        - --client-id=...
        - --cookie-secret=...
      ports:
        - name: proxy
          containerPort: 4180
  extraVolumes:
    - name: proxy-config
      secret: {secretName: my-oauth2-proxy}
```

**2. Route-level authentication** — enable the OpenShift Route
(`route.enabled=true`) and add your router's auth annotations under
`route.annotations`. If your proxy terminates TLS itself, use
`route.tls.termination: passthrough` and point `ui.targetPort` at the proxy's port.

Either way, keep the API service internal (default) so it can't be bypassed.

## Configuration reference

| Environment variable           | Chart value                        | Source    | Default             | Description                                        |
| ------------------------------ | ---------------------------------- | --------- | ------------------- | -------------------------------------------------- |
| `ZBS_ZK_HOSTS`                 | `config.zkHosts`                   | ConfigMap | `localhost:2181`    | Comma-separated ZooKeeper client `host:port` list  |
| `ZBS_ZK_ROOT`                  | `config.zkRoot`                    | ConfigMap | `/`                 | Subtree to back up / restore                       |
| `ZBS_ZK_SESSION_TIMEOUT`       | `config.zkSessionTimeout`          | ConfigMap | `15`                | ZK session timeout (seconds)                       |
| `ZBS_BACKUP_INTERVAL_SECONDS`  | `config.backupIntervalSeconds`     | ConfigMap | `0` (disabled)      | Seconds between scheduled backups                  |
| `ZBS_RETENTION_MAX_AGE`        | `config.retentionMaxAge`           | ConfigMap | *(disabled)*        | Delete backups older than this; `0`/empty = off    |
| `ZBS_RETENTION_INTERVAL`       | `config.retentionInterval`         | ConfigMap | `6h`                | How often the retention sweeper runs               |
| `ZBS_RETENTION_MIN_KEEP`       | `config.retentionMinKeep`          | ConfigMap | `1`                 | Newest backups never deleted, even when expired    |
| `ZBS_RESTORE_ACLS`             | `config.restoreAcls`               | ConfigMap | `false`             | Replay stored ACLs on restore                      |
| `ZBS_S3_BUCKET`                | `config.s3Bucket`                  | ConfigMap | *(required)*        | Target bucket                                      |
| `ZBS_S3_REGION`                | `config.s3Region`                  | ConfigMap | `us-east-1`         | AWS region                                         |
| `ZBS_S3_PREFIX`                | `config.s3Prefix`                  | ConfigMap | `zbs/`              | Key prefix for backup objects                      |
| `ZBS_S3_ENDPOINT`              | `config.s3Endpoint`                | ConfigMap | *(AWS S3)*          | Custom endpoint URL (MinIO, RadosGW, …)            |
| `ZBS_S3_FOLDERS`               | `config.s3Folders`                 | ConfigMap | *(auto-discover)*   | Comma-separated folder allow-list                  |
| `ZBS_CLUSTER_FOLDER`           | `config.clusterFolder`             | ConfigMap | *(legacy prefix)*   | Folder this instance uploads its own backups to    |
| `ZBS_RETENTION_FOLDERS`        | `config.retentionFolders`          | ConfigMap | *(own folder only)* | Folders retention may clean up (explicit opt-in)   |
| `ZBS_LISTEN_PORT`              | `config.listenPort`                | ConfigMap | `8080`              | HTTP listen port (API container)                   |
| `ZBS_LOG_LEVEL`                | `config.logLevel`                  | ConfigMap | `INFO`              | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `ZBS_RESTORE_MAX_DEPTH`        | *(env only)*                       | ConfigMap | `256`               | Max znode depth for dumps and restores             |
| `ZBS_RESTORE_MAX_NODES`        | *(env only)*                       | ConfigMap | `0` (unlimited)     | Refuse restoring documents larger than this        |
| `ZBS_RESTORE_MAX_BYTES`        | *(env only)*                       | ConfigMap | `1073741824` (1 GiB)| Max uncompressed size of a restored backup; `0` = off |
| `ZBS_ZK_USERNAME` / `ZBS_ZK_PASSWORD` | `secret.zkUsername` / `secret.zkPassword` | Secret | *(empty)* | Digest auth credentials (optional)          |
| `ZBS_S3_ACCESS_KEY_ID` / `ZBS_S3_SECRET_ACCESS_KEY` | `secret.s3AccessKeyId` / `secret.s3SecretAccessKey` | Secret | *(empty)* | S3 credentials |

Duration values accept plain seconds (`"604800"`) or units — `"45s"`, `"5m"`, `"12h"`
(twelve hours), `"1d"` (one day), `"7d"`/`"1w"` (one week), and compounds like
`"1d12h"`.

### Multi-folder examples

```bash
helm upgrade --install zbs chart/zbs -n zbs --create-namespace \
  --set config.clusterFolder=dev-test-my-zookeeper \
  --set config.s3Folders=dev-test-my-zookeeper,staging-demo-zk,prod-payments-zk
```

- With `clusterFolder` set, "Back up now" and the scheduler write to
  `dev-test-my-zookeeper/…`; every other folder stays browsable/restorable.
- With no `s3Folders`, all root folders in the bucket appear automatically —
  legacy deployments keep working: their old `zbs/` prefix simply shows up as a
  folder named `zbs`.
- Retention cleans only this instance's folder by default; set
  `config.retentionFolders` to widen it.

The UI lists every backup with its creation time (local time + age, exact UTC on
hover) and can filter them by preset ranges or a custom from/to window; retention
state is shown in the header chips and footer.

UI-only settings (nginx env, not in the ConfigMap): `ui.containerPort`
(`ZBS_UI_PORT`), `ui.apiUpstream` (`ZBS_API_UPSTREAM`), plus `ui.replicaCount`,
`ui.sidecars`, `ui.extraVolumes`, `route.*`.

## Backup integrity & error handling

Backups are wrapped in a checksummed envelope: the SHA-256 of the embedded
document is stored alongside it and re-verified on restore. On top of that,
`validate_document()` deep-checks the structure (absolute normalized paths, no
duplicates, strict base64 payloads, well-formed ACLs, depth/node rails).
Downloads and decompression are size-capped (`ZBS_RESTORE_MAX_BYTES`), so a
corrupt or hostile artifact fails fast instead of exhausting memory.

The restore pipeline verifies everything **before** touching ZooKeeper:

```
download from S3 -> gunzip + JSON parse -> checksum verify -> structural validation -> only now connect & mutate ZK
```

A corrupted artifact fails its job with a precise error and the cluster is never
modified. All expected failures use a typed exception hierarchy (`errors.py`)
that maps onto honest HTTP codes (400/404/409/500/503) in the API layer.

## Logging

Every component logs through the `zbs.*` logger tree with timestamps; level is
set once via `ZBS_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`;
invalid values fall back to `INFO` with a warning). Highlights:

- Every line carries the current HTTP request id (`[a1b2c3…]`) for correlation;
  an inbound `X-Request-ID` header is honored, otherwise one is minted and echoed
  back on the response.
- HTTP access logging runs at **INFO**; probe/metrics traffic
  (`/healthz`, `/readyz`, `/metrics`) stays at DEBUG to keep logs readable.
- `DEBUG` — per-node dump/restore traces, S3 keys, scheduler ticks
- `INFO` — job lifecycle with durations, sweep summaries, startup banner (secrets masked)
- `WARNING` — vanished nodes during dumps, invalid-but-recovered config values, partial S3 deletes
- `ERROR` / `CRITICAL` — failed jobs vs. unexpected crashes (with tracebacks)

Example: `helm upgrade ... --set config.logLevel=DEBUG`.

## Metrics

The API exposes Prometheus counters/gauges at `/metrics` (text exposition format,
no extra dependencies):

| Metric | Labels | Meaning |
| --- | --- | --- |
| `zbs_jobs_total` | kind, result | Backup/restore jobs by terminal status |
| `zbs_last_job_duration_seconds` | kind | Duration of the most recent job |
| `zbs_backup_payload_bytes_total` | – | Compressed bytes uploaded by backups |
| `zbs_retention_deleted_total` | – | Backups deleted by retention sweeps |
| `zbs_scheduler_timeouts_total` | – | Scheduled jobs whose wait deadline expired |
| `zbs_http_requests_total` | method, path, status | API requests (route templates only) |
| `zbs_engine_busy` | – | 1 while a backup/restore is running |

Scrape it directly, or set `api.serviceMonitor.enabled=true` when the
prometheus-operator CRDs are installed.

## Testing

```bash
# back end (155 tests): integrity matrix, S3 failure translation, retention
# boundaries, job races, API contracts - all offline via fakes/stubs
cd backend && pip install -r requirements-dev.txt && python -m pytest tests

# front end (69 tests): formatting edge cases, time filters, components, API client
cd frontend && npm install && npm test
```

## Backup format & restore semantics

Snapshots are gzipped JSON (`zbs-backup-v1`): each node stores its path,
base64-encoded data, ACLs and an ephemeral flag. On restore:

- missing znodes are created top-down (parents first), existing ones are overwritten;
- `wipe=true` deletes the subtree under the backup's source root first
  (except `/zookeeper`, which is always protected);
- ephemeral nodes are restored as persistent nodes (ephemerality is session-bound);
- sequential node names keep their sequence suffixes;
- ACLs are only replayed when `ZBS_RESTORE_ACLS=true` or the UI request opts in.

## Operational notes

- **Single API replica by design** – job state lives in memory and only one job runs at
  a time; the chart pins the API to 1 replica with a `Recreate` strategy so upgrades
  never run two schedulers. The UI is stateless and may be scaled freely. Backup
  artifacts themselves are durable in S3.
- **No RBAC needed** – ZBS talks to ZooKeeper and S3, never to the Kubernetes API.
- **Probes** – API: `/healthz` (liveness) and `/readyz` (mandatory config present);
  deep dependency checks are exposed at `/api/status`. UI: nginx `/healthz`.
  Both deployments add a `startupProbe` so slow container starts are not killed.
- **Job history** resets on API pod restart; backups in S3 do not.
- **Resilience primitives** (chart): UI `PodDisruptionBudget` renders when
  `ui.replicaCount > 1`; optional `ui.autoscaling` HPA; optional vanilla-K8s
  `ingress.*` alongside the OpenShift Route. Validate any install with
  `helm test <release>` (smoke-tests `/readyz` and `/healthz`).
