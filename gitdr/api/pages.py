"""HTML page routes — server-rendered Jinja2 templates for the GitDR web UI."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

from gitdr.api.deps import get_session
from gitdr.config import get_settings
from gitdr.database.models import (
    BackupDestination,
    BackupJob,
    BackupRun,
    GitSource,
    Repository,
    RestoreRun,
)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _normalize_uuid(value: object) -> UUID | None:
    """Coerce a UUID or hex string (from _UUIDString columns) to a UUID object."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(hex=str(value))
    except ValueError, AttributeError:
        return None


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Jinja2 template filters
# ---------------------------------------------------------------------------


def _humanize_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _format_duration(value: float | None) -> str:
    if value is None:
        return "—"
    secs = int(value)
    if secs < 60:
        return f"{value:.1f}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _relative_time(value: datetime | None) -> str:
    if value is None:
        return "never"
    now = datetime.now(UTC)
    dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    secs = int((now - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 604800:
        return f"{secs // 86400}d ago"
    return dt.strftime("%d %b %Y")


def _forge_label(forge_type: str) -> str:
    return {
        "github": "GitHub",
        "gitlab": "GitLab",
        "azure_devops": "Azure DevOps",
        "bitbucket": "Bitbucket",
        "generic": "Generic Git",
    }.get(forge_type, forge_type.title())


def _dest_label(dest_type: str) -> str:
    return {
        "local": "Local FS",
        "s3": "Amazon S3",
        "azure_blob": "Azure Blob",
        "gcs": "Google GCS",
        "sftp": "SFTP",
    }.get(dest_type, dest_type.upper())


def _parse_ref_manifest(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return list(data.items())
    except json.JSONDecodeError, AttributeError:
        return []


templates.env.filters["humanize_bytes"] = _humanize_bytes
templates.env.filters["format_duration"] = _format_duration
templates.env.filters["relative_time"] = _relative_time
templates.env.filters["parse_ref_manifest"] = _parse_ref_manifest

templates.env.filters["forge_label"] = _forge_label
templates.env.filters["dest_label"] = _dest_label

router = APIRouter()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    sources = session.exec(select(GitSource)).all()
    repos = session.exec(select(Repository)).all()
    jobs = session.exec(select(BackupJob)).all()
    runs = session.exec(
        select(BackupRun).order_by(col(BackupRun.created_at).desc()).limit(25)
    ).all()

    all_runs = session.exec(select(BackupRun)).all()
    successful = sum(1 for r in all_runs if r.status == "success")
    total_size = sum(r.size_bytes for r in all_runs if r.size_bytes is not None)
    success_rate = round(successful / len(all_runs) * 100) if all_runs else 0

    # Build lookup maps for template
    source_map = {s.id: s for s in sources}
    repo_map = {r.id: r for r in repos}
    job_map = {j.id: j for j in jobs}

    # Most recent run per job (for the Active Jobs widget)
    last_run_map: dict[UUID, BackupRun] = {}
    for r in sorted(all_runs, key=lambda x: x.created_at):
        last_run_map[r.job_id] = r

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current": "dashboard",
            "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "stats": {
                "total_sources": len(sources),
                "total_repos": len(repos),
                "active_jobs": sum(1 for j in jobs if j.enabled),
                "total_jobs": len(jobs),
                "success_rate": success_rate,
                "total_size_bytes": total_size,
                "total_runs": len(all_runs),
                "successful_runs": successful,
            },
            "recent_runs": runs,
            "source_map": source_map,
            "repo_map": repo_map,
            "job_map": job_map,
            "last_run_map": last_run_map,
            "active_jobs": [j for j in jobs if j.enabled],
        },
    )


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    sources = session.exec(select(GitSource).order_by(GitSource.name)).all()
    # Count repos per source
    repos = session.exec(select(Repository)).all()
    repo_counts: dict[UUID, int] = {}
    for r in repos:
        # r.source_id comes back as a hex string from _UUIDString (no process_result_value);
        # source.id is a UUID object — normalise to UUID so the template lookup matches.
        raw = r.source_id
        key = UUID(hex=raw) if isinstance(raw, str) else raw
        repo_counts[key] = repo_counts.get(key, 0) + 1

    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "current": "sources",
            "sources": sources,
            "repo_counts": repo_counts,
        },
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail_page(
    source_id: UUID, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    source = session.get(GitSource, source_id)
    if not source:
        return templates.TemplateResponse(
            request, "404.html", {"current": "sources"}, status_code=404
        )
    repo_count = len(
        session.exec(select(Repository).where(Repository.source_id == source_id)).all()
    )
    return templates.TemplateResponse(
        request,
        "source_detail.html",
        {
            "current": "sources",
            "source": source,
            "repo_count": repo_count,
        },
    )


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


@router.get("/destinations", response_class=HTMLResponse)
def destinations_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    dests = session.exec(select(BackupDestination).order_by(BackupDestination.name)).all()
    return templates.TemplateResponse(
        request,
        "destinations.html",
        {
            "current": "destinations",
            "destinations": dests,
        },
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    jobs = session.exec(select(BackupJob).order_by(BackupJob.name)).all()
    sources = {s.id: s for s in session.exec(select(GitSource)).all()}
    dests = {d.id: d for d in session.exec(select(BackupDestination)).all()}

    # Last run per job — keyed by UUID so template lookup job.id matches
    runs = session.exec(select(BackupRun).order_by(col(BackupRun.created_at).desc())).all()
    last_run: dict[UUID, BackupRun] = {}
    for r in runs:
        if r.job_id not in last_run:
            last_run[r.job_id] = r

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "current": "jobs",
            "jobs": jobs,
            "source_map": sources,
            "dest_map": dests,
            "last_run": last_run,
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail_page(
    job_id: UUID, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    job = session.get(BackupJob, job_id)
    if not job:
        return templates.TemplateResponse(request, "404.html", {"current": "jobs"}, status_code=404)
    source = session.get(GitSource, job.source_id)
    dest = session.get(BackupDestination, job.destination_id)
    runs = session.exec(
        select(BackupRun)
        .where(BackupRun.job_id == job_id)
        .order_by(col(BackupRun.created_at).desc())
        .limit(50)
    ).all()
    repo_map = {r.id: r for r in session.exec(select(Repository)).all()}
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "current": "jobs",
            "job": job,
            "source": source,
            "dest": dest,
            "runs": runs,
            "repo_map": repo_map,
        },
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    status: str | None = None,
    job_id: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    # Coerce empty string (from empty <select>) to None
    status = status or None
    job_id = job_id or None

    # Parse job_id string to UUID for the DB query
    job_id_uuid: UUID | None = None
    if job_id:
        try:
            job_id_uuid = UUID(job_id)
        except ValueError:
            job_id = None

    query = select(BackupRun).order_by(col(BackupRun.created_at).desc()).limit(200)
    if status:
        query = query.where(BackupRun.status == status)
    if job_id_uuid:
        query = query.where(BackupRun.job_id == job_id_uuid)

    backup_runs = session.exec(query).all()

    # Fetch restore runs (not filtered by status/job — always shown unless a
    # status filter is active and it wouldn't match any restore status)
    restore_runs_raw = session.exec(
        select(RestoreRun).order_by(col(RestoreRun.created_at).desc()).limit(200)
    ).all()

    # Build unified row list for the template
    rows: list[dict[str, object]] = []
    for run in backup_runs:
        rows.append({"kind": "backup", "run": run, "ts": run.created_at})

    # Only include restore rows if no status/job filter is active (they don't
    # belong to a job and have their own status vocabulary)
    if not status and not job_id_uuid:
        for rr in restore_runs_raw:
            backup_run_uuid = _normalize_uuid(rr.backup_run_id)
            rows.append(
                {
                    "kind": "restore",
                    "rr": rr,
                    "backup_run_uuid": backup_run_uuid,
                    "ts": rr.created_at,
                }
            )

    def _row_ts(row: dict[str, object]) -> datetime:
        ts = row["ts"]
        if isinstance(ts, datetime):
            return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
        return datetime.min.replace(tzinfo=UTC)

    rows.sort(key=_row_ts, reverse=True)

    # Key maps by UUID so template lookups (run.repo_id / run.job_id) work directly
    repo_map = {r.id: r for r in session.exec(select(Repository)).all()}
    job_map = {j.id: j for j in session.exec(select(BackupJob)).all()}

    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "current": "runs",
            "rows": rows,
            "runs": backup_runs,  # kept for the result count / compat
            "repo_map": repo_map,
            "job_map": job_map,
            "filter_status": status,
            "filter_job_id": job_id,
        },
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(
    run_id: UUID, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    run = session.get(BackupRun, run_id)
    if not run:
        return templates.TemplateResponse(request, "404.html", {"current": "runs"}, status_code=404)
    repo = session.get(Repository, run.repo_id)
    job = session.get(BackupJob, run.job_id)
    restore_runs = list(
        session.exec(
            select(RestoreRun)
            .where(RestoreRun.backup_run_id == run_id)
            .order_by(col(RestoreRun.created_at).desc())
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "current": "runs",
            "run": run,
            "repo": repo,
            "job": job,
            "restore_runs": restore_runs,
        },
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _cache_stats(cache_dir: Path) -> dict:  # type: ignore[type-arg]
    """Return mirror-cache usage stats."""
    if not cache_dir.exists():
        return {"exists": False, "repo_count": 0, "size_bytes": 0}

    repo_dirs = [p for p in cache_dir.rglob("*.git") if p.is_dir()]
    total_bytes = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file())
    return {
        "exists": True,
        "repo_count": len(repo_dirs),
        "size_bytes": total_bytes,
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    sources = session.exec(select(GitSource)).all()
    repos = session.exec(select(Repository)).all()
    dests = session.exec(select(BackupDestination)).all()
    jobs = session.exec(select(BackupJob)).all()
    runs = session.exec(select(BackupRun)).all()
    successful = sum(1 for r in runs if r.status == "success")
    failed = sum(1 for r in runs if r.status == "failed")
    total_size = sum(r.size_bytes for r in runs if r.size_bytes is not None)
    completed_times: list[datetime] = [r.completed_at for r in runs if r.completed_at is not None]
    last_run_at = max(completed_times) if completed_times else None

    settings = get_settings()
    cache_stats = _cache_stats(settings.gitdr_cache_dir)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "current": "settings",
            "stats": {
                "total_sources": len(sources),
                "total_repos": len(repos),
                "total_destinations": len(dests),
                "total_jobs": len(jobs),
                "total_runs": len(runs),
                "successful_runs": successful,
                "failed_runs": failed,
                "total_size_bytes": total_size,
                "last_run_at": last_run_at,
            },
            "cache_stats": cache_stats,
        },
    )


@router.post("/settings/clear-cache", response_class=RedirectResponse)
def clear_cache(request: Request) -> RedirectResponse:
    """Delete the mirror cache directory (will be re-populated on next backup run)."""
    settings = get_settings()
    if settings.gitdr_cache_dir.exists():
        shutil.rmtree(settings.gitdr_cache_dir, ignore_errors=True)
        settings.gitdr_cache_dir.mkdir(parents=True, exist_ok=True)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/purge-runs", response_class=RedirectResponse)
def purge_runs(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    """Delete all BackupRun records (archives on the storage backend are not touched)."""
    runs = session.exec(select(BackupRun)).all()
    for run in runs:
        session.delete(run)
    session.commit()
    return RedirectResponse(url="/settings", status_code=303)


# ---------------------------------------------------------------------------
# Partials (HTMX fragments)
# ---------------------------------------------------------------------------


@router.get("/partials/source-repos/{source_id}", response_class=HTMLResponse)
def source_repos_partial(
    source_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Return a checkbox list of cached repos for a source (used by job form)."""
    repos = list(
        session.exec(
            select(Repository)
            .where(Repository.source_id == source_id)
            .order_by(Repository.repo_name)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "_repo_picker.html",
        {"repos": repos},
    )


@router.get("/partials/restore-history/{run_id}", response_class=HTMLResponse)
def restore_history_partial(
    run_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial — restore attempt history for a backup run.

    Returns the restore history card fragment.  Active ('pending'/'running')
    restore runs trigger continued polling; once all are terminal the polling
    stops.
    """
    run = session.get(BackupRun, run_id)
    if not run:
        return HTMLResponse("", status_code=404)
    restore_runs = list(
        session.exec(
            select(RestoreRun)
            .where(RestoreRun.backup_run_id == run_id)
            .order_by(col(RestoreRun.created_at).desc())
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "_restore_history.html",
        {"run": run, "restore_runs": restore_runs},
    )
