"""Tests for knowledge_reader internal functions: outline, section sizes, grouping."""

from chaos_agent.tools.knowledge_reader import (
    _build_outline,
    _calculate_section_sizes,
    _format_size,
)


# ── _format_size ──────────────────────────────────────────────────────


class TestFormatSize:
    """Test character-count → size hint formatting."""

    def test_small_size(self):
        assert _format_size(200) == "(~200c)"

    def test_exact_1000(self):
        assert _format_size(1000) == "(~1.0Kc)"

    def test_1500(self):
        assert _format_size(1500) == "(~1.5Kc)"

    def test_large_size(self):
        assert _format_size(20000) == "(~20.0Kc)"

    def test_zero(self):
        assert _format_size(0) == "(~0c)"

    def test_999(self):
        assert _format_size(999) == "(~999c)"


# ── _calculate_section_sizes ──────────────────────────────────────────


class TestCalculateSectionSizes:
    """Test heading boundary detection and size computation."""

    def test_flat_document_only_h2(self):
        """Flat doc with only ## headings — each section size is its own content."""
        content = (
            "## Section A\n"
            "Line 1\n"
            "Line 2\n"
            "## Section B\n"
            "Line 3\n"
        )
        sizes = _calculate_section_sizes(content)
        assert len(sizes) == 2
        assert sizes[0][0] == "Section A"
        assert sizes[0][1] == 2  # level
        assert sizes[0][2] > 0  # char count
        assert sizes[1][0] == "Section B"

    def test_hierarchical_document(self):
        """3-level hierarchy: ## → ### → ####."""
        content = (
            "## Group 1\n"
            "### Sub A\n"
            "Content A\n"
            "#### Deep A\n"
            "Deep content\n"
            "### Sub B\n"
            "Content B\n"
            "## Group 2\n"
            "Content 2\n"
        )
        sizes = _calculate_section_sizes(content)
        # Group 1 size = from "## Group 1" line to "## Group 2" line
        # Sub A size = from "### Sub A" to "### Sub B"
        # Deep A size = from "#### Deep A" to "### Sub B"
        # Sub B size = from "### Sub B" to "## Group 2"
        # Group 2 size = from "## Group 2" to end
        assert len(sizes) == 5

        # Verify levels
        assert sizes[0][1] == 2  # Group 1
        assert sizes[1][1] == 3  # Sub A
        assert sizes[2][1] == 4  # Deep A
        assert sizes[3][1] == 3  # Sub B
        assert sizes[4][1] == 2  # Group 2

        # Group 1 section includes all its subsections
        group1_size = sizes[0][2]
        sub_a_size = sizes[1][2]
        sub_b_size = sizes[3][2]
        # Group 1 should be larger than any single subsection
        assert group1_size > sub_a_size
        assert group1_size > sub_b_size

    def test_no_headings(self):
        """Document with no headings returns empty list."""
        content = "Just some text\nNo headings here\n"
        sizes = _calculate_section_sizes(content)
        assert sizes == []

    def test_only_h1_headings_skipped(self):
        """# (title) level headings are skipped, only >= ## processed."""
        content = "# Title\nSome text\n## Section 1\nMore text\n"
        sizes = _calculate_section_sizes(content)
        assert len(sizes) == 1
        assert sizes[0][0] == "Section 1"

    def test_last_section_extends_to_end_of_file(self):
        """Last heading's section extends to end of file."""
        content = "## Only Section\nLine 1\nLine 2\n"
        sizes = _calculate_section_sizes(content)
        assert len(sizes) == 1
        # splitlines() strips \n, so char count is sum of line lengths
        # (content chars, not raw bytes). Size should be > 0 and include
        # heading + content lines.
        assert sizes[0][2] > 0
        assert sizes[0][2] >= len("## Only Section") + len("Line 1") + len("Line 2")


# ── _build_outline ────────────────────────────────────────────────────


class TestBuildOutline:
    """Test outline generation with size hints and grouping."""

    def test_flat_document_no_subsections(self):
        """Flat doc: ## headings with no ### children — no grouping annotation."""
        content = (
            "## Advisory Rules\n"
            "Some content\n"
            "## Blast Radius\n"
            "More content\n"
        )
        outline = _build_outline(content)
        assert "Available headings:" in outline
        assert "Advisory Rules" in outline
        assert "Blast Radius" in outline
        # No "[N subsections]" because these ## headings have no ### children
        assert "subsections]" not in outline

    def test_hierarchical_with_subsections(self):
        """Hierarchical doc: ## headings annotated with subsection count."""
        content = (
            "## Group 1\n"
            "### Sub A\n"
            "Content A\n"
            "### Sub B\n"
            "Content B\n"
            "## Group 2\n"
            "### Sub C\n"
            "Content C\n"
        )
        outline = _build_outline(content)
        assert "Group 1" in outline
        assert "[2 subsections]" in outline
        assert "Group 2" in outline
        assert "[1 subsections]" in outline

    def test_size_hints_present(self):
        """All headings show size hints."""
        content = "## Section A\n" + "X" * 200 + "\n"
        outline = _build_outline(content)
        assert "(~" in outline  # Some size hint is present
        assert "c)" in outline

    def test_large_section_shows_Kc(self):
        """Sections over 1000 chars show Kc format."""
        content = "## Big Section\n" + "X" * 5000 + "\n"
        outline = _build_outline(content)
        assert "Kc)" in outline

    def test_no_headings_recommends_full_doc(self):
        """Document with no headings recommends loading full document."""
        content = "Plain text without any headings.\n"
        outline = _build_outline(content)
        assert "No section/subsection-level headings found" in outline
        assert "Recommend: call without section" in outline
        assert "(~" in outline  # Total doc size hint

    def test_indentation_levels(self):
        """### and #### headings get proper indentation."""
        content = (
            "## Top\n"
            "### Middle\n"
            "#### Deep\n"
        )
        outline = _build_outline(content)
        # ## has 0 indent, ### has 2 spaces, #### has 4 spaces
        lines = outline.splitlines()
        # Find heading lines (skip "Available headings:" header)
        heading_lines = [ln for ln in lines if ln.strip().startswith("-")]
        assert any(ln.startswith("- Top") for ln in heading_lines)
        assert any(ln.startswith("  - Middle") for ln in heading_lines)
        assert any(ln.startswith("    - Deep") for ln in heading_lines)

    def test_h4_not_counted_as_subsection(self):
        """#### headings are NOT counted in [N subsections] annotation."""
        content = (
            "## Group\n"
            "#### Deep Only\n"
            "Content\n"
        )
        outline = _build_outline(content)
        # Only ### headings count as subsections; #### doesn't
        assert "subsections]" not in outline