"""Unit tests for the S3 storage backend (boto3 fully mocked)."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gitdr.services.storage.s3 import S3StorageBackend


@pytest.fixture()
def mock_boto3_client() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def backend(mock_boto3_client: MagicMock, tmp_path: Path) -> S3StorageBackend:
    with patch("boto3.client", return_value=mock_boto3_client):
        b = S3StorageBackend(
            {
                "bucket": "test-bucket",
                "prefix": "gitdr/",
                "region": "us-east-1",
            }
        )
    return b


@pytest.fixture()
def backend_no_prefix(mock_boto3_client: MagicMock) -> S3StorageBackend:
    with patch("boto3.client", return_value=mock_boto3_client):
        b = S3StorageBackend({"bucket": "test-bucket"})
    return b


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------


def test_construct_with_credentials() -> None:
    client_mock = MagicMock()
    with patch("boto3.client", return_value=client_mock) as mock_ctor:
        S3StorageBackend(
            {
                "bucket": "my-bucket",
                "region": "eu-west-1",
                "access_key_id": "AKID",
                "secret_access_key": "secret",
                "endpoint_url": "https://s3.example.com",
            }
        )
    mock_ctor.assert_called_once_with(
        "s3",
        region_name="eu-west-1",
        endpoint_url="https://s3.example.com",
        aws_access_key_id="AKID",
        aws_secret_access_key="secret",
    )


def test_http_endpoint_rejected() -> None:
    with pytest.raises(ValueError, match="https://"):
        S3StorageBackend({"bucket": "b", "endpoint_url": "http://s3.example.com"})


def test_missing_boto3_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="boto3 is not installed"):
        S3StorageBackend({"bucket": "b"})


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def test_full_key_with_prefix(backend: S3StorageBackend) -> None:
    assert backend._full_key("mysrc/repo/ts.bundle") == "gitdr/mysrc/repo/ts.bundle"


def test_full_key_no_prefix(backend_no_prefix: S3StorageBackend) -> None:
    assert backend_no_prefix._full_key("mysrc/repo/ts.bundle") == "mysrc/repo/ts.bundle"


def test_strip_prefix(backend: S3StorageBackend) -> None:
    assert backend._strip_prefix("gitdr/mysrc/repo/ts.bundle") == "mysrc/repo/ts.bundle"


def test_strip_prefix_no_prefix(backend_no_prefix: S3StorageBackend) -> None:
    assert backend_no_prefix._strip_prefix("mysrc/repo/ts.bundle") == "mysrc/repo/ts.bundle"


# ---------------------------------------------------------------------------
# Sync operations (called directly to avoid asyncio overhead in unit tests)
# ---------------------------------------------------------------------------


def test_upload_sync(
    backend: S3StorageBackend, mock_boto3_client: MagicMock, tmp_path: Path
) -> None:
    f = tmp_path / "archive.bundle"
    f.write_bytes(b"data")
    backend._client = mock_boto3_client
    backend._upload_sync(f, "mysrc/repo/ts.bundle")
    mock_boto3_client.upload_file.assert_called_once_with(
        str(f), "test-bucket", "gitdr/mysrc/repo/ts.bundle"
    )


def test_download_sync(
    backend: S3StorageBackend, mock_boto3_client: MagicMock, tmp_path: Path
) -> None:
    backend._client = mock_boto3_client
    dest = tmp_path / "out.bundle"
    backend._download_sync("mysrc/repo/ts.bundle", dest)
    mock_boto3_client.download_file.assert_called_once_with(
        "test-bucket", "gitdr/mysrc/repo/ts.bundle", str(dest)
    )


def test_delete_sync(backend: S3StorageBackend, mock_boto3_client: MagicMock) -> None:
    backend._client = mock_boto3_client
    backend._delete_sync("mysrc/repo/ts.bundle")
    mock_boto3_client.delete_object.assert_called_once_with(
        Bucket="test-bucket", Key="gitdr/mysrc/repo/ts.bundle"
    )


def test_list_sync(backend: S3StorageBackend, mock_boto3_client: MagicMock) -> None:
    paginator_mock = MagicMock()
    paginator_mock.paginate.return_value = [
        {
            "Contents": [
                {"Key": "gitdr/mysrc/repo/20250101T000000_000000Z.bundle"},
                {"Key": "gitdr/mysrc/repo/20250102T000000_000000Z.bundle"},
            ]
        }
    ]
    mock_boto3_client.get_paginator.return_value = paginator_mock
    backend._client = mock_boto3_client

    keys = backend._list_sync("mysrc/repo")
    assert keys == [
        "mysrc/repo/20250101T000000_000000Z.bundle",
        "mysrc/repo/20250102T000000_000000Z.bundle",
    ]
    paginator_mock.paginate.assert_called_once_with(Bucket="test-bucket", Prefix="gitdr/mysrc/repo")


def test_list_sync_empty(backend: S3StorageBackend, mock_boto3_client: MagicMock) -> None:
    paginator_mock = MagicMock()
    paginator_mock.paginate.return_value = [{}]  # no Contents key
    mock_boto3_client.get_paginator.return_value = paginator_mock
    backend._client = mock_boto3_client

    keys = backend._list_sync("mysrc/repo")
    assert keys == []


def test_exists_true(backend: S3StorageBackend, mock_boto3_client: MagicMock) -> None:
    mock_boto3_client.exceptions.ClientError = Exception  # not raised
    backend._client = mock_boto3_client
    result = backend._exists_sync("mysrc/repo/ts.bundle")
    assert result is True
    mock_boto3_client.head_object.assert_called_once_with(
        Bucket="test-bucket", Key="gitdr/mysrc/repo/ts.bundle"
    )


def test_exists_false_on_404(backend: S3StorageBackend, mock_boto3_client: MagicMock) -> None:
    exc = Exception("Not Found")
    exc.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
    mock_boto3_client.head_object.side_effect = exc
    mock_boto3_client.exceptions.ClientError = type(exc)
    backend._client = mock_boto3_client

    result = backend._exists_sync("mysrc/repo/ts.bundle")
    assert result is False


# ---------------------------------------------------------------------------
# Async wrappers (smoke tests — confirm they delegate to sync helpers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_async(backend: S3StorageBackend, tmp_path: Path) -> None:
    f = tmp_path / "archive.bundle"
    f.write_bytes(b"x")
    backend._upload_sync = MagicMock()  # type: ignore[method-assign]
    await backend.upload(f, "mysrc/repo/ts.bundle")
    backend._upload_sync.assert_called_once_with(f, "mysrc/repo/ts.bundle")


@pytest.mark.asyncio
async def test_download_async(backend: S3StorageBackend, tmp_path: Path) -> None:
    dest = tmp_path / "out.bundle"
    backend._download_sync = MagicMock()  # type: ignore[method-assign]
    await backend.download("mysrc/repo/ts.bundle", dest)
    backend._download_sync.assert_called_once_with("mysrc/repo/ts.bundle", dest)


@pytest.mark.asyncio
async def test_delete_async(backend: S3StorageBackend) -> None:
    backend._delete_sync = MagicMock()  # type: ignore[method-assign]
    await backend.delete("mysrc/repo/ts.bundle")
    backend._delete_sync.assert_called_once_with("mysrc/repo/ts.bundle")


@pytest.mark.asyncio
async def test_list_keys_async(backend: S3StorageBackend) -> None:
    backend._list_sync = MagicMock(return_value=["a", "b"])  # type: ignore[method-assign]
    result = await backend.list_keys("pfx")
    assert result == ["a", "b"]


@pytest.mark.asyncio
async def test_exists_async(backend: S3StorageBackend) -> None:
    backend._exists_sync = MagicMock(return_value=True)  # type: ignore[method-assign]
    assert await backend.exists("mysrc/repo/ts.bundle") is True
