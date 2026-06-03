"""Tests for the file writing tool."""

import pytest

from chaos_agent.tools.file_writer import safe_write_file, _is_write_denied


class TestSafeWriteFile:
    """Tests for safe_write_file."""

    def test_write_new_file(self, tmp_path):
        """Can write a new file under an allowed directory."""
        f = tmp_path / "output.txt"
        result = safe_write_file(str(f), "hello world")
        assert "Successfully wrote" in result
        assert f.read_text(encoding="utf-8") == "hello world"

    def test_overwrite_existing_file(self, tmp_path):
        """Can overwrite an existing file."""
        f = tmp_path / "output.txt"
        f.write_text("old content", encoding="utf-8")
        safe_write_file(str(f), "new content")
        assert f.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_directories(self, tmp_path):
        """Creates parent directories if they don't exist."""
        f = tmp_path / "subdir" / "nested" / "file.txt"
        result = safe_write_file(str(f), "nested content")
        assert "Successfully wrote" in result
        assert f.read_text(encoding="utf-8") == "nested content"

    def test_write_to_directory_fails(self, tmp_path):
        """Raises IsADirectoryError when path is a directory."""
        d = tmp_path / "mydir"
        d.mkdir()
        with pytest.raises(IsADirectoryError, match="directory"):
            safe_write_file(str(d), "content")

    def test_write_to_system_dir_denied(self):
        """Raises PermissionError for system directories."""
        with pytest.raises(PermissionError, match="system directory"):
            safe_write_file("/etc/test.txt", "hacked")

    def test_write_to_ssh_dir_denied(self, tmp_path):
        """Raises PermissionError for .ssh directory."""
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        with pytest.raises(PermissionError, match="restricted"):
            safe_write_file(str(ssh_dir / "config"), "ssh config")

    def test_write_reports_size(self, tmp_path):
        """Return message includes byte count."""
        f = tmp_path / "sized.txt"
        content = "x" * 100
        result = safe_write_file(str(f), content)
        assert "100 bytes" in result

    def test_relative_path_resolved(self, tmp_path, monkeypatch):
        """Relative paths are resolved against cwd."""
        monkeypatch.chdir(tmp_path)
        result = safe_write_file("test.txt", "relative write")
        assert "Successfully wrote" in result
        assert (tmp_path / "test.txt").read_text() == "relative write"


class TestIsWriteDenied:
    """Tests for _is_write_denied."""

    def test_regular_dir_allowed(self, tmp_path):
        denied, _ = _is_write_denied(tmp_path / "test.txt")
        assert denied is False

    def test_etc_denied(self):
        from pathlib import Path
        denied, reason = _is_write_denied(Path("/etc/myapp.conf"))
        assert denied is True
        assert "system directory" in reason

    def test_usr_denied(self):
        from pathlib import Path
        denied, reason = _is_write_denied(Path("/usr/local/bin/script.sh"))
        assert denied is True
