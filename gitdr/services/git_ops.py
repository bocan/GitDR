"""
Git operations service for GitDR.

All subprocess calls use argument lists (never shell=True) to prevent
injection.  Clone URLs are validated before any subprocess is invoked.
Temporary directories are always cleaned up in finally blocks.
"""

import fnmatch
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

logger = logging.getLogger(__name__)

# Schemes accepted for clone URLs.  plain http:// and git:// are rejected.
_ALLOWED_SCHEMES = {"https", "ssh"}


def _sanitize_url(url: str) -> str:
    """Strip credentials from a URL before logging."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        sanitized = parsed._replace(netloc=host)
        return sanitized.geturl()
    return url


def _append_output(
    log_lines: list[str] | None,
    cmd: list[str],
    stdout: bytes | str | None,
    stderr: bytes | str | None,
) -> None:
    """Append a formatted command + output block to *log_lines* if provided."""
    if log_lines is None:
        return
    # Sanitize any clone URL that crept into the command (first bare https/ssh arg)
    display_cmd = " ".join(
        _sanitize_url(a) if a.startswith(("https://", "ssh://")) else a for a in cmd
    )
    log_lines.append(f"$ {display_cmd}")
    for stream, _label in ((stdout, "stdout"), (stderr, "stderr")):
        if stream:
            text = stream.decode("utf-8", errors="replace") if isinstance(stream, bytes) else stream
            for line in text.rstrip("\n").splitlines():
                log_lines.append(f"  {line}")


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def validate_clone_url(url: str) -> None:
    """
    Raise ``ValueError`` if *url* is not safe to pass to git.

    Only ``https://`` and ``ssh://`` schemes are accepted.  Plain ``http://``
    and ``git://`` are rejected to enforce encrypted-in-transit policy.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Clone URL scheme {parsed.scheme!r} is not allowed. "
            f"Use https:// or ssh:// (got: {url!r})"
        )


# ---------------------------------------------------------------------------
# Mirror cache helpers
# ---------------------------------------------------------------------------


def mirror_path(cache_dir: Path, source_id: str | UUID, repo_name: str) -> Path:
    """Return the canonical path for the bare mirror repo inside the cache."""
    return cache_dir / str(source_id) / f"{repo_name}.git"


# ---------------------------------------------------------------------------
# Core git operations
# ---------------------------------------------------------------------------


def clone_mirror(
    clone_url: str,
    source_id: str | UUID,
    repo_name: str,
    cache_dir: Path,
    temp_dir: Path,
    log_lines: list[str] | None = None,
) -> Path:
    """
    Clone *clone_url* as a bare mirror into the cache.

    Clones atomically: the bare repo is written to a temp directory first and
    then moved into place.  This prevents a partial mirror from being left on
    disk if the clone fails.

    Returns the path to the bare repo inside the cache.
    Raises ``subprocess.CalledProcessError`` if the git command fails.
    """
    validate_clone_url(clone_url)

    dest = mirror_path(cache_dir, source_id, repo_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    tmp_root = Path(tempfile.mkdtemp(dir=temp_dir))
    try:
        tmp_mirror = tmp_root / f"{repo_name}.git"
        logger.info("Cloning mirror: %s -> %s", _sanitize_url(clone_url), dest)
        cmd = ["git", "clone", "--mirror", clone_url, str(tmp_mirror)]  # noqa: S607
        result = subprocess.run(  # noqa: S603
            cmd,
            check=True,
            capture_output=True,
        )
        _append_output(log_lines, cmd, result.stdout, result.stderr)
        shutil.move(str(tmp_mirror), str(dest))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return dest


def update_mirror(
    source_id: str | UUID,
    repo_name: str,
    cache_dir: Path,
    log_lines: list[str] | None = None,
) -> Path:
    """
    Update an existing mirror with ``git remote update --prune``.

    Returns the path to the bare repo.
    Raises ``FileNotFoundError`` if the mirror does not exist.
    Raises ``subprocess.CalledProcessError`` if the git command fails.
    """
    dest = mirror_path(cache_dir, source_id, repo_name)
    if not dest.exists():
        raise FileNotFoundError(f"Mirror cache not found at {dest}. Run clone_mirror first.")
    logger.info("Updating mirror: %s", dest)
    cmd = ["git", "-C", str(dest), "remote", "update", "--prune"]  # noqa: S607
    result = subprocess.run(  # noqa: S603
        cmd,
        check=True,
        capture_output=True,
    )
    _append_output(log_lines, cmd, result.stdout, result.stderr)
    return dest


def clone_or_update_mirror(
    clone_url: str,
    source_id: str | UUID,
    repo_name: str,
    cache_dir: Path,
    temp_dir: Path,
    log_lines: list[str] | None = None,
) -> Path:
    """
    Clone the mirror if it does not exist; otherwise fetch updates.

    This is the primary entry point for the backup orchestrator.
    """
    dest = mirror_path(cache_dir, source_id, repo_name)
    if dest.exists():
        return update_mirror(source_id, repo_name, cache_dir, log_lines)
    return clone_mirror(clone_url, source_id, repo_name, cache_dir, temp_dir, log_lines)


# ---------------------------------------------------------------------------
# Ref inspection
# ---------------------------------------------------------------------------


def list_mirror_refs(mirror: Path) -> dict[str, str]:
    """
    Return a ``{refname: sha}`` dict for every ref in *mirror*.

    Uses ``git for-each-ref`` which works on bare repos.
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(mirror),
            "for-each-ref",
            "--format=%(objectname) %(refname)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            sha, refname = line.split(" ", 1)
            refs[refname] = sha
    return refs


# ---------------------------------------------------------------------------
# Selective branch filtering
# ---------------------------------------------------------------------------


def prune_refs(mirror: Path, keep_patterns: list[str], log_lines: list[str] | None = None) -> None:
    """
    Delete branch refs in *mirror* that do not match any of *keep_patterns*.

    *keep_patterns* are fnmatch-style globs applied to the short branch name
    (e.g. ``"main"``, ``"release/*"``).

    Only ``refs/heads/`` refs are pruned; tags and other refs are left intact.
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(mirror),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    branches = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    for branch in branches:
        if not any(fnmatch.fnmatch(branch, pat) for pat in keep_patterns):
            logger.debug("Pruning ref refs/heads/%s", branch)
            cmd = ["git", "-C", str(mirror), "update-ref", "-d", f"refs/heads/{branch}"]  # noqa: S607
            result2 = subprocess.run(  # noqa: S603
                cmd,
                check=True,
                capture_output=True,
            )
            _append_output(log_lines, cmd, result2.stdout, result2.stderr)


# ---------------------------------------------------------------------------
# Archive creation
# ---------------------------------------------------------------------------


def create_bundle(mirror: Path, output_path: Path, log_lines: list[str] | None = None) -> Path:
    """
    Create a git bundle containing all refs from *mirror*.

    The bundle is written directly to *output_path*.  Git bundles are
    verifiable with ``git bundle verify`` and can be cloned directly.

    Returns *output_path*.
    Raises ``subprocess.CalledProcessError`` if the git command fails.
    """
    logger.info("Creating bundle: %s -> %s", mirror, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "-C", str(mirror), "bundle", "create", str(output_path), "--all"]  # noqa: S607
    result = subprocess.run(  # noqa: S603
        cmd,
        check=True,
        capture_output=True,
    )
    _append_output(log_lines, cmd, result.stdout, result.stderr)
    return output_path


def create_tar_archive(mirror: Path, output_path: Path, log_lines: list[str] | None = None) -> Path:
    """
    Create a tar+zstd archive of the bare mirror repo.

    Preserves hooks, config, and any non-standard files that git bundles
    exclude.  The archive root contains the ``<repo_name>.git`` directory.

    Uses a Unix pipeline (tar stdout → zstd stdin) without shell=True so it
    works with both GNU tar (Linux) and bsdtar (macOS).  Both sub-processes
    are managed via context managers so all pipe file-handles are closed
    deterministically, preventing ``ResourceWarning`` under strict pytest
    warning filters.

    Returns *output_path*.
    Raises ``subprocess.CalledProcessError`` if either command fails.
    """
    logger.info("Creating tar archive: %s -> %s", mirror, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tar_cmd = ["tar", "-cf", "-", "-C", str(mirror.parent), mirror.name]
    zstd_cmd = ["zstd", "--force", "-", "-o", str(output_path)]

    if log_lines is not None:
        log_lines.append(f"$ {' '.join(tar_cmd)} | {' '.join(zstd_cmd)}")

    # Use context managers so all PIPE handles are closed deterministically.
    # tar stderr is discarded (DEVNULL) since we only need the return code.
    with subprocess.Popen(  # noqa: S603
        tar_cmd,  # noqa: S607
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as tar_proc:
        with subprocess.Popen(  # noqa: S603
            zstd_cmd,  # noqa: S607
            stdin=tar_proc.stdout,
            stderr=subprocess.PIPE,
        ) as zstd_proc:
            # Close parent's copy so tar receives SIGPIPE if zstd exits early.
            tar_proc.stdout.close()  # type: ignore[union-attr]
            _, zstd_stderr = zstd_proc.communicate()
            zstd_return = zstd_proc.returncode

        tar_return = tar_proc.wait()

    if tar_return != 0:
        raise subprocess.CalledProcessError(tar_return, tar_cmd)
    if zstd_return != 0:
        raise subprocess.CalledProcessError(zstd_return, zstd_cmd, stderr=zstd_stderr)

    if log_lines is not None and zstd_stderr:
        text = zstd_stderr.decode("utf-8", errors="replace").rstrip("\n")
        for line in text.splitlines():
            log_lines.append(f"  {line}")

    return output_path


# ---------------------------------------------------------------------------
# Restore operations
# ---------------------------------------------------------------------------


def restore_bundle(bundle_path: Path, restore_dir: Path) -> Path:
    """
    Clone a git bundle into *restore_dir*.

    The restored repository is a regular (non-bare) clone.  *restore_dir*
    must not already exist.

    Returns *restore_dir*.
    Raises ``subprocess.CalledProcessError`` if git fails.
    """
    logger.info("Restoring bundle %s -> %s", bundle_path, restore_dir)
    restore_dir.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603
        ["git", "clone", str(bundle_path), str(restore_dir)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    logger.debug("git clone (bundle) stdout: %s", result.stdout.decode())
    return restore_dir


def restore_tar_archive(archive_path: Path, restore_dir: Path) -> Path:
    """
    Extract a tar+zstd archive into *restore_dir*.

    The extracted directory is a bare git repository (``<repo_name>.git``
    directory).  *restore_dir* is created if it does not exist.

    Uses a ``zstd | tar`` pipeline (matching the creation approach) so it
    works on both Linux (GNU tar) and macOS (bsdtar).

    Returns *restore_dir*.
    Raises ``subprocess.CalledProcessError`` if extraction fails.
    """
    logger.info("Extracting tar archive %s -> %s", archive_path, restore_dir)
    restore_dir.mkdir(parents=True, exist_ok=True)

    zstd_cmd = ["zstd", "-d", "--stdout", str(archive_path)]
    tar_cmd = ["tar", "-xf", "-", "-C", str(restore_dir)]

    with subprocess.Popen(  # noqa: S603
        zstd_cmd,  # noqa: S607
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as zstd_proc:
        with subprocess.Popen(  # noqa: S603
            tar_cmd,  # noqa: S607
            stdin=zstd_proc.stdout,
            stderr=subprocess.PIPE,
        ) as tar_proc:
            zstd_proc.stdout.close()  # type: ignore[union-attr]
            _, tar_stderr = tar_proc.communicate()
            tar_return = tar_proc.returncode

        _, zstd_stderr = zstd_proc.communicate()
        zstd_return = zstd_proc.wait()

    if zstd_return != 0:
        raise subprocess.CalledProcessError(zstd_return, zstd_cmd, stderr=zstd_stderr)
    if tar_return != 0:
        raise subprocess.CalledProcessError(tar_return, tar_cmd, stderr=tar_stderr)

    return restore_dir


def push_to_remote(repo_dir: Path, remote_url: str) -> None:
    """
    Push all refs from the restored repo to *remote_url*.

    Validates that *remote_url* uses ``https://`` or ``ssh://`` before
    invoking git.  Uses ``--mirror`` to push all branches and tags.

    Raises ``ValueError`` if the URL scheme is not allowed.
    Raises ``subprocess.CalledProcessError`` if git push fails.
    """
    validate_clone_url(remote_url)
    logger.info("Pushing mirror to %s", _sanitize_url(remote_url))
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_dir), "push", "--mirror", remote_url],  # noqa: S607
        check=True,
        capture_output=True,
    )
