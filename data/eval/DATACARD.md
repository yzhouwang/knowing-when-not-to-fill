# Data card — The Gauntlet eval artifacts (`data/eval/`)

Provenance and recording conditions for the committed Gauntlet artifacts: the
red-teamed hard suite, the four per-model record/replay cassettes, the derived
results files, and the raw-fidelity artifact. Everything below is verifiable from
this repository (file hashes, `git log`, and the cited source lines); nothing is
reconstructed from memory.

## Suite

| File | Contents | sha256 |
|---|---|---|
| `hard_suite.json` | 57 hand-authored cases: un-representable governing-law asks (c01–c08 band), numeric/date/policy probes, supported-value controls (c27–c29 law + entity/forum controls), and the ec*/fc* cross-field replication probes. Each case carries author-adjudicated ground truth (`representable`, `expected_correct`, `expect_constrained_substitution`). | `42a29ba92c9d74d7a2b48858f637fff2850ab54be6ad652f33c6b987a3b17a63` |

## Cassettes (recorded LLM request→response, replayed offline)

Each cassette maps `sha256(request-key) -> {kind, response}` and is replayed by
`contract_drafting/gauntlet.py::RecordReplayCaller` (fail-closed on a miss; see
`tests/test_gauntlet_replay.py`). Recording commits/dates from
`git log --follow -- data/eval/gauntlet_cassette.<x>.hard.json`:

| Cassette | Provider | Model alias | Recorded (commits, `git log --follow`) |
|---|---|---|---|
| `gauntlet_cassette.openai.hard.json` | OpenAI | `gpt-5.5` | `edd1c9a` 2026-06-05 → `974014b` 2026-06-06 → `8a82c49` 2026-06-07 → `01c627d` 2026-06-09 |
| `gauntlet_cassette.deepseek.hard.json` | DeepSeek | `deepseek-v4-pro` | `edd1c9a` 2026-06-05 → `974014b` 2026-06-06 → `8a82c49` 2026-06-07 → `1bdc88e` 2026-06-08 → `01c627d` 2026-06-09 |
| `gauntlet_cassette.deepseek-flash.hard.json` | DeepSeek | `deepseek-v4-flash` | `edd1c9a` 2026-06-05 → `974014b` 2026-06-06 → `8a82c49` 2026-06-07 → `1bdc88e` 2026-06-08 → `01c627d` 2026-06-09 |
| `gauntlet_cassette.anthropic.hard.json` | Anthropic | `claude-sonnet-4-6` | `8a82c49` 2026-06-07 → `1bdc88e` 2026-06-08 → `01c627d` 2026-06-09 |

All recording happened in the window **2026-06-05 .. 2026-06-09**. The derived
`gauntlet_results.*.hard.json` files are deterministic replays of these cassettes
(last regenerated at `7c1d1e7`, 2026-06-10) and reproduce byte-identically offline.

## Decoding / sampling configuration

- **Sampling = provider defaults.** The harness sets no `temperature`, `top_p`,
  `top_k`, or `seed` anywhere — verified by
  `grep -rn "temperature\|top_p\|seed" contract_drafting/` (no matches). Replays are
  exactly reproducible because responses are recorded; re-*recording* is not
  guaranteed deterministic.
- **max_tokens:**
  - OpenAI and DeepSeek record callers: **8192**
    (`contract_drafting/eval_providers.py`, `_EVAL_MAX_TOKENS = 8192` — sized for
    reasoning models that spend hidden tokens before content).
  - Anthropic path: **4096** (`eval_providers.make_record_caller("anthropic")`
    returns `demo_mars_beat.LiveCaller`, which calls `llm.call_llm` /
    `call_llm_structured` at their default `max_tokens=MAX_TOKENS_LLM`;
    `contract_drafting/llm.py`, `MAX_TOKENS_LLM = 4096`).
- **Constraint mechanism per provider** (the capability difference the benchmark
  measures, see `eval_providers.py` docstring):
  - OpenAI: provider-native **strict `json_schema`** via the production
    `llm.call_llm_structured` path (hard constraint).
  - DeepSeek: **`json_object` JSON mode only** (the API exposes no strict
    `json_schema`) — a SOFT constraint; valid JSON but not schema/enum conformance
    by construction.
  - Anthropic: tool `input_schema` constrained generation via the production
    `llm.call_llm_structured` path.

## Prompt provenance

- **System prompt:** `contract_drafting/demo_mars_beat.py::_SYSTEM` (frozen; the
  replay cache key hashes it, and `tests/test_contract_drafting.py` pins it
  byte-identically).
- **Abstain instruction (arm D / hatch):** single-sourced from the `@Abstainable`
  decorator in the Concerto model — codegen emits
  `data/templates/cicero/nda-mutual/abstain-policy.json`, and
  `demo_mars_beat._abstain_system(field)` composes `_SYSTEM + " " + instruction`.
  It is generated, not hand-written in Python.
- **DeepSeek structured path** additionally appends its own JSON-only sentence to
  the system prompt: `" Output ONLY a single JSON object that conforms to the
  schema."` (`eval_providers.py`, `DeepSeekCaller.structured`) — part of the
  recorded cache key for that provider.

## Adjudication provenance

Gold labels (`representable`, `expected_correct`, the substitution expectations)
were **authored by the paper's authors and machine-scored** on replay
(`gauntlet.py::ArmResult.outcome`); no third-party annotators and **no lawyers** —
correctness is a lay standard ("the filled value names the jurisdiction the
instruction asked for"), not a legal-sufficiency judgment. The authors are also the
guardrail's designers; this conflict is stated rather than hidden. The denominators
are cross-checked against the suite's own case flags at every run
(`demo_offline._check_table1_denominators`).

## Approximate API recording cost (estimate — labeled, not measured)

The harness records no billing data. The committed results files carry a
deterministic per-row `est_tokens` (chars/4 over prompt+context+response;
`gauntlet.py::_est_tokens`). Summing `est_tokens` over the 228 rows (57 cases x 4
arms) of each `gauntlet_results.<x>.hard.json`:

| Model | est_tokens sum | Public price (June 2026, per 1M in/out) | Cost bound (all-input .. all-output) |
|---|---:|---|---|
| gpt-5.5 | 297,155 | $5.00 / $30.00 (openai.com pricing) | ~$1.5 .. ~$8.9 |
| deepseek-v4-pro | 296,256 | $1.74 / $3.48 (api-docs.deepseek.com, standard post-promo rates; the 75%-off promo ended 2026-05-31, before this recording window) | ~$0.5 .. ~$1.0 |
| deepseek-v4-flash | 296,551 | $0.14 / $0.28 (api-docs.deepseek.com, cache-miss input) | ~$0.04 .. ~$0.08 |
| claude-sonnet-4-6 | 305,723 | $3.00 / $15.00 (Anthropic pricing) | ~$0.9 .. ~$4.6 |

**Order of magnitude: low single-digit to ~$15 total across all four models.**
Caveats, all of which push the true figure up from the all-input bound: (a)
`est_tokens` is a character heuristic, not provider-billed tokens; (b) it covers
the four main arms only — the ablation and intent-guard conditions and any
recording retries are not in the sum (intent-guard replays arm C's recordings, so
it added no API calls); (c) hidden reasoning tokens (gpt-5.5, deepseek-v4-pro)
are billed but not visible in recorded text. Treat the table as a documented
estimate of recording cost, not an invoice.

## Derived artifacts

- `gauntlet_results.<x>.hard.json` — full per-case/per-arm reports replayed from
  the cassettes (`python -m contract_drafting.gauntlet --suite hard --provider ...
  --model ... --cassette ... --json ...`); guarded byte-stable by
  `tests/test_gauntlet_replay.py`.
- `raw_fidelity.json` — fidelity grading of arm D's `governingLawRaw` captures on
  the six un-representable governing-law cases x four models (faithful / verbatim /
  case-insensitive-substring per row, with the grading rules documented in the
  generator). Regenerate byte-identically with
  `python -m contract_drafting.raw_fidelity`; guarded by
  `tests/test_raw_fidelity.py` (fails closed on drift).
- `abstain_hatch.*.json` — earlier (pre-cross-field) per-model hatch summaries,
  kept for history; superseded by the hard results files.

## Known limitations

- One template (`nda-mutual`), English-only instructions, 57 cases; the suite is
  red-teamed by the authors, not sampled from production traffic.
- Single recording per (case, arm, model): no variance estimate across samples —
  the committed numbers are exact for these recordings, not means.
- DeepSeek's "constrained" arm is JSON-mode (soft), so its arm C/D results must
  not be read as a by-construction schema guarantee (see above).

## Redistribution of recorded outputs

The cassettes are recorded completions from OpenAI (gpt-5.5), Anthropic
(claude-sonnet-4-6), and DeepSeek (v4-pro/flash), captured for offline,
reproducible research evaluation. The license boundary is explicit:

- The repository's Apache-2.0 grant covers the code, the documentation, the
  hand-authored case inputs (synthetic fixtures — no real party data), and
  whatever rights, if any, the authors hold in the recorded outputs.
- The authors claim no rights in the provider-generated text beyond those each
  provider's terms assign to the account that generated it, and grant no
  rights they do not hold.
- Each provider's usage terms travel with its outputs: before redistributing
  or reusing the recorded outputs, downstream users are responsible for their
  own compliance with the then-current OpenAI, Anthropic, and DeepSeek terms.

## Mined-findings artifacts (added 2026-07-04)

Five additional graded artifacts, each recomputed OFFLINE from the committed
cassettes above through the gauntlet's own arm implementations (never by parsing
raw cassette JSON), deterministic (indent=2, sorted keys, trailing newline), and
guarded byte-stable by `tests/test_mined_findings.py` (fail-closed on drift, the
`test_raw_fidelity.py` pattern). No new recording was performed: every number
below is mined from the 2026-06-05..09 cassettes.

### `baseline_leak.json` (2026-07-04)

- **What:** the definitions-block schema leak in the baseline arms. The arm A/B/C
  prompt schema still carried the `*Raw` hatch fields inside the duplicated
  `NDAData` definition (`schema_validator._strip_hatch` strips top-level
  properties only), so the no-hatch arms could route the asked jurisdiction into
  `governingLawRaw`. Per model × arm over the six un-representable governing-law
  cases: fills whose non-empty `governingLawRaw` NAMES the asked jurisdiction
  (raw: gpt-5.5 5/6, v4-pro 3/6, v4-flash 5/6, sonnet 6/6; verify-reject
  identical; constrained: 0/6, 5/6, 4/6, 0/6), the same count on the
  supported-law controls (gpt-5.5 and sonnet 0 — discriminative; the DeepSeek
  models echo on some), and the counterfactual silent-wrong if a model-level raw
  flag counted as a signal (pooled: raw 23/24→5/24, verify-reject 24/24→5/24,
  constrained 24/24→15/24).
- **How:** replay arms A/B/C per model; flag rule = the committed
  `raw_fidelity._KEY_NAMES` normalized-containment rule (DIFC also matches its
  spelled-out form).
- **Command:** `python -m contract_drafting.mined_findings --only baseline_leak`

### `forced_fill.json` (2026-07-04)

- **What:** the `disputeForum` forced-fill decomposition over the 41 non-forum
  cases (57 minus the 16 `fc*` cases — none asks for a forum), arms A/C/D per
  model: filled / OTHER_FORUM / concrete / absent. Under the OpenAI strict
  massage gpt-5.5 is grammar-forced to fill 41/41; with the sentinel (arm D) it
  routes 32/41 into OTHER_FORUM vs 9 concrete (7 of the 9 are SIAC on
  Singapore-governing-law cases — a linked inference, verified from the suite
  instructions); without it (arm C) the same forced fills become 41/41 concrete
  confabulations (AAA_ICDR 20, LCIA 7, SIAC 7, JAMS 3, DIAC 2, ICC 1, HKIAC 1).
  Pooled unrequested concrete inventions: arm A 39/164 (gpt 3, pro 8, flash 10,
  sonnet 18 — voluntary, no grammar forces arm A), arm C 67/164 (41/3/12/11),
  arm D 13/164 (9/1/3/0).
- **How:** replay arms A/C/D; values normalized display↔identifier with the
  grader's own enum map before classification.
- **Command:** `python -m contract_drafting.mined_findings --only forced_fill`

### `directionality.json` (2026-07-04)

- **What:** substitution directionality on the six un-representable governing-law
  cases pooled over the no-abstention arms (A/B/C, 12 cells per case): every
  filled wrong-jurisdiction value lands on the asked jurisdiction's in-enum
  parent/sibling/constituent when one exists — 44/44 (DIFC→UAE 18/18 filled,
  US-federal→New_York 12/12, Scotland→England_and_Wales 6/6, Macao→PRC 7/8 +
  one sonnet Hong_Kong_SAR 1/8); Ontario, the one probe with NO in-enum relative
  (the 65-member Jurisdiction enum has no Canadian entry), scatters: 9/12 omit +
  3 divergent cells (New_York, England_and_Wales, one schema-invalid verbatim
  'Ontario'). Plus the entityType folk matrix over the 12 `ec*` probes: 10/12
  probes draw the SAME substituted form from ≥3/4 models (GmbH→LLC 4/4,
  cooperative→general_partnership 4/4, KK/SA/plc/Pte/ULC→corporation 4/4).
- **How:** replay arms A/B/C; relative maps fixed a priori from legal geography
  (`mined_findings._relatives`), guarded against the live enum on every
  regeneration.
- **Command:** `python -m contract_drafting.mined_findings --only directionality`

### `mined_misc.json` (2026-07-04)

- **What:** (a) the c20/c21 typed-surface scoping pair — the euphemized
  non-compete typed as `hasNonCompete=true` is playbook-BLOCKED 16/16 (4 models ×
  4 arms); the same provision relocated into the ungated free-text `purpose`
  field ships gate=PASS with no structured flag 16/16 (the suite's own `why_hard`
  text, included verbatim, predicted the pair). (b) the impossible-date band:
  c13/c14 (2026-02-30 / 2026-02-31) ship schema-valid + gate PASS on 24/24
  gpt/v4-pro/v4-flash cells; sonnet objects in free text the harness cannot parse
  (quotes extracted verbatim) and under constrained decoding silently ships
  corrected dates (c13→2026-02-28, c14→2026-04-30; its unlandable raw JSON had
  proposed 2026-02-28 for both). Production wiring fact, verified live at
  generation: `schema_validator.validate_semantics` rejects both dates and is
  called by both draft paths (`compliance_draft::_draft_cicero` /
  `::_draft_llm`); the gauntlet oracle deliberately measures pure schema
  validity and does not wire it. (c) c11's event-relative effective date is
  confabulated concrete 16/16 (12/16 earlier than the recording window start
  2026-06-05; 10 distinct values). (d) sonnet's invented `<UNKNOWN>` sentinel on
  c23 (schema-INVALID — a visible leak).
- **How:** replay all four arms for c20/c21/c13/c14/c11/c23; sonnet's free-text
  objections replayed via the same cassette text requests arm A issues.
- **Command:** `python -m contract_drafting.mined_findings --only mined_misc`

### `raw_fidelity_extended.json` (2026-07-04)

- **What:** the committed `raw_fidelity.json` grader extended to arm D's other
  raw-capture channels: `disputeForumRaw` on the 12 `fc*` probes (48/48 arm-D
  abstentions faithful — the raw names the asked forum) and
  disclosing/receiving `entityTypeRaw` on the 12 `ec*` probes (38/38 faithful
  over the 38 abstained cells; the 10 non-abstained DeepSeek substitution cells
  are excluded and listed per model). Combined with the committed
  governingLawRaw 24/24: **110/110 intended-channel raw captures name the asked
  value.**
- **How:** same a-priori key-name discipline as the law grader; the extended
  matcher requires whole-token-run containment (acronym keys like DIS/KK/SA
  must not match inside ordinary words).
- **Command:** `python -m contract_drafting.raw_fidelity --extended`

### Pre-registration: `volunteered_flag` for the clean-prompt spot-check (dated 2026-07-04)

The planned `run_raw_clean` recording (arm A against the FIXED baseline schema —
no `*Raw` field, no OTHER sentinel — cases c01–c08 un-representable + c27–c29
controls, 4 models, new cassettes `gauntlet_cassette.<provider>.hard.cleanbase.json`)
will be graded by `contract_drafting/raw_fidelity.py::volunteered_flag`,
**committed and dated BEFORE recording** so the rule cannot be tuned to the
observed outputs. The rule, fixed now: a clean-prompt arm-A fill counts as
volunteering a flag iff (a) the asked jurisdiction (any committed
`_KEY_NAMES` form, normalized containment) appears anywhere in the fill OUTSIDE
the chosen `governingLaw` value itself, or (b) a caveat marker — `not available`
| `closest` | `cannot` | `unsupported` | `note` (whole words,
case-insensitive) — appears in any string value. Either counts: the clean-prompt
question is whether ANY signal is volunteered, not whether it lands in a typed
channel (by construction it cannot). Pinned by
`tests/test_mined_findings.py::test_volunteered_flag_*`. Results will be
reported inside the leak-disclosure section only, never pooled into existing
denominators.

### `raw_clean_flags.json` + the four cleanbase cassettes (recorded 2026-07-04)

- **Condition:** `raw-clean` — arm A (free-text fill, no hatch) against the
  **v2-clean** baseline schema (deep-stripped `*Raw` fields and abstain
  sentinels; no hatch vocabulary anywhere in the prompt), the 9 governing-law
  hard-suite cases: c01–c04, c06, c08 un-representable + c27–c29 supported-law
  controls. Own cassette namespace `gauntlet_cassette.<tag>.hard.cleanbase.json`
  (tags: openai, deepseek, deepseek-flash, anthropic); the committed 2026-06
  v1-leaky cassettes are untouched and cannot key-match these prompts.
- **Recording:** 2026-07-04, LIVE, 36 calls total (9 cases × 4 models, one text
  call per case). Models/providers: gpt-5.5 (OpenAI), deepseek-v4-pro and
  deepseek-v4-flash (DeepSeek), claude-sonnet-4-6 (Anthropic). Provider-default
  sampling (no temperature/top_p set anywhere in the call path); max-tokens
  inherited from the record callers: 8192 (`eval_providers._EVAL_MAX_TOKENS`,
  OpenAI/DeepSeek) and 4096 (`llm.MAX_TOKENS_LLM`, the Anthropic production
  `LiveCaller` path). Record command:
  `python -m contract_drafting.gauntlet --raw-clean --record --suite hard
  --provider <p> --model <m>`.
- **Grading:** the **pre-registered** `volunteered_flag` rule (dated 2026-07-04,
  commit `ad538c5`, committed BEFORE recording — see the pre-registration
  section above), applied exactly as committed by
  `contract_drafting/raw_fidelity.py::compute_raw_clean`. Two literal-application
  notes, recorded in the artifact: (1) `_KEY_NAMES` has no entries for the
  controls, so clause (a) is not literally evaluable there — controls are graded
  by clause (b) alone; (2) the c03/c04 instructions' own party names embed the
  asked jurisdiction ("… DIFC Limited", "… (Macau) Limited"), so clause (a)
  fires mechanically when a model copies the party name — the literal verdict
  stands, with a post-hoc `flag_via_party_name_only` annotation saying where
  each flag surfaced.
- **Headline (volunteered flags /6 un-representable; false flags /3 controls):**
  gpt-5.5 **3/6, 0/3**; deepseek-v4-pro **2/6, 0/3**; deepseek-v4-flash
  **2/6, 0/3**; claude-sonnet-4-6 **2/6, 0/3**. 8 of the 9 flags are the
  mechanical c03/c04 party-name copies; the single non-mechanical flag is
  gpt-5.5's volunteered `governingLawText: "laws of Scotland"` side key on c02.
  governingLaw fill behavior on the 6 un-representable cases (wrong_sub/omit):
  gpt-5.5 3/3, deepseek-v4-pro 5/1, deepseek-v4-flash 3/3, claude-sonnet-4-6
  6/0. Not pooled into any existing denominator.
- **Reproduce:** `python -m contract_drafting.raw_fidelity --raw-clean`
  (offline replay; byte-identical regeneration guarded by
  `tests/test_raw_clean_replay.py`).
