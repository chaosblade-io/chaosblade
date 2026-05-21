"""Tests for chaos_agent.agent.baseline_extractors."""

from chaos_agent.agent.baseline_extractors import (
    _parse_cpu_text_to_mc,
    _parse_mem_text_to_mb,
    extract_pod_top_metrics,
)


class TestParseCpuTextToMc:
    """``_parse_cpu_text_to_mc`` — kubectl CPU strings → millicores."""

    def test_millicore_suffix(self):
        assert _parse_cpu_text_to_mc("1500m") == 1500
        assert _parse_cpu_text_to_mc("50m") == 50

    def test_bare_cores_become_thousand_mc(self):
        assert _parse_cpu_text_to_mc("2") == 2000
        assert _parse_cpu_text_to_mc("0") == 0

    def test_fractional_cores(self):
        assert _parse_cpu_text_to_mc("1.5") == 1500
        assert _parse_cpu_text_to_mc("0.25") == 250

    def test_whitespace_tolerated(self):
        assert _parse_cpu_text_to_mc("  100m  ") == 100

    def test_unrecognised_returns_none(self):
        assert _parse_cpu_text_to_mc("") is None
        assert _parse_cpu_text_to_mc("abc") is None
        assert _parse_cpu_text_to_mc("100Mi") is None  # mem suffix on cpu


class TestParseMemTextToMb:
    """``_parse_mem_text_to_mb`` — kubectl memory strings → integer MiB.

    Boundary cases checked here drive the FCAT P0 size ceiling math, so
    silent format-drift would set burn safety limits to ``None`` and
    surface as "Pod memory limit: unknown" downstream. Each table row
    pins one suffix shape against the expected MiB integer."""

    def test_binary_suffixes(self):
        assert _parse_mem_text_to_mb("120Mi") == 120
        assert _parse_mem_text_to_mb("1Gi") == 1024
        assert _parse_mem_text_to_mb("1.5Gi") == 1536
        # Ki rounds down to 0 unless ≥ 1 MiB
        assert _parse_mem_text_to_mb("1024Ki") == 1
        assert _parse_mem_text_to_mb("512Ki") == 0

    def test_decimal_suffixes_round_to_mib(self):
        # 850M = 850_000_000 bytes ≈ 810 MiB
        assert _parse_mem_text_to_mb("850M") == 810
        # 2G ≈ 1907 MiB
        assert _parse_mem_text_to_mb("2G") == 1907

    def test_bare_bytes(self):
        # 1 MiB in bytes = 1024*1024 = 1048576
        assert _parse_mem_text_to_mb("1048576") == 1

    def test_unrecognised_returns_none(self):
        assert _parse_mem_text_to_mb("") is None
        assert _parse_mem_text_to_mb("abc") is None
        # Mi suffix typo
        assert _parse_mem_text_to_mb("100MMi") is None


class TestExtractPodTopMetrics:
    """``extract_pod_top_metrics`` — parse the ``kubectl top pod`` table
    output. The extractor must match by target pod name (handling the
    label-selector case where multiple pods are listed), and must fail
    soft (``{}``) rather than raise on shape surprises."""

    def _state(self, pod_name: str) -> dict:
        return {"target": {"names": [pod_name]}}

    def test_single_pod_with_header(self):
        stdout = (
            "NAME                              CPU(cores)   MEMORY(bytes)\n"
            "accounting-6fbdb464c7-qn2vr       50m          120Mi\n"
        )
        result = extract_pod_top_metrics(
            stdout, self._state("accounting-6fbdb464c7-qn2vr")
        )
        assert result == {
            "pod_cpu_usage_mc": 50,
            "pod_memory_usage_mb": 120,
        }

    def test_no_header_form(self):
        # --no-headers output (the form direct_execute used to issue)
        stdout = "my-pod-abc   100m   256Mi\n"
        result = extract_pod_top_metrics(stdout, self._state("my-pod-abc"))
        assert result == {
            "pod_cpu_usage_mc": 100,
            "pod_memory_usage_mb": 256,
        }

    def test_multi_pod_label_selector_picks_target(self):
        # When the label selector matches multiple pods, the extractor
        # MUST pick the one whose name matches state.target.names[0]
        # — picking the wrong row would feed FCAT P0 the wrong number.
        stdout = (
            "NAME                              CPU(cores)   MEMORY(bytes)\n"
            "demo-other-pod                    200m         500Mi\n"
            "target-pod-xyz                    30m          60Mi\n"
            "demo-third-pod                    10m          40Mi\n"
        )
        result = extract_pod_top_metrics(stdout, self._state("target-pod-xyz"))
        assert result == {
            "pod_cpu_usage_mc": 30,
            "pod_memory_usage_mb": 60,
        }

    def test_target_pod_not_in_output_returns_empty(self):
        # Selector matched siblings but not the target → caller falls
        # back to direct fetch. Must NOT silently return a sibling's
        # numbers.
        stdout = (
            "NAME                              CPU(cores)   MEMORY(bytes)\n"
            "other-pod-only                    200m         500Mi\n"
        )
        result = extract_pod_top_metrics(stdout, self._state("target-pod-xyz"))
        assert result == {}

    def test_empty_target_names_returns_empty(self):
        result = extract_pod_top_metrics("any output", {"target": {"names": []}})
        assert result == {}

    def test_no_target_key_returns_empty(self):
        result = extract_pod_top_metrics("any output", {})
        assert result == {}

    def test_blank_lines_skipped(self):
        stdout = "\nNAME  CPU(cores)  MEMORY(bytes)\n\nmy-pod  50m  100Mi\n\n"
        result = extract_pod_top_metrics(stdout, self._state("my-pod"))
        assert result["pod_memory_usage_mb"] == 100

    def test_malformed_row_returns_empty(self):
        # A row matching the pod name but missing the MEM column —
        # parser returns {} so caller fallback-fetches rather than
        # asserting on partial data.
        stdout = "my-pod  50m\n"  # only 2 columns
        result = extract_pod_top_metrics(stdout, self._state("my-pod"))
        assert result == {}

    def test_partial_parse_recovered(self):
        # CPU column parseable, MEM column garbage → extractor returns
        # ONLY the parseable field. Better than returning {} since FCAT
        # can still benefit from one of the two.
        stdout = "my-pod  50m  ???\n"
        result = extract_pod_top_metrics(stdout, self._state("my-pod"))
        assert result == {"pod_cpu_usage_mc": 50}
