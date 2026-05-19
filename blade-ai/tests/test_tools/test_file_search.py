"""Tests for the file searching tool."""

import pytest

from chaos_agent.tools.file_search import safe_search_files


class TestSafeSearchFiles:
    """Tests for safe_search_files."""

    def test_search_all_files(self, tmp_path):
        """Search with default pattern returns all files."""
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.yaml").write_text("b", encoding="utf-8")

        result = safe_search_files(str(tmp_path))
        assert "a.txt" in result
        assert "b.yaml" in result
        assert "Found 2 file(s)" in result

    def test_search_by_extension(self, tmp_path):
        """Search with *.yaml returns only YAML files."""
        (tmp_path / "config.yaml").write_text("y", encoding="utf-8")
        (tmp_path / "data.json").write_text("j", encoding="utf-8")

        result = safe_search_files(str(tmp_path), pattern="*.yaml")
        assert "config.yaml" in result
        assert "data.json" not in result
        assert "Found 1 file(s)" in result

    def test_search_recursive(self, tmp_path):
        """Search with **/*.py finds files in subdirectories."""
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("code", encoding="utf-8")
        (sub / "utils.py").write_text("code", encoding="utf-8")
        (tmp_path / "readme.md").write_text("doc", encoding="utf-8")

        result = safe_search_files(str(tmp_path), pattern="**/*.py")
        assert "main.py" in result
        assert "utils.py" in result
        assert "readme.md" not in result

    def test_search_no_matches(self, tmp_path):
        """Returns no-matches message when pattern matches nothing."""
        (tmp_path / "file.txt").write_text("a", encoding="utf-8")

        result = safe_search_files(str(tmp_path), pattern="*.xyz")
        assert "No files matching" in result

    def test_search_nonexistent_directory(self):
        """Raises FileNotFoundError for missing directories."""
        with pytest.raises(FileNotFoundError, match="not found"):
            safe_search_files("/nonexistent/path/xyz")

    def test_search_file_instead_of_directory(self, tmp_path):
        """Raises NotADirectoryError when path is a file."""
        f = tmp_path / "file.txt"
        f.write_text("a", encoding="utf-8")

        with pytest.raises(NotADirectoryError, match="not a directory"):
            safe_search_files(str(f))

    def test_search_denylisted_directory(self, tmp_path):
        """Raises PermissionError for denylisted directories."""
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("ssh", encoding="utf-8")

        with pytest.raises(PermissionError, match="restricted"):
            safe_search_files(str(ssh_dir))

    def test_search_shows_file_sizes(self, tmp_path):
        """Results include file sizes."""
        (tmp_path / "small.txt").write_text("hi", encoding="utf-8")

        result = safe_search_files(str(tmp_path), pattern="*.txt")
        assert "bytes" in result

    def test_search_result_limit(self, tmp_path):
        """Results are capped at max_results."""
        for i in range(60):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}", encoding="utf-8")

        result = safe_search_files(str(tmp_path), pattern="*.txt", max_results=5)
        assert "showing first 5 of 60" in result
