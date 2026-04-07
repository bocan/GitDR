# GitDR - Specification

> A self-hosted tool for backing up and restoring Git repositories across multiple forges and storage backends.

---

## 1. Overview

GitDR provides scheduled and on-demand backups of Git repositories from multiple source forges (GitHub, GitLab, Azure DevOps, Bitbucket, generic Git) to multiple storage destinations (local filesystem, S3, Azure Blob Storage, GCS). It maintains an encrypted SQLite database of sources, destinations, jobs, and run history, and exposes a web UI for configuration, monitoring, and restore operations.

### 1.1 Design Principles

- **Encryption by default** - All credentials encrypted at rest (field-level Fernet + SQLCipher database encryption).
- **Git-native backups** - Uses `git clone --mirror` and `git bundle` to produce portable, verifiable backup artifacts.
- **Incremental where possible** - Maintains a local mirror cache; subsequent backups only fetch deltas.
- **Minimal dependencies** - Single-container deployment, SQLite storage, no external message queue required.
- **Forge-aware** - Discovers repositories automatically via forge APIs rather than requiring manual registration.

### 1.2 Non-Goals (For Now)

- Multi-user / multi-tenant access control.
- Backing up non-Git assets (wikis, issues, PRs, CI config) - may be added later.
- Real-time / continuous backup (webhook-triggered). Scheduled + manual is sufficient.

---

## 2. Tech Stack

| Layer            | Technology                             | Pinned Version          | Rationale                                                              |
|------------------|----------------------------------------|-------------------------|------------------------------------------------------------------------|
| Language         | Python                                 | 3.14.3                  | Mature ecosystem for git, cloud SDKs, and web frameworks.              |
| Web Framework    | FastAPI                                | 0.135.3                 | Async, typed, auto-generated OpenAPI docs.                             |
| Frontend         | FastAPI + Jinja2 + HTMX                | -                       | Single-container, no JS build step, interactive without SPA.           |
| CSS              | Tailwind CSS (CDN)                     | latest CDN              | Utility-first, no build pipeline needed.                               |
| Database         | SQLite via SQLCipher                   | -                       | Encrypted at rest, zero-ops, single-file.                              |
| SQLCipher binding| `sqlcipher3`                           | latest                  | Python SQLCipher driver; compiled against `libsqlcipher-dev`.          |
| ORM              | SQLModel                               | 0.0.38                  | Pydantic + SQLAlchemy hybrid, plays well with FastAPI.                 |
| Encryption       | `cryptography` (Fernet)                | 46.0.6                  | Field-level encryption for credentials within the database.            |
| Scheduling       | APScheduler v4 (**pre-release**)       | 4.0.0a6                 | In-process, async-native; pin this exact version, monitor for GA.     |
| Git Operations   | Git CLI (subprocess)                   | system git              | `--mirror`, `bundle`, `remote update` - more reliable than libs.       |
| Storage: Local   | stdlib (`shutil`, `pathlib`)           | -                       | No dependency needed.                                                  |
| Storage: S3      | `boto3`                                | 1.42.83                 | Standard AWS SDK.                                                      |
| Storage: Azure   | `azure-storage-blob`                   | 12.28.0                 | Standard Azure SDK.                                                    |
| Storage: GCS     | `google-cloud-storage`                 | 3.10.1                  | Standard GCP SDK.                                                      |
| Containerisation | Docker + Docker Compose                | -                       | Single-container deployment with volume-mounted SQLite.                |
| Testing          | pytest + pytest-asyncio + httpx        | 9.0.2 / 1.3.0 / 0.28.1 | Test runner, async test support, FastAPI TestClient.                   |
| Linting/Format   | ruff                                   | latest                  | Fast linting and formatting (replaces flake8 + black).                 |
| Type checking    | mypy                                   | latest                  | Static type verification enforced in CI.                               |

> **APScheduler v4 note:** The v4.x series is still in alpha (4.0.0a6 as of writing). The upstream authors explicitly warn against production use. We accept this risk given the superior async-native design. Pin the exact version in `pyproject.toml`, run the full test suite on every dependency update, and track the upstream changelog for breaking changes before upgrading.

---

## 3. Data Model

### 3.1 `git_sources`

Represents a forge or Git server from which repositories are discovered and cloned.

| Column           | Type         | Notes                                                            |
|------------------|--------------|------------------------------------------------------------------|
| `id`             | UUID (PK)    | Auto-generated.                                                  |
| `name`           | TEXT         | Human-readable label. Unique.                                    |
| `forge_type`     | TEXT         | Enum: `github`, `gitlab`, `azure_devops`, `bitbucket`, `generic`.|
| `base_url`       | TEXT         | API base URL. e.g. `https://api.github.com`.                     |
| `auth_type`      | TEXT         | Enum: `pat`, `ssh_key`, `app_token`, `oauth`.                    |
| `auth_credential`| BLOB         | Fernet-encrypted. Contains token, key material, or OAuth blob.   |
| `org_or_group`   | TEXT         | Optional. GitHub org, GitLab group, Azure DevOps project.        |
| `verify_ssl`     | BOOLEAN      | Default `true`. Set `false` for self-hosted with dodgy certs.    |
| `created_at`     | DATETIME     | Auto-set.                                                        |
| `updated_at`     | DATETIME     | Auto-updated.                                                    |

### 3.2 `repositories`

Internal cache of repositories discovered from a source. **Not directly managed by users** — populated and refreshed by the discovery service whenever a job's repo picker is opened or a manual refresh is triggered. Repo selection (what a job backs up) is stored on the job itself, not here.

| Column           | Type         | Notes                                                            |
|------------------|--------------|------------------------------------------------------------------|
| `id`             | UUID (PK)    | Auto-generated.                                                  |
| `source_id`      | UUID (FK)    | References `git_sources.id`. Cascade delete.                     |
| `repo_name`      | TEXT         | Full name, e.g. `org/repo-name`.                                 |
| `clone_url`      | TEXT         | HTTPS or SSH clone URL.                                          |
| `default_branch` | TEXT         | Informational metadata from the forge API. Backup always captures all refs (mirror). |
| `description`    | TEXT         | From forge API. Nullable.                                        |
| `is_archived`    | BOOLEAN      | Whether the repo is archived on the forge.                       |
| `last_seen_at`   | DATETIME     | Updated each time discovery runs. Used to detect repos removed from the forge. |
| `created_at`     | DATETIME     | Auto-set.                                                        |

**Unique constraint:** `(source_id, repo_name)`.

> **Note:** The `excluded` boolean (from the original design) has been removed. Per-job repo selection replaces it — see `backup_jobs.included_repos`.

### 3.3 `backup_destinations`

A storage target where backup artifacts are written.

| Column           | Type         | Notes                                                            |
|------------------|--------------|------------------------------------------------------------------|
| `id`             | UUID (PK)    | Auto-generated.                                                  |
| `name`           | TEXT         | Human-readable label. Unique.                                    |
| `dest_type`      | TEXT         | Enum: `local`, `s3`, `azure_blob`, `gcs`, `sftp`.               |
| `config`         | BLOB         | Fernet-encrypted JSON. Contents vary by `dest_type` (see §3.3.1).|
| `created_at`     | DATETIME     | Auto-set.                                                        |
| `updated_at`     | DATETIME     | Auto-updated.                                                    |

#### 3.3.1 Destination Config Schemas (decrypted JSON)

**Local:**
```json
{
  "base_path": "/backups/gitdr"
}
```

**S3:**
```json
{
  "bucket": "my-git-backups",
  "prefix": "gitdr/",
  "region": "eu-west-2",
  "access_key_id": "AKIA...",
  "secret_access_key": "...",
  "endpoint_url": null
}
```

**Azure Blob:**
```json
{
  "container": "git-backups",
  "prefix": "gitdr/",
  "connection_string": "DefaultEndpointsProtocol=https;..."
}
```

**GCS:**
```json
{
  "bucket": "my-git-backups",
  "prefix": "gitdr/",
  "service_account_json": "{ ... }"
}
```

### 3.4 `backup_jobs`

Defines what to back up, where to, and when. **Repo selection lives here**, not on the source or individual repo records.

| Column            | Type         | Notes                                                            |
|-------------------|--------------|------------------------------------------------------------------|
| `id`              | UUID (PK)    | Auto-generated.                                                  |
| `name`            | TEXT         | Human-readable label.                                            |
| `source_id`       | UUID (FK)    | References `git_sources.id`.                                     |
| `destination_id`  | UUID (FK)    | References `backup_destinations.id`.                             |
| `schedule_cron`   | TEXT         | Cron expression. Nullable (manual-only if null).                 |
| `included_repos`  | TEXT         | JSON array of `repo_name` strings to back up. e.g. `["org/api", "org/web"]`. **Empty array or null = back up all repos from the source.** Set during job create/edit via the inline repo picker. |
| `backup_type`     | TEXT         | Enum: `mirror` (all refs, default), `selective` (branch-filtered). Displayed clearly in the UI, not buried as a code value. |
| `branch_filter`   | TEXT         | JSON array of glob patterns. e.g. `["main", "release/*"]`. Only applies when `backup_type = selective`. Null = all branches. |
| `archive_format`  | TEXT         | Enum: `bundle`, `tar_zstd`. Default `bundle`.                   |
| `retention_count` | INTEGER      | Number of backup generations to retain per repo. 0 = unlimited. |
| `include_archived`| BOOLEAN      | Whether to back up archived repos. Default `false`.              |
| `enabled`         | BOOLEAN      | Default `true`. Disabled jobs are skipped by the scheduler.      |
| `created_at`      | DATETIME     | Auto-set.                                                        |
| `updated_at`      | DATETIME     | Auto-updated.                                                    |

> **`backup_mode` renamed to `backup_type`** for clarity. The UI labels it as **"Mirror — all refs"** or **"Selective — branch filter"** so the distinction between ref-scope and repo-scope is unambiguous.

> **`repo_filter` removed.** The glob-pattern `repo_filter` field added during Phase 4 is superseded by `included_repos` (an explicit list of repo names chosen interactively, not typed as globs).

### 3.5 `backup_runs`

A record of each backup execution, per-repo per-job.

| Column           | Type         | Notes                                                            |
|------------------|--------------|------------------------------------------------------------------|
| `id`             | UUID (PK)    | Auto-generated.                                                  |
| `job_id`         | UUID (FK)    | References `backup_jobs.id`.                                     |
| `repo_id`        | UUID (FK)    | References `repositories.id`.                                    |
| `status`         | TEXT         | Enum: `pending`, `running`, `success`, `failed`, `skipped`.     |
| `trigger`        | TEXT         | Enum: `scheduled`, `manual`.                                     |
| `started_at`     | DATETIME     | Nullable (set when status moves to `running`).                   |
| `completed_at`   | DATETIME     | Nullable (set on terminal status).                               |
| `duration_secs`  | REAL         | Computed from started/completed.                                 |
| `size_bytes`     | INTEGER      | Size of the backup artifact.                                     |
| `archive_path`   | TEXT         | Full path/key in the destination. e.g. `gitdr/org/repo/2025-04-06T12:00:00.bundle`. |
| `ref_manifest`   | TEXT         | JSON object: `{"branches": [...], "tags": [...]}`.              |
| `checksum_sha256`| TEXT         | SHA-256 hex digest of the backup artifact.                       |
| `error_message`  | TEXT         | Nullable. Populated on failure.                                  |
| `created_at`     | DATETIME     | Auto-set.                                                        |

---

## 4. Encryption Architecture

### 4.1 Database-Level Encryption

The SQLite database file is encrypted at rest using **SQLCipher** with AES-256-CBC. The database passphrase is:

- Derived from a user-provided master passphrase using PBKDF2-HMAC-SHA256 (600,000 iterations, random salt).
- The salt is stored in a plaintext file (`gitdr.salt`) alongside the database.
- On first run, the user sets the master passphrase. On subsequent runs, they provide it (via environment variable `GITDR_DB_PASSPHRASE` or interactive prompt at startup).

### 4.2 Field-Level Encryption

Sensitive fields (`auth_credential`, destination `config`) are additionally encrypted using **Fernet** (AES-128-CBC with HMAC-SHA256) before being written to the database. This provides defence in depth - even if the SQLCipher layer is compromised, individual credentials remain encrypted.

The Fernet key is derived from the same master passphrase but with a different salt and context string, so the two keys are cryptographically independent.

### 4.3 Key Rotation

A key rotation mechanism should allow re-encrypting all sensitive fields with a new key derived from a new passphrase, without downtime. This is a batch operation: decrypt all fields with the old key, re-encrypt with the new key, update the database in a transaction.

---

## 5. Git Operations

### 5.1 Mirror Clone (Full Backup)

```bash
git clone --mirror <clone_url> <tmp_mirror_dir>
```

This captures all refs: branches, tags, notes, stashes pushed to remote, and HEAD. The resulting bare repo is a complete replica.

### 5.2 Incremental Update

For subsequent backups of the same repo, maintain a **persistent mirror cache** on disk:

```bash
cd <cache_dir>/<source_id>/<repo_name>.git
git remote update --prune
```

This fetches only new/changed refs and prunes deleted ones. Dramatically faster than a fresh clone.

### 5.3 Selective Branch Backup

When `backup_mode` = `selective` and `branch_filter` is set:

1. Clone/update the full mirror (to ensure we have everything).
2. Create a temporary copy of the mirror.
3. Delete refs that don't match the glob patterns in `branch_filter`.
4. Bundle or archive from the pruned copy.

This ensures the source mirror stays complete for future runs.

### 5.4 Archive Creation

**Bundle format (default):**
```bash
git bundle create <output_path> --all
```

Produces a single file containing all refs. Verifiable with `git bundle verify <path>`. Can be cloned directly: `git clone <bundle_path> <dest>`.

**Tar+zstd format (alternative):**
```bash
tar -I zstd -cf <output_path> -C <mirror_parent> <repo_name>.git
```

Preserves hooks, config, and non-standard files that bundles don't include. Larger but more complete for edge cases.

### 5.5 Restore Operations

**From bundle:**
```bash
git clone <bundle_path> <restore_dir>
# Or push to a new remote:
git clone <bundle_path> <tmp_dir>
cd <tmp_dir>
git remote add target <new_remote_url>
git push target --mirror
```

**From tar+zstd:**
```bash
tar -I zstd -xf <archive_path> -C <restore_dir>
# The extracted directory is a bare repo, push as above.
```

### 5.6 Mirror Cache Layout

```
<data_dir>/mirror-cache/
  <source_id>/
    <repo_name>.git/      # bare mirror repo
    <repo_name>.git/      # ...
```

The cache directory is configurable via `GITDR_CACHE_DIR` and defaults to `./data/mirror-cache/`.

---

## 6. Storage Backends

Each backend implements a common interface:

```python
class StorageBackend(Protocol):
    async def upload(self, local_path: Path, remote_key: str) -> None: ...
    async def download(self, remote_key: str, local_path: Path) -> None: ...
    async def delete(self, remote_key: str) -> None: ...
    async def list_keys(self, prefix: str) -> list[str]: ...
    async def exists(self, remote_key: str) -> bool: ...
```

### 6.1 Remote Key Convention

Archives are stored with a consistent key structure:

```
<prefix>/<source_name>/<repo_name>/<timestamp>.<format>
```

Example: `gitdr/github-myorg/api-service/2025-04-06T120000Z.bundle`

### 6.2 Retention Enforcement

After a successful backup, the retention service:

1. Lists all archives for the repo in the destination.
2. Sorts by timestamp descending.
3. Deletes any beyond `retention_count`.

Retention is per-repo-per-job, not global.

---

## 7. Source Validation and Repository Discovery

### 7.1 Source Creation: Connection Test Only

When a user creates or edits a `git_source`, the form validates the credentials **before saving** by calling the forge API with a lightweight probe (e.g. `GET /user` on GitHub, `GET /api/v4/user` on GitLab). If the probe fails the form stays open and shows the error — it does **not** close and drop the user back to the list. Sources are only saved on a confirmed successful connection.

There is no background repo scan triggered at source creation time.

### 7.2 Discovery: Triggered From Job Setup

Repo discovery is an on-demand operation initiated from within the job create/edit form:

1. User picks a source in the job form.
2. An HTMX request fires `POST /api/v1/sources/{id}/discover` (synchronously — the form waits with a spinner).
3. The discovery service calls the forge API, paginates through all repos, upserts the `repositories` cache table, and returns the full repo list.
4. The form renders the repo list as checkboxes with **Select All** / **Select None** / **Invert** bulk controls.
5. User makes their selection. The selected `repo_name` values are stored in `backup_jobs.included_repos` on save.
6. A **Refresh repos** button in the job form re-runs discovery at any time (e.g. if new repos have been created on the forge since the job was last edited).

Discovery is **not** scheduled automatically. It runs on demand only.

### 7.3 Repository Cache Semantics

- New repos are inserted into `repositories` on each discovery run.
- Existing repos have `last_seen_at` updated.
- Repos not seen in a discovery run are not deleted (they may have been temporarily unavailable); they are flagged as stale by `last_seen_at` age.
- The cache is purely internal — users never see or manage the `repositories` table directly in the UI.

### 7.4 Forge API Endpoints

| Forge        | Endpoint                                          | Pagination          |
|--------------|---------------------------------------------------|---------------------|
| GitHub       | `GET /orgs/{org}/repos` or `GET /user/repos`      | Link header         |
| GitLab       | `GET /groups/{id}/projects` (with subgroups)      | `page` param        |
| Azure DevOps | `GET /{org}/{project}/_apis/git/repositories`     | `continuationToken` |
| Bitbucket    | `GET /repositories/{workspace}`                   | `next` URL          |

All discovery calls handle pagination exhaustively and respect rate limits (back off on 429s).

---

## 8. Scheduling

### 8.1 APScheduler Configuration

- **Job store:** SQLAlchemy data store, backed by the same SQLCipher database. Uses `SQLAlchemyDataStore` from APScheduler v4.
- **Scheduler type:** `AsyncScheduler` (v4 replaces `BackgroundScheduler` with async-native schedulers).
- **Executor / worker:** `AsyncIOWorker` - runs jobs on the same event loop as FastAPI.
- **Misfire grace time:** 3600 seconds (if the app was down when a job was due, run it on startup if within an hour).
- **Coalescing:** Enabled. If multiple misfired executions pile up, run only once.
- **Version pin:** APScheduler 4.0.0a6. The v4 API is unstable; do not upgrade without running the full test suite.

### 8.2 Job Lifecycle

1. When a `backup_job` is created/updated with a `schedule_cron`, register it with APScheduler.
2. When a job is disabled, pause it in APScheduler.
3. When a job is deleted, remove it from APScheduler.
4. Manual triggers bypass the scheduler and invoke the backup service directly.

---

## 9. API Endpoints

All endpoints are prefixed with `/api/v1/`.

### 9.1 Sources

| Method | Path                              | Description                                                      |
|--------|-----------------------------------|------------------------------------------------------------------|
| GET    | `/sources`                        | List all sources.                                                |
| POST   | `/sources`                        | Create a source (validates connection before saving).            |
| GET    | `/sources/{id}`                   | Get source details.                                              |
| PUT    | `/sources/{id}`                   | Update a source (re-validates connection).                       |
| DELETE | `/sources/{id}`                   | Delete source and its repo cache.                                |
| POST   | `/sources/{id}/test-connection`   | Probe the forge API to confirm credentials are valid. Returns 200 + forge identity on success, 422 on failure. Used by the create/edit form before saving. |
| POST   | `/sources/{id}/discover`          | Run repo discovery synchronously. Returns the full repo list. Called from the job form's repo picker. |

### 9.2 Repositories (internal cache — limited API)

| Method | Path                              | Description                                                      |
|--------|-----------------------------------|------------------------------------------------------------------|
| GET    | `/sources/{id}/repositories`      | List cached repos for a source (used by job form repo picker).   |

> There is no public `PATCH /repositories/{id}` endpoint. The `excluded` flag is gone. Repo selection is managed via `backup_jobs.included_repos`.

### 9.3 Destinations

| Method | Path                              | Description                    |
|--------|-----------------------------------|--------------------------------|
| GET    | `/destinations`                   | List all destinations.         |
| POST   | `/destinations`                   | Create a destination.          |
| GET    | `/destinations/{id}`              | Get destination details.       |
| PUT    | `/destinations/{id}`              | Update a destination.          |
| DELETE | `/destinations/{id}`              | Delete a destination.          |
| POST   | `/destinations/{id}/test`         | Test connectivity/write access.|

### 9.4 Jobs

| Method | Path                              | Description                    |
|--------|-----------------------------------|--------------------------------|
| GET    | `/jobs`                           | List all jobs.                 |
| POST   | `/jobs`                           | Create a job.                  |
| GET    | `/jobs/{id}`                      | Get job details.               |
| PUT    | `/jobs/{id}`                      | Update a job.                  |
| DELETE | `/jobs/{id}`                      | Delete a job.                  |
| POST   | `/jobs/{id}/run`                  | Trigger manual backup.         |

### 9.5 Runs

| Method | Path                              | Description                    |
|--------|-----------------------------------|--------------------------------|
| GET    | `/runs`                           | List runs (filterable by job, repo, status, date range). |
| GET    | `/runs/{id}`                      | Get run details.               |
| POST   | `/runs/{id}/restore`              | Initiate restore from this run.|

### 9.6 System

| Method | Path                              | Description                    |
|--------|-----------------------------------|--------------------------------|
| GET    | `/system/health`                  | Health check.                  |
| GET    | `/system/stats`                   | Dashboard stats (total repos, last backup, storage used, etc.). |

---

## 10. Web UI

Server-rendered HTML via Jinja2 templates, enhanced with HTMX for interactivity. No JavaScript build step.

### 10.1 Pages

| Route                | Description                                                      |
|----------------------|------------------------------------------------------------------|
| `/`                  | Dashboard: summary stats, recent runs, next scheduled jobs.      |
| `/sources`           | List/create/edit git sources. Create form validates connection before saving; error stays open on failure. No repo list or discovery button here. |
| `/destinations`      | List/create/edit backup destinations. Test connectivity.         |
| `/jobs`              | List/create/edit backup jobs. Enable/disable, manual trigger.    |
| `/jobs/{id}`         | Job detail: which repos are selected, run history, last status, next scheduled run. |
| `/jobs/new`          | Job create form. After picking a source, fires discovery and renders a checkbox repo picker with Select All / None / Invert. Backup type shown as a labelled choice ("Mirror — all refs" / "Selective — branch filter"), not a raw enum. |
| `/runs`              | Filterable table of all backup runs.                             |
| `/runs/{id}`         | Run detail: ref manifest, archive format, backup type, size, checksum, error log, restore button. |
| `/settings`          | Master passphrase rotation, cache management, system info.       |

### 10.2 HTMX Patterns

- **Inline editing:** Source/destination/job forms use `hx-put` for in-place updates.
- **Polling:** Active runs poll for status updates via `hx-trigger="every 5s"`.
- **Toast notifications:** Backup completion/failure triggers a toast via `hx-swap="afterbegin"` on a notification container.
- **Confirmation modals:** Delete actions use `hx-confirm`.

---

## 11. Configuration

All configuration via environment variables, with sensible defaults.

| Variable                | Default                  | Description                                   |
|-------------------------|--------------------------|-----------------------------------------------|
| `GITDR_DB_PASSPHRASE`   | *(required)*             | Master passphrase for DB + field encryption.  |
| `GITDR_DB_PATH`         | `./data/gitdr.db`        | Path to the SQLCipher database file.          |
| `GITDR_CACHE_DIR`       | `./data/mirror-cache`    | Persistent mirror cache directory.            |
| `GITDR_TEMP_DIR`        | `./data/tmp`             | Temp dir for archive creation.                |
| `GITDR_HOST`            | `0.0.0.0`                | Bind address.                                 |
| `GITDR_PORT`            | `8420`                   | Bind port.                                    |
| `GITDR_LOG_LEVEL`       | `INFO`                   | Logging level.                                |
| `GITDR_WORKERS`         | `1`                      | Uvicorn workers. Keep at 1 for SQLite.        |

---

## 12. Docker Deployment

### 12.1 Dockerfile

Based on `python:3.14.3-slim`. Install `git`, `zstd`, and system deps for SQLCipher (`libsqlcipher-dev`). Install the `sqlcipher3` Python package (compiled against the system `libsqlcipher`). Copy app, install Python deps, expose port.

### 12.2 Docker Compose

```yaml
services:
  gitdr:
    build: .
    ports:
      - "8420:8420"
    volumes:
      - gitdr-data:/app/data        # DB, cache, temp
      - /path/to/local/backups:/backups # optional local destination mount
    environment:
      - GITDR_DB_PASSPHRASE=${GITDR_DB_PASSPHRASE}
    restart: unless-stopped

volumes:
  gitdr-data:
```

### 12.3 Considerations

- **Workers:** Keep at 1. SQLite doesn't handle concurrent writes well, and APScheduler's in-process job store assumes a single process.
- **Volume permissions:** Ensure the container user has write access to the data volume.
- **Git SSH keys:** If any sources use SSH auth, mount `~/.ssh` or inject keys via the encrypted config and write them to a temp file at clone time.

---

## 13. Security Considerations

1. **No credentials in logs.** All logging must sanitise URLs (strip tokens from query strings) and never log decrypted credential values.
2. **HTTPS required.** Deploy behind a TLS-terminating reverse proxy (nginx, Caddy, Traefik). Do not expose port 8420 directly to untrusted networks.
3. **No default credentials.** The master passphrase must be set explicitly; the app refuses to start without it.
4. **Temp file cleanup.** All temporary directories (clones, archives in progress) are cleaned up in a `finally` block, even on failure.
5. **Subprocess safety.** All `git` subprocess calls use argument lists (not shell=True) to prevent injection. Clone URLs are validated before use.
6. **Rate limiting.** Forge API calls respect rate limits. The app should back off on 429 responses with exponential retry.
7. **No auth on the web UI (MVP).** For v1, the app is assumed to be on a trusted network or behind an authenticating reverse proxy. Adding built-in auth (even basic auth) is a fast-follow.
8. **Encryption in transit.** All forge API calls must use HTTPS. Git clone URLs using plain HTTP are rejected at validation time. Forge source `base_url` and `clone_url` values are validated to use `https://` or `ssh://` schemes on creation.
9. **TLS for storage backends.** S3, Azure Blob, and GCS SDKs enforce TLS by default. Custom `endpoint_url` values for S3-compatible stores must use `https://`; the app validates this at startup and refuses to create the destination otherwise.
10. **Secrets never in environment output.** The `GITDR_DB_PASSPHRASE` value must not appear in process listings, health check responses, or log output. Mask it when logging the resolved configuration at startup.

---

## 14. Build Order (MVP Roadmap)

### Phase 1 - Core Infrastructure ✅ COMPLETE
1. Project scaffold: directory structure, `pyproject.toml`, Dockerfile, `docker-compose.yml`, `Makefile`.
2. `database/encryption.py` - Fernet key derivation, encrypt/decrypt helpers.
3. `database/connection.py` - SQLCipher engine initialisation.
4. `database/models.py` - All SQLModel models.
5. Database table creation on first run (no migration framework — nuke and recreate for dev).
6. Tests: `tests/unit/test_encryption.py`, `tests/unit/test_models.py`, `tests/conftest.py`.

### Phase 2 - Git Operations ✅ COMPLETE
7. `services/git_ops.py` - `clone_or_update_mirror`, `create_bundle`, `create_tar_archive`, `list_mirror_refs`, `prune_refs`.
8. `services/storage/local.py` - Local filesystem backend.
9. `services/backup.py` - Orchestrator: resolve `included_repos`, clone/update mirror, archive, upload, record run.
10. Tests: `tests/unit/test_git_ops.py`, `tests/unit/test_storage_local.py`, `tests/integration/test_backup_flow.py`.

### Phase 3 - API + UI (Minimum Viable) ✅ COMPLETE
11. FastAPI app skeleton with lifespan, middleware, error handling.
12. CRUD API routes for sources, destinations, jobs.
13. Manual backup trigger endpoint.
14. Jinja2 templates: dashboard, sources list, destinations list, jobs list, runs list.
15. HTMX interactivity: manual trigger buttons, status polling.
16. Tests: `tests/integration/test_api_sources.py`, `tests/integration/test_api_jobs.py`, `tests/integration/test_api_runs.py`.

### Phase 4 - Scheduling + Discovery ✅ COMPLETE

**Implemented:**
- APScheduler v4 `AsyncScheduler` integration (MemoryDataStore; schedules re-registered from DB on startup).
- Forge discovery clients: GitHub, GitLab, Azure DevOps, Bitbucket (all with pagination).
- `services/discovery.py` with `run_discovery`, `upsert_repos`, per-forge clients.
- `services/scheduler.py` with `sync_job_schedules`, `add_job_schedule`, `remove_job_schedule`, `run_job_now`.
- Scheduler lifespan wired into `main.py` (correct `async with aps:` pattern).
- `POST /sources/{id}/discover` — synchronous call returning `list[RepositoryRead]` directly.
- `POST /sources/test-connection` and `POST /sources/{id}/test-connection` — lightweight probe, no DB write.
- `backup_jobs.included_repos` — explicit JSON list of `repo_name` strings (replaced glob-based `repo_filter`).
- `backup_jobs.backup_type` enum (`mirror`, `selective`).
- `services/backup.py` filter logic uses `included_repos` list.
- Source create form: Save button disabled until Test Connection passes; re-disabled on any credential field change.
- Source edit form: Save enabled by default; if user enters a new token, Save disabled until re-test passes.
- Job create/edit UI: repo picker fires `POST /discover` inline; renders checkboxes with All / None / Invert / Refresh bulk controls; pre-selects current `included_repos` on edit.
- Tests: `tests/unit/test_discovery.py` (23 tests). All 250 tests pass.

### Phase 5 - Restore + Polish
28. Restore workflow: download archive, reconstitute repo, optionally push to new remote.
29. S3 storage backend.
30. Azure Blob storage backend.
31. Retention enforcement.
32. Settings page: passphrase rotation, cache stats.
33. Error handling polish, logging, notification hooks.
34. Tests: `tests/unit/test_retention.py`, `tests/integration/test_restore.py`, `tests/unit/test_storage_s3.py` (mocked boto3).

---

## 15. Testing Strategy

### 15.1 Framework and Tools

| Tool              | Version | Purpose                                               |
|-------------------|---------|-------------------------------------------------------|
| `pytest`          | 9.0.2   | Core test runner.                                     |
| `pytest-asyncio`  | 1.3.0   | Async test support (FastAPI + APScheduler).           |
| `httpx`           | 0.28.1  | `AsyncClient` / `TestClient` for FastAPI route tests. |
| `anyio`           | 4.13.0  | Async backend utilities used by httpx and pytest.     |
| `pytest-cov`      | latest  | Coverage reporting.                                   |

### 15.2 Test Structure

```
tests/
  conftest.py                     # Shared fixtures: encrypted temp DB, temp dirs, bare git repo factory
  unit/
    test_encryption.py            # Fernet helpers, PBKDF2 key derivation, key rotation logic
    test_models.py                # SQLModel validators, enum constraints, unique constraints
    test_git_ops.py               # git subprocess wrappers (subprocess mocked with unittest.mock)
    test_storage_local.py         # Local backend: upload, download, delete, list_keys, exists
    test_storage_s3.py            # S3 backend (boto3 mocked)
    test_retention.py             # Retention enforcement: count, sort, delete-beyond-limit
    test_discovery_github.py      # GitHub discovery client (httpx mocked)
    test_url_validation.py        # Clone URL scheme enforcement (https/ssh only)
  integration/
    test_backup_flow.py           # Full backup cycle against a real local git repo
    test_api_sources.py           # Sources CRUD + discover endpoint via TestClient
    test_api_jobs.py              # Jobs CRUD + manual trigger endpoint via TestClient
    test_api_runs.py              # Runs list/detail/restore endpoints via TestClient
    test_scheduler.py             # APScheduler schedule registration and cancellation
    test_restore.py               # Restore workflow end-to-end (bundle + tar+zstd)
    test_key_rotation.py          # Passphrase rotation re-encrypts all fields correctly
```

### 15.3 Conventions

- **Unit tests** cover pure functions and service logic. All `git` subprocess calls and external HTTP calls are mocked; no network access is permitted in unit tests.
- **Integration tests** use a real SQLCipher database in a `pytest` `tmp_path` fixture directory. Git repo fixtures are created via `git init` and populated with commits.
- **No live cloud calls.** S3/Azure/GCS backends are tested with mocked clients only. Local-filesystem tests serve as the integration baseline for storage behaviour.
- **Every public function in `services/` and `database/` must have at least one test.** Coverage target: 80% minimum.
- **Async tests** use `asyncio_mode = "auto"` set in `pyproject.toml` under `[tool.pytest.ini_options]`.
- **Security-sensitive tests:** key derivation, field encryption, and URL validation tests are always run (not skipped) in CI.
- **Test isolation:** each test gets a fresh SQLCipher database. No shared mutable state between tests.

### 15.4 Coverage and CI

Run coverage locally:

```bash
make test
```

This executes:

```bash
GITDR_DB_PASSPHRASE=testpassphrase uv run pytest tests/ -v --cov=gitdr --cov-report=term-missing --cov-fail-under=80
```

Tests also run inside the Docker build (multi-stage) to confirm the `sqlcipher3` + `libsqlcipher-dev` dependency chain is intact in the container image.

---

## 16. Developer Tooling (Makefile)

All common development tasks are available via `make`. Targets are documented via `make help`.

```makefile
.DEFAULT_GOAL := help

.PHONY: help install dev test lint format type-check docker-build docker-up docker-down clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## Install all dependencies including dev extras
	uv sync --all-extras

dev: ## Run the development server with auto-reload
	GITDR_DB_PASSPHRASE=devpassphrase uv run uvicorn gitdr.main:app --reload --port 8420

test: ## Run the full test suite with coverage
	GITDR_DB_PASSPHRASE=testpassphrase uv run pytest tests/ -v --cov=gitdr --cov-report=term-missing --cov-fail-under=80

lint: ## Run ruff linter
	uv run ruff check gitdr/ tests/

format: ## Format code with ruff
	uv run ruff format gitdr/ tests/

type-check: ## Run mypy static type checker
	uv run mypy gitdr/

docker-build: ## Build the Docker image
	docker compose build

docker-up: ## Start the stack in the background
	docker compose up -d

docker-down: ## Stop the stack
	docker compose down

clean: ## Remove build artefacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache dist build *.egg-info .coverage htmlcov/
```

### 16.1 Additional Dev Dependencies (pyproject.toml dev extras)

```toml
[project.optional-dependencies]
dev = [
    "pytest==9.0.2",
    "pytest-asyncio==1.3.0",
    "pytest-cov",
    "httpx==0.28.1",
    "anyio==4.13.0",
    "ruff",
    "mypy",
]
```

---

## 17. Future Enhancements (Post-MVP)

- **Webhook triggers** - Start backups on push events from forges.
- **Non-Git assets** - Backup issues, PRs, wikis, CI configs via forge APIs.
- **Notifications** - Slack/email/webhook on backup failure.
- **Built-in auth** - Username/password or OIDC for the web UI.
- **Backup verification** - Periodic `git bundle verify` or test-clone of stored backups.
- **Encryption of archives** - `age` or GPG encryption of bundles before upload.
- **Prometheus metrics** - Expose `/metrics` for monitoring integration.
- **Multi-worker support** - Migrate from SQLite to PostgreSQL for concurrent access.
- **APScheduler GA** - Upgrade to APScheduler v4 stable release once it reaches GA.
