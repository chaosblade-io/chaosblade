"""Tests for the unified file reading tool."""

import pytest

from chaos_agent.tools.file_reader import safe_read_file, _is_denylisted, MAX_FILE_BYTES


class TestSafeReadFile:
    """Tests for safe_read_file."""

    def test_read_existing_file(self, tmp_path):
        """Can read a file under a regular directory."""
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        result = safe_read_file(str(f))
        assert result == "hello world"

    def test_read_directory_returns_listing(self, tmp_path):
        """Reading a directory returns its contents listing."""
        (tmp_path / "file1.txt").write_text("a", encoding="utf-8")
        (tmp_path / "subdir").mkdir()

        result = safe_read_file(str(tmp_path))
        assert "file1.txt" in result
        assert "subdir/" in result
        assert "Directory:" in result

    def test_read_empty_directory(self, tmp_path):
        """Reading an empty directory returns empty listing."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        result = safe_read_file(str(empty_dir))
        assert "(empty)" in result

    def test_file_not_found(self, tmp_path):
        """Raises FileNotFoundError for missing paths."""
        with pytest.raises(FileNotFoundError, match="not found"):
            safe_read_file(str(tmp_path / "nonexistent.txt"))

    def test_denylisted_ssh_key(self, tmp_path):
        """Raises PermissionError for SSH key files."""
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key_file = ssh_dir / "id_rsa"
        key_file.write_text("private key", encoding="utf-8")

        with pytest.raises(PermissionError, match="restricted"):
            safe_read_file(str(key_file))

    def test_denylisted_pem_file(self, tmp_path):
        """Raises PermissionError for .pem files."""
        pem_file = tmp_path / "cert.pem"
        pem_file.write_text("certificate", encoding="utf-8")

        with pytest.raises(PermissionError, match="private keys"):
            safe_read_file(str(pem_file))

    def test_denylisted_key_file(self, tmp_path):
        """Raises PermissionError for .key files."""
        key_file = tmp_path / "server.key"
        key_file.write_text("key", encoding="utf-8")

        with pytest.raises(PermissionError, match="private keys"):
            safe_read_file(str(key_file))

    def test_relative_path_resolved(self, tmp_path, monkeypatch):
        """Relative paths are resolved against cwd."""
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "test.txt"
        f.write_text("relative content", encoding="utf-8")

        result = safe_read_file("test.txt")
        assert result == "relative content"

    def test_expanduser(self, tmp_path, monkeypatch):
        """~ is expanded properly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "test.txt"
        f.write_text("home content", encoding="utf-8")

        result = safe_read_file("~/test.txt")
        assert result == "home content"


class TestFileSizeCap:
    """Tests for MAX_FILE_BYTES truncation."""

    def test_small_file_not_truncated(self, tmp_path):
        """Files under MAX_FILE_BYTES are returned in full."""
        content = "x" * 100
        f = tmp_path / "small.txt"
        f.write_text(content, encoding="utf-8")

        result = safe_read_file(str(f))
        assert result == content
        assert "[truncated" not in result

    def test_large_file_truncated(self, tmp_path):
        """Files exceeding MAX_FILE_BYTES are truncated with a notice."""
        # Write a file slightly larger than the cap
        over = MAX_FILE_BYTES + 1024
        content = "A" * over
        f = tmp_path / "big.txt"
        f.write_bytes(content.encode("utf-8"))

        result = safe_read_file(str(f))
        assert "[truncated:" in result
        assert f"showing first {MAX_FILE_BYTES} of {over} bytes" in result
        # The actual content portion should be exactly MAX_FILE_BYTES chars
        content_portion = result.split("\n\n[truncated:")[0]
        assert len(content_portion) == MAX_FILE_BYTES

    def test_exact_boundary_not_truncated(self, tmp_path):
        """A file exactly at MAX_FILE_BYTES is NOT truncated."""
        content = "B" * MAX_FILE_BYTES
        f = tmp_path / "exact.txt"
        f.write_bytes(content.encode("utf-8"))

        result = safe_read_file(str(f))
        assert result == content
        assert "[truncated" not in result


class TestEncodingSafety:
    """Tests for non-UTF-8 / binary file handling."""

    def test_non_utf8_file_does_not_raise(self, tmp_path):
        """Binary / non-UTF-8 files return replacement chars instead of raising."""
        # 0xFF is invalid as the first byte of a UTF-8 sequence
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\xff\xfe\x00\x01binary\xff")

        result = safe_read_file(str(f))
        assert "binary" in result  # ASCII portion survived
        assert "\ufffd" in result  # U+FFFD replacement char present

    def test_mixed_encoding_file(self, tmp_path):
        """Valid UTF-8 prefix + invalid bytes produces no exception."""
        f = tmp_path / "mixed.txt"
        f.write_bytes("hello".encode("utf-8") + b"\xff\xff" + "world".encode("utf-8"))

        result = safe_read_file(str(f))
        assert "hello" in result
        assert "world" in result
        assert "\ufffd" in result


class TestIsDenylisted:
    """Tests for _is_denylisted."""

    def test_regular_file_allowed(self, tmp_path):
        denied, _ = _is_denylisted(tmp_path / "config.yaml")
        assert denied is False

    def test_etc_shadow_denied(self):
        from pathlib import Path
        denied, reason = _is_denylisted(Path("/etc/shadow"))
        assert denied is True
        assert "restricted" in reason

    def test_ssh_dir_denied(self):
        from pathlib import Path
        denied, reason = _is_denylisted(Path("/home/user/.ssh/id_rsa"))
        assert denied is True

    def test_kubeconfig_denied(self):
        from pathlib import Path
        denied, reason = _is_denylisted(Path("/home/user/.kube/config"))
        assert denied is True

    def test_pem_suffix_denied(self):
        from pathlib import Path
        denied, reason = _is_denylisted(Path("/tmp/cert.pem"))
        assert denied is True

    def test_regular_yaml_allowed(self):
        from pathlib import Path
        denied, _ = _is_denylisted(Path("/tmp/config.yaml"))
        assert denied is False

class TestNonRegularFilesRejected:
    """Non-regular files must be rejected *before* opening.

    Regression guard: the size cap was originally gated on
    ``st_size > MAX_FILE_BYTES``, but character devices and FIFOs report
    ``st_size == 0``, so they fell through to an unbounded read. For a FIFO
    even ``open()`` blocks (before any read), so a read-size cap alone
    cannot fix it. Neither hang raises, so ``factory.read_file``'s
    try/except cannot contain it — the guard must be a pre-open check.
    """

    def test_fifo_rejected_without_blocking(self, tmp_path):
        import os

        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        # Must raise immediately; a regression would block here forever
        # (pytest-timeout turns that into a failure rather than a hang).
        with pytest.raises(ValueError, match="Not a regular file"):
            safe_read_file(str(fifo))

    @pytest.mark.parametrize("device", ["/dev/zero", "/dev/urandom"])
    def test_character_device_rejected(self, device):
        from pathlib import Path

        if not Path(device).exists():
            pytest.skip(f"{device} not available on this platform")
        # A regression would read without bound and exhaust memory.
        with pytest.raises(ValueError, match="Not a regular file"):
            safe_read_file(device)

    def test_directory_still_lists(self, tmp_path):
        """The guard must not break the directory-listing branch."""
        (tmp_path / "a.txt").write_text("x")
        out = safe_read_file(str(tmp_path))
        assert "Directory:" in out
        assert "a.txt" in out
