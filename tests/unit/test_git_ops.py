"""
Unit tests for gitdr.services.git_ops.

All subprocess calls are mocked.  No network access or real git is required.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from gitdr.services import git_ops

# ---------------------------------------------------------------------------
# validate_clone_url
# ---------------------------------------------------------------------------


class TestValidateCloneUrl:
    def test_accepts_https(self):
        git_ops.validate_clone_url("https://github.com/org/repo.git")  # no error

    def test_accepts_ssh(self):
        git_ops.validate_clone_url("ssh://git@github.com/org/repo.git")  # no error

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://github.com/org/repo.git",
            "git://github.com/org/repo.git",
            "ftp://files.example.com/repo.git",
            "/local/path/to/repo.git",
            "file:///tmp/repo.git",
            "git@github.com:org/repo.git",  # SCP-style, no scheme
        ],
    )
    def test_rejects_disallowed_schemes(self, bad_url):
        with pytest.raises(ValueError, match="not allowed"):
            git_ops.validate_clone_url(bad_url)


# ---------------------------------------------------------------------------
# mirror_path
# ---------------------------------------------------------------------------


class TestMirrorPath:
    def test_format_with_string_source_id(self, tmp_path):
        p = git_ops.mirror_path(tmp_path, "abc-123", "my-repo")
        assert p == tmp_path / "abc-123" / "my-repo.git"

    def test_format_with_uuid(self, tmp_path):
        import uuid

        uid = uuid.uuid4()
        p = git_ops.mirror_path(tmp_path, uid, "my-repo")
        assert p == tmp_path / str(uid) / "my-repo.git"


# ---------------------------------------------------------------------------
# clone_mirror
# ---------------------------------------------------------------------------


class TestCloneMirror:
    def test_calls_git_clone_mirror(self, tmp_path):
        cache = tmp_path / "cache"
        temp = tmp_path / "tmp"
        url = "https://github.com/org/repo.git"
        source_id = "src-1"
        repo_name = "repo"

        with (
            patch("gitdr.services.git_ops.subprocess.run") as mock_run,
            patch("gitdr.services.git_ops.shutil.move"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            git_ops.clone_mirror(url, source_id, repo_name, cache, temp)

        # git clone --mirror called with list args
        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert "--mirror" in args
        assert url in args

        # shell=True must NOT be used
        kwargs = mock_run.call_args[1]
        assert kwargs.get("shell") is not True

        # check=True must be set
        assert kwargs.get("check") is True

    def test_returns_mirror_path(self, tmp_path):
        cache = tmp_path / "cache"
        temp = tmp_path / "tmp"
        source_id = "src-1"
        repo_name = "repo"
        expected = cache / source_id / f"{repo_name}.git"

        with (
            patch("gitdr.services.git_ops.subprocess.run"),
            patch("gitdr.services.git_ops.shutil.move"),
        ):
            result = git_ops.clone_mirror(
                "https://example.com/repo.git", source_id, repo_name, cache, temp
            )

        assert result == expected

    def test_creates_parent_dirs(self, tmp_path):
        cache = tmp_path / "deep" / "cache"
        temp = tmp_path / "deep" / "tmp"
        source_id = "src-1"

        with (
            patch("gitdr.services.git_ops.subprocess.run"),
            patch("gitdr.services.git_ops.shutil.move"),
        ):
            git_ops.clone_mirror("https://example.com/r.git", source_id, "r", cache, temp)

        assert (cache / source_id).exists()

    def test_rejects_invalid_url(self, tmp_path):
        with pytest.raises(ValueError, match="not allowed"):
            git_ops.clone_mirror(
                "http://example.com/repo.git",
                "src-1",
                "repo",
                tmp_path / "cache",
                tmp_path / "tmp",
            )

    def test_cleans_up_temp_on_failure(self, tmp_path):
        cache = tmp_path / "cache"
        temp = tmp_path / "tmp"
        temp.mkdir(parents=True)

        with patch(
            "gitdr.services.git_ops.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                git_ops.clone_mirror("https://example.com/r.git", "src-1", "r", cache, temp)

        # No leftover temp subdirs
        remaining = list(temp.iterdir())
        assert remaining == []


# ---------------------------------------------------------------------------
# update_mirror
# ---------------------------------------------------------------------------


class TestUpdateMirror:
    def test_calls_git_remote_update(self, tmp_path):
        cache = tmp_path / "cache"
        source_id = "src-1"
        repo_name = "repo"
        mirror = git_ops.mirror_path(cache, source_id, repo_name)
        mirror.mkdir(parents=True)

        with patch("gitdr.services.git_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            git_ops.update_mirror(source_id, repo_name, cache)

        args = mock_run.call_args[0][0]
        assert "remote" in args
        assert "update" in args
        assert "--prune" in args
        assert mock_run.call_args[1].get("shell") is not True
        assert mock_run.call_args[1].get("check") is True

    def test_raises_if_mirror_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Mirror cache not found"):
            git_ops.update_mirror("src-1", "missing-repo", tmp_path / "cache")

    def test_returns_mirror_path(self, tmp_path):
        cache = tmp_path / "cache"
        source_id = "src-1"
        repo_name = "repo"
        mirror = git_ops.mirror_path(cache, source_id, repo_name)
        mirror.mkdir(parents=True)

        with patch("gitdr.services.git_ops.subprocess.run"):
            result = git_ops.update_mirror(source_id, repo_name, cache)

        assert result == mirror


# ---------------------------------------------------------------------------
# clone_or_update_mirror
# ---------------------------------------------------------------------------


class TestCloneOrUpdateMirror:
    def test_clones_when_mirror_absent(self, tmp_path):
        cache = tmp_path / "cache"
        temp = tmp_path / "tmp"

        with (
            patch("gitdr.services.git_ops.clone_mirror") as mock_clone,
            patch("gitdr.services.git_ops.update_mirror") as mock_update,
        ):
            mock_clone.return_value = tmp_path / "mirror"
            git_ops.clone_or_update_mirror("https://example.com/r.git", "src-1", "r", cache, temp)

        mock_clone.assert_called_once()
        mock_update.assert_not_called()

    def test_updates_when_mirror_exists(self, tmp_path):
        cache = tmp_path / "cache"
        temp = tmp_path / "tmp"
        source_id = "src-1"
        repo_name = "r"
        mirror = git_ops.mirror_path(cache, source_id, repo_name)
        mirror.mkdir(parents=True)

        with (
            patch("gitdr.services.git_ops.clone_mirror") as mock_clone,
            patch("gitdr.services.git_ops.update_mirror") as mock_update,
        ):
            mock_update.return_value = mirror
            git_ops.clone_or_update_mirror(
                "https://example.com/r.git", source_id, repo_name, cache, temp
            )

        mock_update.assert_called_once()
        mock_clone.assert_not_called()


# ---------------------------------------------------------------------------
# list_mirror_refs
# ---------------------------------------------------------------------------


class TestListMirrorRefs:
    def test_parses_for_each_ref_output(self, tmp_path):
        fake_output = (
            "abc1234 refs/heads/main\ndef5678 refs/heads/feature/foo\naaa0000 refs/tags/v1.0\n"
        )

        with patch("gitdr.services.git_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
            refs = git_ops.list_mirror_refs(tmp_path / "repo.git")

        assert refs == {
            "refs/heads/main": "abc1234",
            "refs/heads/feature/foo": "def5678",
            "refs/tags/v1.0": "aaa0000",
        }

    def test_empty_repo_returns_empty_dict(self, tmp_path):
        with patch("gitdr.services.git_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            refs = git_ops.list_mirror_refs(tmp_path / "repo.git")

        assert refs == {}


# ---------------------------------------------------------------------------
# prune_refs
# ---------------------------------------------------------------------------


class TestPruneRefs:
    def _run_side_effect(self, branch_output: str):
        """Return a side_effect function for subprocess.run mocks."""
        calls = []

        def side_effect(args, **kwargs):
            calls.append(args)
            mock = MagicMock(returncode=0)
            # First call is for-each-ref listing
            if "for-each-ref" in args:
                mock.stdout = branch_output
            return mock

        return side_effect, calls

    def test_deletes_non_matching_branches(self, tmp_path):
        branch_output = "main\nfeature/foo\nrelease/1.0\n"
        side_effect, recorded = self._run_side_effect(branch_output)

        with patch("gitdr.services.git_ops.subprocess.run", side_effect=side_effect):
            git_ops.prune_refs(tmp_path / "repo.git", ["main", "release/*"])

        # Only feature/foo should be deleted
        delete_calls = [a for a in recorded if "update-ref" in a]
        assert len(delete_calls) == 1
        assert "refs/heads/feature/foo" in delete_calls[0]

    def test_keeps_all_when_all_match(self, tmp_path):
        branch_output = "main\ndev\n"
        side_effect, recorded = self._run_side_effect(branch_output)

        with patch("gitdr.services.git_ops.subprocess.run", side_effect=side_effect):
            git_ops.prune_refs(tmp_path / "repo.git", ["*"])

        delete_calls = [a for a in recorded if "update-ref" in a]
        assert delete_calls == []

    def test_no_shell_true_in_delete_calls(self, tmp_path):
        branch_output = "old-branch\n"
        side_effect, _ = self._run_side_effect(branch_output)

        with patch("gitdr.services.git_ops.subprocess.run", side_effect=side_effect) as mock_run:
            git_ops.prune_refs(tmp_path / "repo.git", ["main"])

        for c in mock_run.call_args_list:
            assert c[1].get("shell") is not True


# ---------------------------------------------------------------------------
# create_bundle
# ---------------------------------------------------------------------------


class TestCreateBundle:
    def test_calls_git_bundle_create(self, tmp_path):
        mirror = tmp_path / "repo.git"
        mirror.mkdir()
        output = tmp_path / "out" / "repo.bundle"

        with patch("gitdr.services.git_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = git_ops.create_bundle(mirror, output)

        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert "bundle" in args
        assert "create" in args
        assert "--all" in args
        assert str(output) in args
        assert mock_run.call_args[1].get("shell") is not True
        assert mock_run.call_args[1].get("check") is True
        assert result == output

    def test_creates_output_parent_dir(self, tmp_path):
        mirror = tmp_path / "repo.git"
        mirror.mkdir()
        output = tmp_path / "deep" / "nested" / "repo.bundle"

        with patch("gitdr.services.git_ops.subprocess.run"):
            git_ops.create_bundle(mirror, output)

        assert output.parent.exists()


# ---------------------------------------------------------------------------
# create_tar_archive
# ---------------------------------------------------------------------------


class TestCreateTarArchive:
    def _make_popen_mock(self, tar_rc: int = 0, zstd_rc: int = 0):
        """Return a Popen mock factory that simulates tar|zstd pipeline."""
        tar_mock = MagicMock()
        tar_mock.stdout = MagicMock()
        tar_mock.stderr = None  # DEVNULL → None
        tar_mock.returncode = tar_rc
        tar_mock.wait.return_value = tar_rc
        # Context manager protocol: __enter__ must return self
        tar_mock.__enter__ = MagicMock(return_value=tar_mock)
        tar_mock.__exit__ = MagicMock(return_value=False)

        zstd_mock = MagicMock()
        zstd_mock.communicate.return_value = (b"", b"")
        zstd_mock.returncode = zstd_rc
        zstd_mock.__enter__ = MagicMock(return_value=zstd_mock)
        zstd_mock.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def popen_factory(args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tar_mock
            return zstd_mock

        return popen_factory, tar_mock, zstd_mock

    def test_calls_tar_and_zstd(self, tmp_path):
        mirror = tmp_path / "repo.git"
        mirror.mkdir()
        output = tmp_path / "out" / "repo.tar.zst"

        factory, tar_mock, zstd_mock = self._make_popen_mock()
        with patch("gitdr.services.git_ops.subprocess.Popen", side_effect=factory) as mock_popen:
            result = git_ops.create_tar_archive(mirror, output)

        assert mock_popen.call_count == 2
        tar_args = mock_popen.call_args_list[0][0][0]
        zstd_args = mock_popen.call_args_list[1][0][0]

        # tar command
        assert tar_args[0] == "tar"
        assert "-cf" in tar_args
        assert "-C" in tar_args
        assert str(mirror.parent) in tar_args
        assert mirror.name in tar_args

        # zstd command
        assert zstd_args[0] == "zstd"
        assert "--force" in zstd_args
        assert str(output) in zstd_args

        # No shell=True in either call
        for c in mock_popen.call_args_list:
            assert c[1].get("shell") is not True

        assert result == output

    def test_creates_output_parent_dir(self, tmp_path):
        mirror = tmp_path / "repo.git"
        mirror.mkdir()
        output = tmp_path / "archives" / "repo.tar.zst"

        factory, _, _ = self._make_popen_mock()
        with patch("gitdr.services.git_ops.subprocess.Popen", side_effect=factory):
            git_ops.create_tar_archive(mirror, output)

        assert output.parent.exists()

    def test_raises_on_tar_failure(self, tmp_path):
        mirror = tmp_path / "repo.git"
        mirror.mkdir()
        output = tmp_path / "out.tar.zst"

        factory, _, _ = self._make_popen_mock(tar_rc=1, zstd_rc=0)
        with patch("gitdr.services.git_ops.subprocess.Popen", side_effect=factory):
            with pytest.raises(subprocess.CalledProcessError):
                git_ops.create_tar_archive(mirror, output)

    def test_raises_on_zstd_failure(self, tmp_path):
        mirror = tmp_path / "repo.git"
        mirror.mkdir()
        output = tmp_path / "out.tar.zst"

        factory, _, _ = self._make_popen_mock(tar_rc=0, zstd_rc=1)
        with patch("gitdr.services.git_ops.subprocess.Popen", side_effect=factory):
            with pytest.raises(subprocess.CalledProcessError):
                git_ops.create_tar_archive(mirror, output)
