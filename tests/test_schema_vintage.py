"""
Schema-vintage regression tests (audit fix plan N-E).

_strip_hatch -- the pre-hatch/baseline schema builder -- is VERSIONED:

  v1-leaky  (default) reproduces the 2026-06 recording condition BYTE-FOR-BYTE: it
            strips the <field>Raw companions + abstain sentinels from the top-level
            properties (and $ref'd enum definitions) but leaves the duplicated concept
            blob under definitions['...NDAData'] untouched, so its four *Raw fields
            survive in the serialized baseline prompt (the disclosed definitions-block
            leak). The four committed hard-suite cassettes key on those bytes.
  v2-clean  additionally deep-strips the *Raw fields + sentinels from EVERY nested
            definitions copy -- for NEW recordings only (the raw_clean condition).

The byte-pins below were snapshotted from HEAD *before* the vintage mechanism landed.
If any v1 pin moves, every committed baseline (arm A/B/C) cassette replay fails closed:
the RecordReplayCaller key sha256-hashes the prompt, and the prompt context embeds the
serialized no-hatch schema.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from contract_drafting import demo_mars_beat as demo
from contract_drafting import gauntlet as g
from contract_drafting import schema_validator as sv

_INSTR = "Draft a mutual NDA between TestCo and AcmeCorp governed by the laws of Atlantis."

# sha256 pins of build_prompt's (question, context) snapshotted at the pre-vintage HEAD
# (2026-07-04, branch docs/paper-editions) for _INSTR on nda-mutual.
_V1_NOHATCH_QUESTION_SHA = "2d894c988ed1a786eee30a03c05c7fcc1067e9ba4efab376d25f83c408cae096"
_V1_NOHATCH_CONTEXT_SHA = "957987c78fc0591044c77d2a4a6414b4d8669d9d8993a4bb2d3f3956fc02376c"
# The hatch (with_abstain=True) prompt never goes through _strip_hatch, so it must be
# vintage-INDEPENDENT: same pin under v1 and v2.
_HATCH_CONTEXT_SHA = "982d50576fce0497653b37e12d8e0eec886bb0c988e494c0872c91c54f4122d5"

# The nine governing-law cases of the hard suite (6 un-representable probes + 3
# supported-law controls) -- the raw_clean condition's case filter.
_GOVLAW_IDS = {"c01", "c02", "c03", "c04", "c06", "c08", "c27", "c28", "c29"}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _concept():
    return demo._sanitize_schema(sv._get_concept_schema(sv._load_schema("nda-mutual")))


# ---------------------------------------------------------------------------
# v1-leaky: byte-pinned to the committed cassettes
# ---------------------------------------------------------------------------
class TestV1LeakyBytePin:
    def test_default_vintage_is_v1_leaky(self):
        assert sv.active_schema_vintage() == sv.SCHEMA_VINTAGE_V1_LEAKY == "v1-leaky"

    def test_v1_nohatch_prompt_bytes_identical_to_head_snapshot(self):
        """N-E test (a): the no-hatch (arm A/B/C) prompt bytes under the default vintage
        are IDENTICAL to the pre-vintage HEAD snapshot. If this fails, the committed
        2026-06 cassettes no longer replay -- do not update the pin without re-recording."""
        q, ctx, _ = demo.build_prompt(_INSTR, "nda-mutual", with_abstain=False)
        assert _sha(q) == _V1_NOHATCH_QUESTION_SHA
        assert _sha(ctx) == _V1_NOHATCH_CONTEXT_SHA

    def test_v1_explicit_param_equals_ambient_default(self):
        concept = _concept()
        assert (sv._strip_hatch(concept, "nda-mutual")
                == sv._strip_hatch(concept, "nda-mutual", vintage="v1-leaky"))

    def test_v1_leak_is_present_by_construction(self):
        """Documents the leak v1 deliberately PRESERVES: the four *Raw companions survive
        in the definitions blob of the no-hatch prompt (top-level copies are stripped, and
        no sentinel survives anywhere -- the enum definitions are $ref'd and stripped)."""
        _, ctx, schema = demo.build_prompt(_INSTR, "nda-mutual", with_abstain=False)
        assert ctx.count("Raw") == 4
        assert '"OTHER"' not in ctx
        nda_def = schema["definitions"]["org.openclaw.nda@1.0.0.NDAData"]["properties"]
        assert "governingLawRaw" in nda_def          # the definitions-block leak
        assert "governingLawRaw" not in schema["properties"]  # top-level IS stripped


# ---------------------------------------------------------------------------
# v2-clean: the corrected builder for new recordings
# ---------------------------------------------------------------------------
class TestV2Clean:
    def test_v2_nohatch_prompt_has_no_hatch_vocabulary(self):
        """N-E test (b): under v2-clean, the no-hatch prompt context contains no 'Raw'
        token and no sentinel value anywhere (definitions copies included)."""
        with sv.schema_vintage("v2-clean"):
            _, ctx, _ = demo.build_prompt(_INSTR, "nda-mutual", with_abstain=False)
        assert "Raw" not in ctx
        assert '"OTHER"' not in ctx
        assert "OTHER_ENTITY" not in ctx
        assert "OTHER_FORUM" not in ctx

    def test_v2_hatch_prompt_keeps_sentinel_and_raw_fields(self):
        """N-E test (c): the HATCH (with_abstain=True) prompt is untouched by the vintage
        -- the sentinel and the *Raw capture fields are the guardrail, not the leak."""
        with sv.schema_vintage("v2-clean"):
            _, ctx, schema = demo.build_prompt(_INSTR, "nda-mutual", with_abstain=True)
        assert '"OTHER"' in ctx
        assert "governingLawRaw" in ctx
        assert "disputeForumRaw" in ctx
        assert "governingLawRaw" in schema["properties"]
        # byte-identical to the v1 hatch prompt (vintage-independent pin)
        assert _sha(ctx) == _HATCH_CONTEXT_SHA

    def test_v2_strips_definitions_copy(self):
        concept = _concept()
        v1 = sv._strip_hatch(concept, "nda-mutual", vintage="v1-leaky")
        v2 = sv._strip_hatch(concept, "nda-mutual", vintage="v2-clean")
        v1_def = v1["definitions"]["org.openclaw.nda@1.0.0.NDAData"]["properties"]
        v2_def = v2["definitions"]["org.openclaw.nda@1.0.0.NDAData"]["properties"]
        for raw in ("governingLawRaw", "disclosingEntityTypeRaw",
                    "receivingEntityTypeRaw", "disputeForumRaw"):
            assert raw in v1_def, f"v1 must PRESERVE the {raw} definitions copy (the pin)"
            assert raw not in v2_def, f"v2 must deep-strip the {raw} definitions copy"
        assert "Raw" not in json.dumps(v2) and '"OTHER"' not in json.dumps(v2)

    def test_v2_baseline_still_schema_rejects_the_sentinel(self):
        """The v2 no-hatch schema must (like v1) reject the OTHER sentinel: fixing the
        raw-field leak must not silently re-admit the sentinel to the baseline enum."""
        fields = {"disclosingParty": "A", "receivingParty": "B", "effectiveDate": "2026-01-15",
                  "purpose": "x", "termMonths": 24, "noticeDays": 30, "survivalYears": 3,
                  "governingLaw": "OTHER", "mutual": True, "hasNonCompete": False,
                  "hasNonSolicitation": False, "hasResidualsClause": False}
        with sv.schema_vintage("v2-clean"):
            errors = sv.validate_template_data(fields, with_abstain=False)
        assert any("governingLaw" in e for e in errors)


# ---------------------------------------------------------------------------
# the vintage mechanism itself
# ---------------------------------------------------------------------------
class TestVintageMechanism:
    def test_unknown_vintage_rejected(self):
        with pytest.raises(ValueError):
            with sv.schema_vintage("v3-imaginary"):
                pass  # pragma: no cover
        with pytest.raises(ValueError):
            sv._strip_hatch(_concept(), "nda-mutual", vintage="v3-imaginary")

    def test_context_restores_on_exit_and_on_exception(self):
        assert sv.active_schema_vintage() == "v1-leaky"
        with sv.schema_vintage("v2-clean"):
            assert sv.active_schema_vintage() == "v2-clean"
        assert sv.active_schema_vintage() == "v1-leaky"
        with pytest.raises(RuntimeError):
            with sv.schema_vintage("v2-clean"):
                raise RuntimeError("boom")
        assert sv.active_schema_vintage() == "v1-leaky"


# ---------------------------------------------------------------------------
# the raw_clean condition (plumbing only; recording happens later)
# ---------------------------------------------------------------------------
_BASE = {
    "disclosingParty": "TestCo", "receivingParty": "AcmeCorp",
    "effectiveDate": "2026-01-15", "governingLaw": "Washington",
    "disclosingEntityType": "corporation", "receivingEntityType": "corporation",
    "disputeForum": "SIAC",
    "purpose": "exploring a potential deal", "termMonths": 24,
    "noticeDays": 30, "survivalYears": 3, "mutual": True,
    "hasNonCompete": False, "hasNonSolicitation": False, "hasResidualsClause": False,
}


class _TextFake(demo.LLMCaller):
    """Arm-A fake: fixed schema-valid fill for any text request; captures the prompt
    context so the test can assert which schema vintage the condition actually sent."""

    def __init__(self):
        self.contexts: list[str] = []

    def text(self, question, context, *, provider, model, system_prompt):
        self.contexts.append(context)
        return json.dumps(_BASE)

    def structured(self, *a, **k):
        raise AssertionError("raw_clean is a text-mode (arm A) condition; structured must not be called")


class TestRawCleanCondition:
    def test_filters_to_the_nine_governinglaw_cases_and_uses_v2_bytes(self, tmp_path):
        suite = g.build_suite("nda-mutual", mode="hard")
        fake = _TextFake()
        caller = g.RecordReplayCaller(fake, tmp_path / "clean.json", mode="record")
        results = g.run_raw_clean(suite, caller=caller, provider="openai", model="gpt-5.5")
        assert {r.case_id for r in results} == _GOVLAW_IDS
        assert all(r.arm == "raw_clean" for r in results)
        # every prompt the condition sent was v2-clean: no hatch vocabulary anywhere
        assert fake.contexts and all(
            "Raw" not in c and '"OTHER"' not in c for c in fake.contexts)
        # ambient vintage restored after the run
        assert sv.active_schema_vintage() == "v1-leaky"

    def test_replay_without_cleanbase_cassette_fails_closed(self, tmp_path):
        suite = g.build_suite("nda-mutual", mode="hard")
        caller = g.RecordReplayCaller(None, tmp_path / "never_recorded.cleanbase.json",
                                      mode="replay")
        with pytest.raises(g.GauntletCacheMiss):
            g.run_raw_clean(suite, caller=caller, provider="openai", model="gpt-5.5")

    def test_cleanbase_keys_disjoint_from_v1_keys(self, tmp_path):
        """The v2 schema changes the prompt bytes, hence the replay keys: a cassette
        recorded by raw_clean can never satisfy a v1 (default) arm-A replay -- the two
        vintages CANNOT be mixed, even over the identical cases."""
        suite = g.build_suite("nda-mutual", mode="hard")
        path = tmp_path / "clean.json"
        g.run_raw_clean(suite, caller=g.RecordReplayCaller(_TextFake(), path, mode="record"),
                        provider="openai", model="gpt-5.5")
        assert len(json.loads(path.read_text())) == len(_GOVLAW_IDS)  # one entry per case
        replay = g.RecordReplayCaller(None, path, mode="replay")
        case = next(c for c in suite if c.id == "c01")
        with pytest.raises(g.GauntletCacheMiss):
            g.run_raw(case, caller=replay, provider="openai", model="gpt-5.5")

    def test_cleanbase_cassette_path_pattern(self):
        assert g.cleanbase_cassette_path("openai", "gpt-5.5").name == \
            "gauntlet_cassette.openai.hard.cleanbase.json"
        assert g.cleanbase_cassette_path("anthropic", "claude-sonnet-4-6").name == \
            "gauntlet_cassette.anthropic.hard.cleanbase.json"
        # the two DeepSeek models get DISTINCT cassettes (mirrors the committed naming)
        assert g.cleanbase_cassette_path("deepseek", "deepseek-v4-pro").name == \
            "gauntlet_cassette.deepseek.hard.cleanbase.json"
        assert g.cleanbase_cassette_path("deepseek", "deepseek-v4-flash").name == \
            "gauntlet_cassette.deepseek-flash.hard.cleanbase.json"
        assert g.cleanbase_cassette_path("openai").parent.name == "eval"

    def test_cli_replay_of_unrecorded_cleanbase_fails_closed_with_message(self, capsys, tmp_path):
        """`--raw-clean` replay of a cassette that was never recorded must exit
        non-zero with a clear message (never a silent live call, never an
        empty-table success). Points at a tmp path with the canonical basename:
        the REPO cassettes exist since the 2026-07-04 recording, so the guard must
        not depend on the tree's recording state."""
        missing = tmp_path / "gauntlet_cassette.openai.hard.cleanbase.json"
        rc = g.main(["--raw-clean", "--suite", "hard", "--provider", "openai",
                     "--model", "gpt-5.5", "--cassette", str(missing)])
        assert rc == 2
        out = capsys.readouterr().out
        assert "REPLAY FAILED" in out and "not recorded yet" in out
        assert "gauntlet_cassette.openai.hard.cleanbase.json" in out
