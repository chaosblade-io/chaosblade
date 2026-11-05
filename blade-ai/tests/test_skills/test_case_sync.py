"""Contract tests for skills/case_sync.py — capabilities-sync generation (v2).

Pinned behaviors:
1. sync_capabilities derives commands from every fault-injection skill's
   catalogue (data-driven multi-skill; caller passes catalogue_roots).
2. Each case carries a ``skill`` field naming its source skill.
3. Parse failure OR a schema-incomplete answer → feedback retry (the model's
   own answer is appended and it is told what to fix; converges within the
   attempt budget, and an exhausted budget keeps the partial answer rather than
   dropping the case).
4. Transport failure (LLM invoke raises) → terminal FAILED for that case,
   no case-layer retry (the LLM client owns the transport retry budget).
5. kubectl cases: the kubectl-native triple / structured_cmd are kept —
   the k8s_native provider injects by triple, so a complete triple means
   executable=true.
6. Output registry JSON: v2 envelope (schema_version/lang/generated_at/
   skills_fingerprint/total/cases), atomic write to disk.
7. Case naming is content-first: the md's own **用例名称** line wins over any
   filename convention; filename derivation is only the fallback.
8. v2 capability definition: stable id, family/profile/cluster_scoped
   derivation, executable flag, params normalization.
9. Cross-language alignment: passing ``reference_registry`` anchors the
   structural fields (triple/params) to the first-language artifact, so both
   languages stay id-isomorphic despite LLM derivation jitter; prose fields
   stay localized.
10. The skills fingerprint is stamped BEFORE the case bodies are read, so it
    always describes the inputs the artifact was actually built from.
11. ``cache_path`` reuses an unchanged case's distillation without an LLM call;
    a prompt change, a ``model_tag`` change or a case edit forces
    re-distillation, failures and schema-incomplete answers are never cached,
    ``cache_path=None`` (the default) touches no cache at all, and
    ``use_cache=False`` skips reading while still rebuilding the file.
12. Cache completeness is key presence, not non-empty values: an answer with an
    empty scope/target/action triple is a legitimate non-executable case and is
    cached like any other.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from chaos_agent.skills import case_sync


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------

class TestPromptContract:
    """Wiring tests for the generation prompt's hard constraints."""

    def test_lang_parameter_defaults_to_english(self):
        """Language is caller-controlled; default must stay "en".

        Regression: a 2026-08 run produced 42 ZH / 48 EN nl_cmd entries
        because language was unconstrained. It is now pinned per run via
        --lang (default en, cn opt-in).
        """
        import inspect

        sig = inspect.signature(case_sync.sync_capabilities)
        assert sig.parameters["lang"].default == "en"
        assert set(case_sync._LANGUAGE_SECTIONS) == {"en", "cn"}

    def test_lang_cn_pins_chinese_and_en_pins_english(self):
        cn = case_sync._SYSTEM_PROMPT + case_sync._LANGUAGE_SECTIONS["cn"]
        en = case_sync._SYSTEM_PROMPT + case_sync._LANGUAGE_SECTIONS["en"]
        assert "简体中文" in cn and "简体中文" not in en
        assert "in English" in en
        for prompt in (cn, en):
            assert "fault_symptom" in prompt and "nl_cmd" in prompt


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, content: str):
        self.content = content
        self.additional_kwargs = {}


class FakeLLM:
    """Scripted LLM: pops one scripted response per ainvoke call."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)


def _make_catalogue(root: Path, category: str, cases: dict[str, str]) -> Path:
    cat_dir = root / "references" / "catalogue" / category
    cat_dir.mkdir(parents=True)
    for stem, body in cases.items():
        (cat_dir / f"{category}_{stem}.md").write_text(body, encoding="utf-8")
    return root / "references" / "catalogue"


def _blade_json(**overrides) -> str:
    payload = {
        "inject_kind": "blade",
        "title": "CPU fullload",
        "fault_symptom": "cpu high",
        "scope": "pod",
        "target": "cpu",
        "action": "fullload",
        "params": [
            {"name": "labels", "kind": "target_resource", "resolved_from": "environment",
             "required": True, "default": "", "description": "pod selector"},
            {"name": "cpu-percent", "kind": "fault_param", "resolved_from": "user",
             "required": False, "default": "80", "description": "cpu load percent"},
        ],
        "nl_cmd": 'blade-ai inject -i "stress cpu"',
        "structured_cmd": "blade-ai inject --scope pod --target cpu --action fullload",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _run_sync(tmp_path: Path, llm, roots: dict[str, Path], cache_path: Path | None = None) -> dict:
    out = tmp_path / "out.json"
    return asyncio.run(case_sync.sync_capabilities(roots, llm, out, cache_path=cache_path))


def _truncated_json(*drop: str) -> str:
    """A parseable answer that omits ``drop`` — what a truncated or partly
    formed model response looks like once the JSON is extracted."""
    payload = json.loads(_blade_json())
    for key in drop:
        payload.pop(key)
    return json.dumps(payload)


class _MidRunEditor(FakeLLM):
    """Edits the catalogue on its first call — what a maintainer saving a case
    doc during a minutes-long run looks like from the sync's point of view."""

    def __init__(self, responses: list, on_first_call):
        super().__init__(responses)
        self._on_first_call = on_first_call
        self._fired = False

    async def ainvoke(self, messages):
        if not self._fired:
            self._fired = True
            self._on_first_call()
        return await super().ainvoke(messages)


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------

class TestSyncRetryLoop:
    def test_parse_failure_feeds_back_and_retries(self, tmp_path):
        """Non-JSON answer → model gets its own answer + a 'JSON only' nudge
        appended, and the second answer is accepted."""
        good = _blade_json()
        llm = FakeLLM(["Here is some prose instead of JSON", good])
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, llm, roots)

        assert llm.calls and len(llm.calls) == 2
        assert catalog["total"] == 1
        assert catalog["cases"][0]["structured_cmd"] == json.loads(good)["structured_cmd"]
        # Second call carries the feedback pair
        second = llm.calls[1]
        assert second[-2].content.startswith("Here is some prose")
        assert "could not be parsed as JSON" in second[-1].content

    def test_schema_incomplete_answer_feeds_back_and_retries(self, tmp_path):
        """Parseable but off-schema → same feedback-and-retry treatment, with
        the missing key names in the nudge.

        Regression: the loop only asked "did it parse?". A truncated answer
        like ``{"inject_kind": "blade"}`` parsed fine, so it was shaped into a
        silently degraded case — inject_kind kept, everything else defaulted to
        ""/[]/unknown — with no retry and no signal in the output.
        """
        partial = _truncated_json("structured_cmd", "params")
        llm = FakeLLM([partial, _blade_json()])
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, llm, roots)

        assert len(llm.calls) == 2
        second = llm.calls[1]
        assert second[-2].content == partial
        nudge = second[-1].content
        assert "missing these required keys" in nudge
        assert "structured_cmd" in nudge and "params" in nudge
        # The repaired answer is what lands in the registry.
        assert catalog["cases"][0]["executable"] is True
        assert catalog["cases"][0]["structured_cmd"]

    def test_schema_incomplete_answer_still_yields_a_row_when_retries_run_out(
            self, tmp_path):
        """Budget exhausted → keep the partial answer. A row carrying some real
        fields beats the all-empty FAILED placeholder, and the case must not
        vanish from the registry."""
        partial = _truncated_json("structured_cmd")
        llm = FakeLLM([partial, partial, partial])
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, llm, roots)

        assert len(llm.calls) == 3
        assert catalog["total"] == 1
        case = catalog["cases"][0]
        assert case["inject_kind"] == "blade"          # real fields survived
        assert case["nl_cmd"] == json.loads(partial)["nl_cmd"]
        assert case["structured_cmd"] == ""            # the missing one is empty
        assert case["executable"] is True              # triple was present

    def test_transport_failure_is_terminal(self, tmp_path):
        """ConnectionResetError surfaces after the client's own retry budget —
        case-layer must not re-invoke; the case ends FAILED with a hint."""
        llm = FakeLLM([ConnectionResetError("boom")])
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, llm, roots)

        assert len(llm.calls) == 1
        case = catalog["cases"][0]
        assert case["inject_kind"] == "unknown"
        assert case["structured_cmd"] == ""
        # 无三元组的失败 case 不可执行（如实呈现，不伪装能力）
        assert case["executable"] is False


# ---------------------------------------------------------------------------
# kubectl semantics
# ---------------------------------------------------------------------------

class TestSyncKubectl:
    def test_kubectl_case_keeps_triple(self, tmp_path):
        """Control-plane case: kubectl-native triple 与 structured_cmd 保留
        （k8s_native provider 按三元组注入/反向恢复）。"""
        payload = _blade_json(
            inject_kind="kubectl",
            scope="node",
            target="schedule",
            action="drain",
            structured_cmd="blade-ai inject --scope node --target schedule --action drain",
        )
        llm = FakeLLM([payload])
        roots = {"s1": _make_catalogue(tmp_path / "s1", "Node", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, llm, roots)

        case = catalog["cases"][0]
        assert case["inject_kind"] == "kubectl"
        assert "--action drain" in case["structured_cmd"]
        # 三元组齐备 → 可一键注入（kubectl-native 后端）
        assert case["executable"] is True
        assert (case["scope"], case["target"], case["action"]) == ("node", "schedule", "drain")

    def test_kubectl_case_without_triple_not_executable(self, tmp_path):
        """三元组缺失（模型推导失败）的 kubectl case 如实标为不可执行。"""
        payload = _blade_json(
            inject_kind="kubectl",
            scope="", target="", action="",
            structured_cmd="",
        )
        llm = FakeLLM([payload])
        roots = {"s1": _make_catalogue(tmp_path / "s1", "Node", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, llm, roots)

        case = catalog["cases"][0]
        assert case["executable"] is False


# ---------------------------------------------------------------------------
# Case naming
# ---------------------------------------------------------------------------

class TestCaseNaming:
    def test_content_case_name_wins_over_filename(self, tmp_path):
        """A freely named file (no category prefix) whose content carries the
        **用例名称** line gets exactly that name — filenames are not forced."""
        cat_dir = tmp_path / "s1" / "references" / "catalogue" / "CPU"
        cat_dir.mkdir(parents=True)
        (cat_dir / "随便起名.md").write_text(
            "**用例名称** Sidecar容器CPU资源争抢 导致 CPU\n\n**故障现象**：\n1. x\n",
            encoding="utf-8",
        )
        llm = FakeLLM([_blade_json()])

        catalog = _run_sync(tmp_path, llm, {"s1": tmp_path / "s1" / "references" / "catalogue"})

        assert catalog["cases"][0]["use_case_name"] == "Sidecar容器CPU资源争抢 导致 CPU"

    def test_filename_fallback_when_content_has_no_name(self, tmp_path):
        """Legacy file without a **用例名称** line falls back to the
        {category}_{root_cause} filename derivation."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# no name line"})}
        llm = FakeLLM([_blade_json()])

        catalog = _run_sync(tmp_path, llm, roots)

        assert catalog["cases"][0]["use_case_name"] == "caseA 导致 CPU"


# ---------------------------------------------------------------------------
# Multi-skill merge
# ---------------------------------------------------------------------------

class TestSyncMultiSkill:
    def test_merges_all_catalogues_and_tags_skill(self, tmp_path):
        """Cases from every catalogue root are merged into one catalog and
        each record carries its source skill name."""
        roots = {
            "k8s-skill": _make_catalogue(
                tmp_path / "k8s", "CPU", {"caseK": "# k"}),
            "host-skill": _make_catalogue(
                tmp_path / "host", "MEM", {"caseH": "# h"}),
        }
        llm = FakeLLM([_blade_json(), _blade_json(inject_kind="blade")])

        out = tmp_path / "out.json"
        catalog = asyncio.run(case_sync.sync_capabilities(roots, llm, out))

        assert catalog["total"] == 2
        # v2 envelope：schema/lang/指纹/生成时间齐备，case 带稳定 id 与参数 schema
        assert catalog["schema_version"] == 2
        assert catalog["lang"] == "en"  # sync 默认英文（--lang cn 出 zh）
        assert "skills_fingerprint" in catalog and "generated_at" in catalog
        for c in catalog["cases"]:
            assert c["id"] and "family" in c and "profile" in c
            assert isinstance(c["params"], list)
        by_skill = {c["skill"]: c for c in catalog["cases"]}
        assert set(by_skill) == {"k8s-skill", "host-skill"}
        assert by_skill["k8s-skill"]["category"] == "CPU"
        assert by_skill["host-skill"]["category"] == "MEM"
        # 三元组齐备的 blade case 可执行；参数槽位归一化（环境槽位/用户参数）
        case = by_skill["k8s-skill"]
        assert case["executable"] is True
        assert (case["scope"], case["target"], case["action"]) == ("pod", "cpu", "fullload")
        kinds = {p["name"]: p["kind"] for p in case["params"]}
        assert kinds == {"labels": "target_resource", "cpu-percent": "fault_param"}
        # resource_path stays relative to the skill directory (existing contract)
        for c in catalog["cases"]:
            assert c["case_path"].startswith("references/catalogue/")
        # File was written atomically with the same content
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["total"] == 2
        assert written["schema_version"] == 2
        assert "blade_version" not in written

    def test_duplicate_triples_get_distinct_stable_ids(self, tmp_path):
        """同一三元组的多个 case（变体）获得后缀递增的稳定 id。"""
        roots = {
            "s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a", "caseB": "# b"}),
        }
        llm = FakeLLM([_blade_json(), _blade_json()])

        catalog = asyncio.run(case_sync.sync_capabilities(roots, llm, tmp_path / "out.json"))

        ids = [c["id"] for c in catalog["cases"]]
        assert len(set(ids)) == 2
        assert ids[0] == "k8s.pod.cpu.fullload"
        assert ids[1] == "k8s.pod.cpu.fullload.2"


# ---------------------------------------------------------------------------
# Cross-language alignment
# ---------------------------------------------------------------------------

class TestCrossLanguageAlignment:
    """双语产物必须 id 同构：第二语言生成时用 --reference 锚定结构字段。

    回归：2026-08 首次双语生成时 LLM 三元组推导漂移，27/90 个 case 的
    id 在 zh/en 间不一致，平台按 id 合并双语文本失效。
    """

    def test_reference_anchors_structure_and_keeps_ids_isomorphic(self, tmp_path):
        # 第一语言（zh）：正常生成
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        zh_catalog = asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json(title="CPU满载", fault_symptom="CPU 过高")]),
            tmp_path / "zh.json", lang="cn",
        ))

        # 第二语言（en）：LLM 漂移出了不同三元组/参数，但传了 reference
        drifted = _blade_json(
            title="CPU Fullload", fault_symptom="cpu high",
            target="process", action="kill",  # 漂移的三元组
            params=[{"name": "cpu-percent", "kind": "fault_param", "resolved_from": "user",
                     "required": False, "default": "90", "description": "cpu load percent"}],
        )
        en_catalog = asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([drifted]), tmp_path / "en.json", lang="en",
            reference_registry=tmp_path / "zh.json",
        ))

        zh_case, en_case = zh_catalog["cases"][0], en_catalog["cases"][0]
        # id 完全一致（结构锚定后派生结果相同）
        assert en_case["id"] == zh_case["id"]
        # 三元组/槽位结构锚定到参考，不被漂移污染
        assert (en_case["scope"], en_case["target"], en_case["action"]) == ("pod", "cpu", "fullload")
        names = [p["name"] for p in en_case["params"]]
        assert names == [p["name"] for p in zh_case["params"]]
        # 同名槽位保留英文 description，缺失槽位回退参考文本
        by_name = {p["name"]: p for p in en_case["params"]}
        assert by_name["cpu-percent"]["description"] == "cpu load percent"
        assert by_name["labels"]["description"] == "pod selector"
        # prose 字段保持本次语言生成结果
        assert en_case["title"] == "CPU Fullload"
        assert en_case["symptom"] == "cpu high"

    def test_unreadable_reference_is_a_noop(self, tmp_path):
        """参考文件不可读 → 告警跳过对齐，不阻断生成。"""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        catalog = asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json()]), tmp_path / "en.json",
            reference_registry=tmp_path / "missing.json",
        ))
        assert catalog["total"] == 1
        assert catalog["cases"][0]["target"] == "cpu"

    def test_prose_fallback_uses_registry_key_for_symptom(self, tmp_path):
        """LLM 失败时 fault_symptom 从 reference 的 symptom 键回落。

        回归：build_registry 把 fault_symptom 重命名为 symptom 写入文件，
        但 _align_with_reference 在 build_registry 之前跑，ref_case 来自
        文件（键是 symptom），_PROSE_FIELDS 用 fault_symptom 查 → 恒 None
        → 回落永不触发。修复：_PROSE_REF_KEY_MAP 做键名映射。
        """
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        # zh 正常生成 → 文件里 symptom 有值
        asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json(fault_symptom="CPU 飙高到 95%")]),
            tmp_path / "zh.json", lang="cn",
        ))
        # en LLM 失败 → 兜底 dict 的 fault_symptom 是空串
        en_catalog = asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([RuntimeError("network timeout")]),
            tmp_path / "en.json", lang="en",
            reference_registry=tmp_path / "zh.json",
        ))
        en_case = en_catalog["cases"][0]
        # 修复前：symptom 是空串（回落查 fault_symptom 键，文件里叫 symptom，恒 None）
        # 修复后：从 reference 的 symptom 键回落成功
        assert en_case["symptom"] == "CPU 飙高到 95%", (
            f"fault_symptom 回落失败，symptom={en_case['symptom']!r}"
        )
        # title/nl_cmd 同样回落（两侧键名一致，原本就正常）
        assert en_case["title"] == "CPU fullload"
        assert "blade-ai inject" in en_case["nl_cmd"]


# ---------------------------------------------------------------------------
# Fingerprint timing
# ---------------------------------------------------------------------------

class TestFingerprintTiming:
    def test_fingerprint_describes_the_inputs_the_artifact_was_built_from(self, tmp_path, monkeypatch):
        """The stamp is taken BEFORE any case body is read.

        Regression: it used to be computed at the very end of the run, so an
        edit landing inside the (minutes-long) distillation window produced an
        artifact holding the OLD content stamped with the NEW fingerprint —
        every later staleness check then passed while the registry was wrong,
        which is worse than failing: the drift is invisible.
        """
        cat = _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# original"})
        roots = {"s1": cat}
        state = {"fp": "fp-before"}
        monkeypatch.setattr(case_sync, "_compute_sync_fingerprint", lambda: state["fp"])

        def _edit_midway():
            (cat / "CPU" / "CPU_caseA.md").write_text("# edited mid-run", encoding="utf-8")
            state["fp"] = "fp-after"

        llm = _MidRunEditor([_blade_json()], _edit_midway)
        catalog = _run_sync(tmp_path, llm, roots)

        # Both sides of the artifact agree on the pre-edit input.
        assert catalog["skills_fingerprint"] == "fp-before"
        assert llm.calls[0][1].content == "# original"

    def test_build_registry_computes_late_when_not_given(self, monkeypatch):
        """``convert_v1_to_v2`` assembles from data it already holds, so the
        historical late computation stays for that caller."""
        monkeypatch.setattr(case_sync, "_compute_sync_fingerprint", lambda: "fp-late")
        assert case_sync.build_registry([])["skills_fingerprint"] == "fp-late"

    def test_build_registry_honours_an_explicit_fingerprint(self, monkeypatch):
        monkeypatch.setattr(case_sync, "_compute_sync_fingerprint", lambda: "fp-late")
        registry = case_sync.build_registry([], skills_fingerprint="fp-early")
        assert registry["skills_fingerprint"] == "fp-early"

    def test_fingerprint_failure_does_not_abort_the_sync(self, tmp_path, monkeypatch):
        """The fingerprint is metadata about the product, not the product."""
        def _boom():
            raise RuntimeError("skills dir unreadable")

        monkeypatch.setattr(case_sync, "_compute_sync_fingerprint", _boom)
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, FakeLLM([_blade_json()]), roots)

        assert catalog["total"] == 1
        assert catalog["skills_fingerprint"] == ""


# ---------------------------------------------------------------------------
# Per-case distillation cache
# ---------------------------------------------------------------------------

class TestDistillationCache:
    """A sync run is one LLM call per case (~115 today) while a typical edit
    touches one case. Re-distilling the rest to produce a byte-identical
    artifact is pure cost — but a reuse that survives a prompt change or a case
    edit would be a silent correctness bug, so both are pinned here."""

    def test_second_run_reuses_unchanged_case_without_llm(self, tmp_path):
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        first = _run_sync(tmp_path, FakeLLM([_blade_json()]), roots, cache_path=cache)
        assert cache.exists()

        # An exhausted FakeLLM pops from an empty list, so any call would raise
        # IndexError — the empty response list IS the assertion.
        replay = FakeLLM([])
        second = _run_sync(tmp_path, replay, roots, cache_path=cache)

        assert replay.calls == []
        assert second["cases"] == first["cases"]

    def test_edited_case_goes_back_to_the_model(self, tmp_path):
        cat = _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})
        roots = {"s1": cat}
        cache = tmp_path / "cache.json"
        _run_sync(tmp_path, FakeLLM([_blade_json()]), roots, cache_path=cache)

        (cat / "CPU" / "CPU_caseA.md").write_text("# a, edited", encoding="utf-8")
        llm = FakeLLM([_blade_json(title="edited title")])
        catalog = _run_sync(tmp_path, llm, roots, cache_path=cache)

        assert len(llm.calls) == 1
        assert catalog["cases"][0]["title"] == "edited title"

    def test_only_the_new_case_is_distilled(self, tmp_path):
        cat = _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})
        roots = {"s1": cat}
        cache = tmp_path / "cache.json"
        _run_sync(tmp_path, FakeLLM([_blade_json()]), roots, cache_path=cache)

        (cat / "CPU" / "CPU_caseB.md").write_text("# b", encoding="utf-8")
        llm = FakeLLM([_blade_json(scope="node", target="schedule", action="drain")])
        catalog = _run_sync(tmp_path, llm, roots, cache_path=cache)

        assert len(llm.calls) == 1                       # caseA reused, caseB distilled
        assert catalog["total"] == 2
        triples = {(c["scope"], c["target"], c["action"]) for c in catalog["cases"]}
        assert triples == {("pod", "cpu", "fullload"), ("node", "schedule", "drain")}

    def test_prompt_change_invalidates_the_whole_cache(self, tmp_path):
        """The language section is part of the prompt, and the prompt is part of
        the cache identity — so sharing ONE cache file across languages is safe
        but useless: every switch drops the other language's entries.
        Correctness backstop; the cost half is fixed by giving each language its
        own file (see ``test_per_language_caches_do_not_evict`` and
        ``capabilities.distill_cache_path``)."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json()]), tmp_path / "en.json", lang="en", cache_path=cache))

        cn = FakeLLM([_blade_json(title="CPU满载")])
        catalog = asyncio.run(case_sync.sync_capabilities(
            roots, cn, tmp_path / "zh.json", lang="cn", cache_path=cache))

        assert len(cn.calls) == 1
        assert catalog["lang"] == "zh"
        assert catalog["cases"][0]["title"] == "CPU满载"

    def test_failures_are_not_cached(self, tmp_path):
        """A transient outage must not be frozen into every later run."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        failed = _run_sync(tmp_path, FakeLLM([ConnectionResetError("boom")]), roots, cache_path=cache)
        assert failed["cases"][0]["inject_kind"] == "unknown"

        retried = FakeLLM([_blade_json()])
        ok = _run_sync(tmp_path, retried, roots, cache_path=cache)

        assert len(retried.calls) == 1
        assert ok["cases"][0]["inject_kind"] == "blade"

    def test_deleted_case_drops_out_of_the_cache(self, tmp_path):
        cat = _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a", "caseB": "# b"})
        roots = {"s1": cat}
        cache = tmp_path / "cache.json"
        _run_sync(tmp_path, FakeLLM([_blade_json(), _blade_json()]), roots, cache_path=cache)
        assert len(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == 2

        (cat / "CPU" / "CPU_caseB.md").unlink()
        llm = FakeLLM([])                                  # caseA reused; nothing else may run
        catalog = _run_sync(tmp_path, llm, roots, cache_path=cache)

        assert catalog["total"] == 1
        assert llm.calls == []
        assert len(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == 1

    def test_corrupt_cache_falls_back_to_a_full_run(self, tmp_path):
        """The cache is an optimization: it may never be the reason a sync
        refuses to run."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        cache.write_text("{not json", encoding="utf-8")

        llm = FakeLLM([_blade_json()])
        catalog = _run_sync(tmp_path, llm, roots, cache_path=cache)

        assert len(llm.calls) == 1
        assert catalog["total"] == 1

    def test_cache_path_none_leaves_no_cache_behind(self, tmp_path):
        """Default is OFF: a caller that passes no cache_path must not read or
        write one, or every test run would scribble on the real
        ``~/.blade-ai/memory``."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        catalog = _run_sync(tmp_path, FakeLLM([_blade_json()]), roots)

        assert catalog["total"] == 1
        assert sorted(p.name for p in tmp_path.glob("*.json")) == ["out.json"]

    def test_cache_survives_reference_alignment(self, tmp_path):
        """The cache stores the RAW model answer, not the assembled registry
        case — so anchoring to a reference registry still applies on a fully
        cached run. Caching the post-processed case instead would silently
        drop the id-isomorphism guarantee whenever the cache hit."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        zh = asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json(title="CPU满载")]), tmp_path / "zh.json", lang="cn",
        ))

        # Warm the cache with a DRIFTED answer (same language, so the prompt —
        # and therefore the cache — matches the run below).
        cache = tmp_path / "cache.json"
        drifted = _blade_json(title="CPU Fullload", target="process", action="kill")
        asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([drifted]), tmp_path / "warm.json", lang="en", cache_path=cache,
        ))

        replay = FakeLLM([])  # exhausted: any LLM call raises IndexError
        en = asyncio.run(case_sync.sync_capabilities(
            roots, replay, tmp_path / "en.json", lang="en",
            reference_registry=tmp_path / "zh.json", cache_path=cache,
        ))

        assert replay.calls == []
        # Structure anchored to the reference despite coming from the cache.
        assert en["cases"][0]["id"] == zh["cases"][0]["id"]
        assert (en["cases"][0]["scope"], en["cases"][0]["target"], en["cases"][0]["action"]) \
            == ("pod", "cpu", "fullload")
        # Prose stays this run's own generation.
        assert en["cases"][0]["title"] == "CPU Fullload"

    def test_per_language_caches_do_not_evict_each_other(self, tmp_path):
        """cn → en → cn: the third run must be free.

        Regression: one shared cache file plus a language-dependent prompt
        fingerprint meant every language switch wiped the whole file, so the
        bilingual refresh the platform needs (``catalog.py`` loads zh AND en)
        re-distilled all ~115 cases on every alternating run, forever.
        """
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        zh_cache, en_cache = tmp_path / "cache_zh.json", tmp_path / "cache_en.json"

        def _run(lang: str, cache: Path, llm) -> dict:
            return asyncio.run(case_sync.sync_capabilities(
                roots, llm, tmp_path / f"{lang}.json", lang=lang, cache_path=cache))

        cn1 = FakeLLM([_blade_json(title="CPU满载")])
        _run("cn", zh_cache, cn1)
        en1 = FakeLLM([_blade_json(title="CPU fullload")])
        _run("en", en_cache, en1)
        assert len(cn1.calls) == len(en1.calls) == 1      # both cold

        cn2 = FakeLLM([])                                  # exhausted: any call raises
        again = _run("cn", zh_cache, cn2)

        assert cn2.calls == []
        assert again["cases"][0]["title"] == "CPU满载"
        # The en cache is intact too — writing zh never touched it.
        en2 = FakeLLM([])
        assert _run("en", en_cache, en2)["cases"][0]["title"] == "CPU fullload"
        assert en2.calls == []

    def test_no_cache_reruns_every_case_but_rewrites_the_cache(self, tmp_path):
        """``use_cache=False`` disables READING, not writing.

        Regression: the flag used to pass ``cache_path=None``, which disabled
        both. The forced run's fresh answers were then thrown away while the
        previous generation stayed on disk, so the next ordinary run reused
        exactly the entries the caller had just paid to replace — and left a
        fresh registry beside a stale cache.
        """
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json(title="stale title")]), tmp_path / "a.json",
            cache_path=cache))

        forced = FakeLLM([_blade_json(title="fresh title")])
        catalog = asyncio.run(case_sync.sync_capabilities(
            roots, forced, tmp_path / "b.json", cache_path=cache, use_cache=False))

        assert len(forced.calls) == 1                      # the warm entry was ignored
        assert catalog["cases"][0]["title"] == "fresh title"

        # …and the cache now holds THIS run's answer, not the one it replaced.
        replay = FakeLLM([])
        after = asyncio.run(case_sync.sync_capabilities(
            roots, replay, tmp_path / "c.json", cache_path=cache))
        assert replay.calls == []
        assert after["cases"][0]["title"] == "fresh title"

    def test_no_cache_without_a_path_still_writes_nothing(self, tmp_path):
        """``cache_path=None`` stays a hard OFF in both directions, so a caller
        that never opted in cannot scribble on ``~/.blade-ai/memory``."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}

        asyncio.run(case_sync.sync_capabilities(
            roots, FakeLLM([_blade_json()]), tmp_path / "out.json",
            cache_path=None, use_cache=False))

        assert sorted(p.name for p in tmp_path.glob("*.json")) == ["out.json"]

    def test_model_change_invalidates_the_whole_cache(self, tmp_path):
        """The answer is a function of (prompt, case body, model); the cache
        identity used to cover only the first two.

        Regression: switching models left every entry in place, so the
        "refresh" re-emitted the old model's answers with zero LLM calls, and
        nothing on disk recorded which model had produced them.
        """
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"

        def _run(llm, model_tag: str) -> dict:
            return asyncio.run(case_sync.sync_capabilities(
                roots, llm, tmp_path / "out.json", cache_path=cache, model_tag=model_tag))

        weak = FakeLLM([_blade_json(title="weak-model answer")])
        _run(weak, "qwen-weak@https://api.example/v1")
        assert len(weak.calls) == 1

        strong = FakeLLM([_blade_json(title="strong-model answer")])
        catalog = _run(strong, "qwen-strong@https://api.example/v1")

        assert len(strong.calls) == 1                       # nothing was reused
        assert catalog["cases"][0]["title"] == "strong-model answer"

    def test_endpoint_change_alone_invalidates_the_cache(self, tmp_path):
        """Same model NAME served by a different endpoint is a different
        generator — a self-hosted ``qwen-max`` and the vendor's are not
        interchangeable, and the tag the CLI composes must say so."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"

        def _run(llm, model_tag: str) -> dict:
            return asyncio.run(case_sync.sync_capabilities(
                roots, llm, tmp_path / "out.json", cache_path=cache, model_tag=model_tag))

        vendor = FakeLLM([_blade_json(title="vendor answer")])
        _run(vendor, "qwen-max@https://dashscope.example/v1")

        local = FakeLLM([_blade_json(title="self-hosted answer")])
        catalog = _run(local, "qwen-max@http://127.0.0.1:8000/v1")

        assert len(local.calls) == 1
        assert catalog["cases"][0]["title"] == "self-hosted answer"

    def test_same_generator_keeps_the_cache(self, tmp_path):
        """The other half: the tag is an identity, not a nonce. A run under the
        same generator must still be free, or the fix would just turn the cache
        off."""
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        tag = "qwen-max@https://api.example/v1"

        def _run(llm) -> dict:
            return asyncio.run(case_sync.sync_capabilities(
                roots, llm, tmp_path / "out.json", cache_path=cache, model_tag=tag))

        cold = FakeLLM([_blade_json(title="cached answer")])
        _run(cold)
        assert len(cold.calls) == 1

        replay = FakeLLM([])                                  # exhausted: any call raises
        warm = _run(replay)

        assert replay.calls == []
        assert warm["cases"][0]["title"] == "cached answer"

    def test_incomplete_answer_is_not_cached(self, tmp_path):
        """A degraded answer must stay a one-run accident, not a permanent
        artifact.

        Regression: ``_remember`` only refused EMPTY results, so a parseable
        but off-schema answer was frozen — every later run replayed it with
        zero LLM calls until someone edited that case's markdown, whereas a
        run without the cache would have re-asked and probably healed.
        """
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        partial = _truncated_json("structured_cmd")
        degraded = _run_sync(
            tmp_path, FakeLLM([partial, partial, partial]), roots, cache_path=cache)

        assert degraded["cases"][0]["structured_cmd"] == ""     # this run did degrade
        assert json.loads(cache.read_text(encoding="utf-8"))["entries"] == {}

        # …so the next run asks again and gets a real answer.
        healed = FakeLLM([_blade_json()])
        ok = _run_sync(tmp_path, healed, roots, cache_path=cache)

        assert len(healed.calls) == 1
        assert ok["cases"][0]["structured_cmd"]
        assert len(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == 1

    def test_complete_answer_with_an_empty_triple_is_still_cached(self, tmp_path):
        """Completeness is KEY PRESENCE, not non-empty values.

        The prompt allows a case the model could not reduce to a
        scope/target/action triple — it is reported as non-executable rather
        than invented (pinned by ``test_kubectl_case_without_triple_not_
        executable``). Demanding values here would reject that legitimate
        answer and re-distill it on every run forever.
        """
        roots = {"s1": _make_catalogue(tmp_path / "s1", "CPU", {"caseA": "# a"})}
        cache = tmp_path / "cache.json"
        no_triple = _blade_json(inject_kind="kubectl", scope="", target="", action="")
        first = _run_sync(tmp_path, FakeLLM([no_triple]), roots, cache_path=cache)

        assert first["cases"][0]["executable"] is False
        assert len(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == 1

        replay = FakeLLM([])
        again = _run_sync(tmp_path, replay, roots, cache_path=cache)

        assert replay.calls == []                             # reused, not re-asked
        assert again["cases"][0]["executable"] is False
