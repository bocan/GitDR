"""
Retention enforcement for GitDR.

After each successful backup, ``enforce_retention`` trims older archives for
the same repo+job combination so that at most ``retention_count`` copies are
kept on the storage backend.

The archive key convention is:

    gitdr/<source_name>/<repo_name>/<timestamp>.<ext>

Keys are sorted lexicographically; because the timestamp format is
``%Y%m%dT%H%M%S_%fZ`` (fully left-padded), lexicographic order matches
chronological order.  Older keys sort first, so we delete the *head* of the
sorted list once we trim beyond the limit.
"""

import logging

from gitdr.services.storage.base import StorageBackend

logger = logging.getLogger(__name__)


async def enforce_retention(
    storage: StorageBackend,
    source_name: str,
    repo_name: str,
    retention_count: int,
) -> int:
    """
    Delete old archives for *repo_name* beyond *retention_count*.

    Parameters
    ----------
    storage:
        The storage backend to query and delete from.
    source_name:
        The ``GitSource.name`` for this job (used to build the key prefix).
    repo_name:
        The repository name (used to build the key prefix).
    retention_count:
        Maximum number of archives to keep.  ``0`` means keep all — the
        function returns immediately without making any API calls.

    Returns
    -------
    int
        The number of archives deleted.
    """
    if retention_count <= 0:
        return 0

    prefix = f"gitdr/{source_name}/{repo_name}"
    keys = await storage.list_keys(prefix)

    # Keys are comparable lexicographically because the timestamp portion is
    # zero-padded. Sort ascending so oldest come first.
    keys = sorted(keys)

    excess = len(keys) - retention_count
    if excess <= 0:
        return 0

    to_delete = keys[:excess]
    deleted = 0
    for key in to_delete:
        try:
            await storage.delete(key)
            logger.info("Retention: deleted old archive %s", key)
            deleted += 1
        except Exception:
            logger.exception("Retention: failed to delete %s", key)

    return deleted
