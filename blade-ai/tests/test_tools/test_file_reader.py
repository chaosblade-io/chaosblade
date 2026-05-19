"""Tests for the unified file reading tool."""

import pytest

from chaos_agent.tools.file_reader import safe_read_file, _is_denylisted


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
