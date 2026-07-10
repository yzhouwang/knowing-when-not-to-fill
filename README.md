# contract-drafting

A typed-abstention guardrail for constrained contract drafting on the open
Accord Project / Concerto standard. One Concerto `.cto` model drives both
JSON-Schema validation and provider-native constrained generation (Anthropic
tool `input_schema`, OpenAI strict `json_schema`), so the validator and the
generator cannot disagree about the type; the render path is a deterministic,
LLM-free Cicero/Mustache pass. Constrained decoding makes malformed output
unrepresentable but not *correct*: on asks the enum cannot represent ("the laws
of Scotland"), the decoder silently substitutes a valid-but-wrong value. The
guardrail — an in-schema `OTHER` sentinel, a free-text raw-capture field, an
abstain instruction, and a prompt-independent intent-consistency gate — turns
that silent substitution into an honest, typed abstention. **The Gauntlet** is
the accompanying record/replay evaluation: four arms x four models, replayed
entirely offline from committed cassettes, no API key. This repo backs the
EMNLP 2026 demo-track paper *"Knowing When Not to Fill: Contract Drafting on
the Accord Standard."*

## Requirements

- **Python 3.12+** (required)
- **Node.js** (optional) — only for regenerating Concerto-derived artifacts
  (`schema.json`, `abstain-policy.json`, TypeScript types) via
  `npm run generate`. Pre-generated artifacts are committed, so the eval, the
  demo, and the test suite all run without Node (three codegen drift tests
  skip when it is absent).
- **pandoc** (optional) — higher-fidelity `.docx` output; a pure-Python
  fallback converter is built in (docx round-trip tests skip without pandoc).
- **API keys are optional.** Every reported result replays offline from the
  committed cassettes (fail-closed on a cache miss); bringing your own key to run
  the models live is an opt-in step (see
  [Bring your own API key](#bring-your-own-api-key-run-it-live)).

## Install

```bash
git clone https://github.com/yzhouwang/knowing-when-not-to-fill && cd knowing-when-not-to-fill
./setup.sh
```

`setup.sh` checks for Python >= 3.12, creates `./venv`, installs
`requirements.txt` into it, and — only if `node` is on PATH — runs
`npm install` for the Concerto build tooling. Manual equivalent:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
npm ci          # optional: Concerto codegen tooling only
```

## Quickstart: replay the evaluation offline

The paper's four model runs replay from committed cassettes in ~1 s each
(commands from Appendix A; note the `deepseek` / `deepseek-v4-pro` cassette
filename divergence). Run with the venv active, or substitute
`venv/bin/python` for `python`:

```bash
P=contract_drafting.gauntlet
python -m $P --suite hard --provider openai \
  --model gpt-5.5 --ablation \
  --cassette data/eval/gauntlet_cassette.openai.hard.json
python -m $P --suite hard --provider deepseek \
  --model deepseek-v4-pro \
  --cassette data/eval/gauntlet_cassette.deepseek.hard.json
python -m $P --suite hard --provider deepseek \
  --model deepseek-v4-flash \
  --cassette data/eval/gauntlet_cassette.deepseek-flash.hard.json
python -m $P --suite hard --provider anthropic \
  --model claude-sonnet-4-6 \
  --cassette data/eval/gauntlet_cassette.anthropic.hard.json
```

Each command replays the full hard suite (57 cases: governing-law plus the
cross-field `entityType`/`disputeForum` probes) through all four arms plus the
Arm E intent gate; `--ablation` additionally emits the slot-only vs
instruction-only ablation rows.

CI guard — replays every committed cassette through the full harness and
fails closed on any cache miss:

```bash
venv/bin/python -m pytest tests/test_gauntlet_replay.py
```

Full test suite (offline; pandoc/Node-dependent tests skip if absent):

```bash
venv/bin/python -m pytest tests/ -q
```

Offline demo driver — the paper's six demonstration beats, from committed
artifacts:

```bash
venv/bin/python -m contract_drafting.demo_offline all      # all six beats
venv/bin/python -m contract_drafting.demo_offline beat 1   # one beat (1..6)
venv/bin/python -m contract_drafting.demo_offline table1   # the paper's Table 1
venv/bin/python -m contract_drafting.demo_offline explain \
  --field governingLaw --asked "laws of Scotland"          # type-error explainer
```

## Bring your own API key: run it live

Everything above runs offline. To watch the
guardrail act on a *live* model — and reproduce the paper's core effect yourself
— export the relevant key(s) and use one of the entry points below. Keys are
read from the environment (or a local `.env`, auto-loaded by `main`); nothing is
written to the repo.

```bash
export ANTHROPIC_API_KEY=...   # --provider anthropic
export OPENAI_API_KEY=...      # --provider openai
export DEEPSEEK_API_KEY=...    # --provider deepseek (gauntlet --record only)
```

**1. The Mars beat — the fastest live demo (one call per arm).** The same
unrepresentable ask down two paths: free-form (validate-and-reject) vs
constrained (well-typed by construction).

```bash
venv/bin/python -m contract_drafting.demo_mars_beat \
  "Draft a mutual NDA between TestCo and AcmeCorp governed by the laws of Mars, term forever." \
  --provider openai --model gpt-5.5
```

Both arms come back schema-*valid* — the free-form one by post-hoc validation,
the constrained one by construction. Because the schema carries the in-schema
`OTHER` sentinel, a capable model reaches for `OTHER` on an ask no enum can
represent (`governingLaw → OTHER`, an honest typed abstention) instead of
inventing a wrong value. The *silent substitution* the guardrail prevents — a
valid-but-wrong fill with no signal — is what the no-sentinel baseline arms of
the Gauntlet (below) measure directly. `--provider` accepts `anthropic` or
`openai` (omit `--model` for the provider default).

**2. Reproduce the Gauntlet on your own model.** `--record` calls the live
provider and writes a fresh cassette, then reports all four arms; drop
`--record` afterward to replay it offline forever. Point `--cassette` at a NEW
path so you never overwrite the committed recordings:

```bash
venv/bin/python -m contract_drafting.gauntlet --record \
  --provider deepseek --model deepseek-v4-flash --suite schema \
  --cassette data/eval/mine.deepseek.schema.json \
  --json data/eval/mine.deepseek.schema.report.json
```

The report shows the effect directly: the constrained arm (C) silently fills a
wrong value on the un-representable case while the constrained+hatch arm (D)
abstains via the sentinel (`silent-wrong` 1→0). Then drop `--record` to replay
that cassette offline — byte-identical. `--provider` accepts
`anthropic`, `openai`, and `deepseek` (DeepSeek is soft `json_object`, not
strict schema). Use `--suite hard` for the 57-case adversarial suite, and add
`--ablation` (its own `--record` pass) to regenerate the cross-field table.

Two things to expect while recording: it prints nothing until it finishes, and
reasoning models are slow — even the 11-case `schema` suite is ~40 live calls
and takes several minutes (longer on `deepseek-v4-pro` / `gpt-5.5`), so start
with `--suite schema` to gauge cost and time. It also prints
`WARNING: --record served a CACHED cassette entry` lines — that is normal within
a run (the hatch and verify arms reuse earlier recordings), not an error; just
keep `--cassette` on a fresh path so re-recording never replays stale entries.

**3. Draft an NDA with live field generation.** The default `cicero` engine is
deterministic and needs no key; `--engine llm` generates the fields with the
provider, then validates against the schema and playbook before rendering
(fail-closed on violations):

```bash
venv/bin/python -m contract_drafting.main --mode draft --engine llm \
  --provider openai --party-a "TestCo" --party-b "AcmeCorp" \
  --jurisdiction "Delaware" --json
```

`--jurisdiction` must name a value the `governingLaw` enum represents — a US
state such as `Delaware`, or a country in display form such as
`"Republic of Singapore"`; an unrepresentable ask fails closed as `BLOCKED` with
the raw value in the audit row. `--provider` accepts `anthropic` or `openai`.
Every live run still ends at the same deterministic, LLM-free render path and
audit trail as the offline demo.

## Draft contracts from the typed templates

Beyond the evaluation, the repo ships six Concerto-typed contract templates. Each
is a `.cto` data model plus a CiceroMark template; drafting fills the typed
fields, validates the instance against the model, and renders it deterministically
(LLM-free) to a Word `.docx`.

| Template | What it is | Notable typed fields |
|----------|------------|----------------------|
| `nda-mutual` | Mutual / one-way non-disclosure agreement | `governingLaw`, `disputeForum`, `entityType` — typed enums with `OTHER` abstain sentinels |
| `consulting` | Consulting / advisory agreement (percentage fee, post-term commission tail) | `consultingFeePercent`, `tailPeriodMonths`, `targetCountry` |
| `intermediary` | Finder / intermediary agreement (finder's fee, per-deal exclusivity) | `finderFeePercent`, `exclusivityExpiryMonths` |
| `partnership` | Operating partnership (capital contributions + reserved-matter governance) | `partyACapitalAmount`, `expenditureThresholdUSD` |
| `strategic-cooperation` | MOU-style cooperation framework | `cooperationAreas`, party descriptions |
| `joint-venture` | JV-company formation (equity split, board, staged capital) | `partyAEquityPercent`, board seats, `registeredCapital` |

**`nda-mutual` — the full governed pipeline (CLI, no key):**

```bash
venv/bin/python -m contract_drafting.main --mode draft --doc-type nda \
  --party-a "Acme AI Inc." --party-b "Beacon Trading Corporation" \
  --jurisdiction "New York" --term-months 24 --effective-date 2026-05-15 \
  --engine cicero --output-path data/drafts/nda.docx
```

This is the one type wired end-to-end through the CLI: it resolves the template,
validates the fields, runs the organizational playbook gate, writes an audit row,
and renders the `.docx` (`Gate: PASS` on success). To have an LLM fill the fields
from your inputs instead of the fixed ones, add `--engine llm --provider …` (see
[Bring your own API key](#bring-your-own-api-key-run-it-live)).

**The other five — render from a typed data instance (Python, no key):**

Each of the other templates has its own Concerto schema, so you hand it a data
dict that satisfies that model and render directly — deterministic and
schema-validated (this path does not run the NDA playbook gate). Run it from the
repo root so `contract_drafting` is importable:

```python
from contract_drafting.cicero_bridge import draft_with_data, markdown_to_docx
from contract_drafting.schema_validator import validate_template_data

data = {  # keys + enum values must satisfy data/templates/cicero/consulting/schema.json
    "agreementNo": "CONS-2026-001", "signingPlace": "Singapore",
    "effectiveDate": "2026-05-15",
    "partyAName": "Acme AI Inc.", "partyAAddress": "1 Example Way, Singapore",
    "partyAContact": "+65 0000 0000", "partyALegalRep": "Alice Tan",
    "partyBName": "Beacon Advisory Ltd", "partyBAddress": "Nairobi",
    "partyBContact": "+254 700 000000", "partyBLegalRep": "Bob Kimani",
    "partyBRoleDescription": "market advisor", "targetCountry": "Kenya",
    "consultingFeePercent": 5, "governingLaw": "Republic of Singapore",
}
assert validate_template_data(data, template_name="consulting") == []   # schema-valid
result = draft_with_data(data, template_name="consulting")
markdown_to_docx(result.text, "data/drafts/consulting.docx")
```

Swap `template_name` for `intermediary`, `partnership`, `strategic-cooperation`,
or `joint-venture`; each schema's `required` field list is in its own
`schema.json`. Only `nda-mutual` runs through the governed CLI gate today; the
other five render through this data API.

## Built on the Accord Project & Concerto

[The Accord Project](https://accordproject.org) is an open-source initiative for
computable ("smart") legal contracts — open standards and libraries for
representing contracts as machine-readable, typed artifacts rather than static
prose, developed in the open at
[github.com/accordproject](https://github.com/accordproject) under Apache-2.0. At
its center is [Concerto](https://concerto.accordproject.org), a lightweight,
object-oriented data-modeling language: a domain model is written once in a
human-readable `.cto` file — concepts, enumerations, typed and optional fields,
constraints — and the [`concerto-codegen`](https://github.com/accordproject/concerto-codegen)
toolchain compiles that single model into JSON Schema, TypeScript, and more.
[Cicero](https://github.com/accordproject/template-archive) is the templating
layer: a template authored in **TemplateMark** (Markdown whose `{{ }}` slots map
to typed model fields) renders a validated data instance into finished contract
text.

This repository is built directly on that stack. A single `org.openclaw.*` `.cto`
model is the one source of truth that drives JSON-Schema validation,
provider-native constrained generation, **and** the typed-abstention policy,
while the render path is a deterministic, LLM-free Cicero/Mustache pass over the
already-validated instance. The abstain sentinel this paper is about lives in the
model, not a prompt: `@Abstainable enum Jurisdiction` plus
`@Abstainable("OTHER", "…instruction…")` on the field (with a companion
`governingLawRaw`) is compiled by codegen into the `abstain-policy.json` the
guardrail enforces.

**Extend a model, or author a new contract type.** Because the `.cto` is the
single source of truth, you change the model and regenerate — no Python edits for
a new field or enum value:

- **Add a jurisdiction, enum value, or field:** edit the enum/concept in
  `data/templates/cicero/<type>/model/model.cto`, then `npm run generate` — it
  regenerates `schema.json`, the TypeScript types, and `abstain-policy.json` from
  the model. Re-validate with `venv/bin/python -m contract_drafting.validate_templates`
  then `venv/bin/python -m pytest tests/ -q`.
- **Author a new contract type:** create `data/templates/cicero/<my-type>/` with a
  `package.json`, a `model/model.cto` (namespace `org.openclaw.<type>@1.0.0`, one
  `@template concept`, and `@Abstainable` enums + `<field>Raw` companions wherever
  a not-representable value must abstain instead of silently substituting), and a
  `text/grammar.tem.md` with `{{field}}` tags. Run `npm run generate`; the Python
  side auto-discovers the new template — no code change.

The Node tooling is only needed when you change a model (`npm ci` first;
committed generated artifacts mean the eval, demo, and drafting all run without
it). Concerto and template authoring are documented at
[docs.accordproject.org](https://docs.accordproject.org).

## Repository layout

| Path | Contents |
|------|----------|
| `contract_drafting/` | The system: drafting pipeline, Cicero bridge, schema validator, intent gate, constrained-generation wrapper, Gauntlet harness, offline demo driver |
| `data/eval/` | Gauntlet artifacts: `hard_suite.json`, record/replay cassettes, and per-case results for all four models |
| `data/templates/` | Concerto-typed contract templates (`cicero/`) and the composable shared-clause library (`shared-clauses/`) |
| `tests/` | Test suite, including the cassette replay CI guard (`test_gauntlet_replay.py`) |
| `.claude/legal.local.md` | The organizational playbook the gate enforces (shared format with the Anthropic Legal Plugin) |

## Reproducing the paper's tables

- **Table 1** (headline `silent-wrong` /6 over the six un-representable
  governing-law asks, `c01`-`c04`, `c06`, `c08`; the numeric/intent cases and
  the 0/3 supported-law controls are reported separately, never pooled):
  `venv/bin/python -m contract_drafting.demo_offline table1` prints it from
  committed results. Each value is also recomputable from the committed
  per-case reports (`data/eval/gauntlet_results.*.hard.json`; re-emit one by
  adding `--json <path>` to a replay command above).
- **Table 2** (cross-field ablation: slot-only vs instruction-only abstention
  on `governingLaw`, `disputeForum`, `entityType`): add `--ablation` to any of
  the four replay commands above; each prints that model's per-field ablation
  rows offline.
- **Appendix per-case matrix**: the committed
  `data/eval/gauntlet_results.openai.hard.json`.
- **Raw-capture fidelity** (24/24 faithful, 22/24 verbatim):
  `venv/bin/python -m contract_drafting.raw_fidelity` regenerates
  `data/eval/raw_fidelity.json` byte-identically (offline, keys unset).
- **Pre-registration** (frozen cross-field decision rule + dated amendments):
  [`PREREGISTRATION_cross-field.md`](PREREGISTRATION_cross-field.md). The
  freeze-before-recording provenance lives in the development history.

## Disclaimer

This system is drafting assistance for qualified counsel, not a substitute
for legal review, and not legal advice. A **PASS** gate certifies schema and
playbook conformance only — never legal correctness.

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Yuzhou Wang. The recorded
model outputs in the `data/eval/` cassettes are redistributed under the
provider-terms note in [`data/eval/DATACARD.md`](data/eval/DATACARD.md).

---

Yuzhou Wang @ SuperX AI
