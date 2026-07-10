# Pre-registration: cross-field replication of the typed-abstention dissociation

**Status: FROZEN before any model recording.** Authored 2026-06-09, on branch
`feat/cross-field-replication`, before any new cassette is recorded. The point of
committing this *first* is to make the experiment falsifiable and non-cherry-picked:
the fields, enums, sample sizes, primary metric, decision rule, and the criteria
that would *falsify* the general claim are all fixed here, in git, before we look at
a single new model response. Any deviation must be recorded as an amendment below
with a date and reason.

## 1. Claim under test

The headline paper shows, on ONE field (`governingLaw`, the 64-member `Jurisdiction`
enum), that a constrained decoder silently substitutes a wrong-but-valid value for an
un-representable ask unless a typed `OTHER` sentinel is present, and that the **typed
slot, not the prompt instruction, carries the abstention** (ablation: slot-only abstains
4–5/6; instruction-only 0/6 across 4 models).

**General claim to be tested:** this *slot > prompt* dissociation is a property of
**closed categorical types with a representability gap in general**, not an artifact of
the governing-law field. We test it by replicating the same ablation on additional
closed categorical fields of a **different ontological kind** from geography.

## 2. Fields (FROZEN)

Two NEW non-geographic fields are the **headline replication** (the field-generality
claim rests on these two + the original `governingLaw` = three distinct fields, of which
two are new and non-geographic):

| # | Field | Template | Ontological kind | Gap mechanism |
|---|---|---|---|---|
| F1 | `entityType` (disclosing + receiving) | nda-mutual | organizational **form** | curated common-law taxonomy; long tail of foreign legal forms |
| F2 | `disputeForum` | nda-mutual | arbitral **institution** | finite roster of named institutions; ad-hoc / court / unlisted fall outside |

One additional field is an **axis control, NOT part of the field-generality count**:

| # | Field | Template | What it isolates |
|---|---|---|---|
| A1 | `governingLaw` | joint-venture | SAME field+type, DIFFERENT template + prompt (the "one template / one prompt" rebuttal) |

**Independence rule (anti-"same-thing-twice"):** F1 and F2 must be genuinely
non-geographic. We explicitly EXCLUDE any place/jurisdiction-flavoured enum
(`targetCountry`, `signingPlace`, `registrationPlace`, `operatingRegion`, or re-using
the `Jurisdiction` enum on another field) from the field-generality claim — those share
geography's axis and a reviewer would (correctly) reject them as the same instance.
A1 is reported separately and is **never** counted toward "general across fields"; it
answers a different question (template/prompt invariance of the *same* field).

## 3. Enum design (FROZEN before probing)

Each field's representable member set, its `OTHER`-style sentinel, and its `<field>Raw`
companion are authored and committed (in `model.cto` + the regenerated `schema.json`
/ `abstain-policy.json`) **before** any probe is written, to prevent retrofitting the
enum to make a chosen probe fall outside it.

- **F1 `EntityType`** (~11 members): `corporation`, `limited_liability_company`,
  `general_partnership`, `limited_partnership`, `limited_liability_partnership`,
  `sole_proprietorship`, `professional_corporation`, `nonprofit_corporation`, `trust`,
  `joint_venture`, `individual`; sentinel `OTHER_ENTITY`; companions
  `disclosingEntityTypeRaw`, `receivingEntityTypeRaw`. Scope = common-law / US-default
  forms; the gap is **foreign and civil-law legal forms** (GmbH, KK, Pte Ltd, plc, SA,
  Anstalt, statutory body / sovereign).
- **F2 `DisputeForum`** (~9 members): `SIAC`, `ICC`, `LCIA`, `HKIAC`, `AAA_ICDR`,
  `CIETAC`, `DIAC`, `SCC`, `JAMS`; sentinel `OTHER_FORUM`; companion `disputeForumRaw`.
  Scope = major named arbitral institutions; the gap is **ad-hoc/UNCITRAL, national-court
  litigation, and unlisted institutions** (e.g. KCAB, VIAC, Stockholm-but-not-SCC).
- **A1 `governingLaw` (JV)** re-uses the existing 64-member `Jurisdiction` enum +
  `OTHER` sentinel verbatim (no new enum); gap = jurisdictions genuinely absent from the
  64 (Cayman, DIFC, Bermuda, Liechtenstein, Channel Islands).

Sentinel/companion naming follows the `governingLaw`/`governingLawRaw`/`OTHER` pattern so
the same `@Abstainable` codegen (v0.15) produces each policy with no bespoke logic.

## 4. Sample sizes (FROZEN)

Per field: **≥12 un-representable probes**, spanning **≥3 distinct gap mechanisms**
(≥4 probes each), and **≥4 supported-value controls**. Models: the same four —
`anthropic/claude-sonnet-4-6`, `openai/gpt-5.5` (strict `json_schema`),
`deepseek-v4-pro`, `deepseek-v4-flash` (soft `json_object`). This roughly doubles the
n=6/cell power of the original governing-law cells; n=6 is too thin (a single
mis-adjudication flips per-model significance).

## 5. Arms and the ablation (unchanged mechanism, now field-parametric)

For each field X under test, over X's un-representable probes:
- **`other_only`** — X's enum carries its sentinel (slot present); system prompt = base
  `_SYSTEM` (NO abstain instruction).
- **`instr_only`** — X's sentinel stripped from its enum (slot absent); system prompt =
  base + X's abstain instruction.
- **`both` (full hatch)** — slot present AND X's abstain instruction.

The per-field abstain instruction is single-sourced from the `.cto` `@Abstainable`
decorator via the (now multi-field) `abstain-policy.json`. The `governingLaw` instruction
is held **byte-identical** to the shipped string so the existing frozen-string test stays
valid and the governing-law result is comparable.

## 6. Primary metric + decision rule (FROZEN)

For each (field × model) cell, over that field's un-representable probes, compute:

> **Δ = abstain-rate(`other_only`) − abstain-rate(`instr_only`)**

**Replication of the dissociation is declared iff:** across the **8 headline cells**
(F1, F2 × 4 models), **Δ ≥ 0.40 with `instr_only` abstain-rate ≤ 0.34 in ≥ 7 of 8 cells**.

Reported alongside as the full headline table: the same Δ for `governingLaw` (the anchor,
3 fields × 4 models = 12 cells). A1 (JV governingLaw) is a separate 4-cell
template/prompt-invariance panel.

## 7. Secondary / safety metrics (FROZEN)

- **Over-abstention guard:** on each field's supported-value controls, the `both`-arm
  abstain-rate must stay **≤ 0.25 per cell**. A slot that makes the model abstain on
  *everything* (including supported values) is a useless product, not a result — a
  high over-abstention rate **invalidates** that field's slot, even if Δ is large.
- **Headline before/after:** baseline silent-wrong (`constrained`, no hatch) vs hatch
  `abstained` (`constrained+hatch`) per field, to show the same flip the NDA result shows.

## 8. Analysis plan (FROZEN)

- Report **per-cell rates** (no hidden pooling).
- Pooled estimate via a **clustered / mixed-effects** model with **field and model as
  grouping factors** — NOT a naive pooled n = (3 fields × 4 models × 12 probes) = 144,
  which would treat reused probes and within-model correlation as independent trials.
- Bootstrap confidence intervals on Δ, clustered by probe.
- `omit` (model leaves the field empty → renders the `.cto` default) is folded into
  silent-wrong, exactly as the shipped harness already does for `governingLaw`.

## 9. Falsification (stated UP FRONT)

The general claim is **falsified** (and we will say so) if any of:
1. `instr_only` abstain-rate **≥ 0.5** on any headline field's probes (the instruction
   alone works → abstention is NOT type-bound for that field), **or**
2. `other_only` does **not** beat `instr_only` by **≥ 0.40** on **≥ 2 of the 3** fields
   (governingLaw + F1 + F2), **or**
3. the over-abstention guard (§7) is breached on a headline field (the slot is
   indiscriminate, not a targeted abstention).

## 10. Pre-committed outcomes — all publishable

To remove any incentive to p-hack toward "positive," all three are committed as
reportable *before* recording:

- **FULL replication** (decision rule §6 met, falsifiers §9 absent): the principle
  "abstention capacity belongs in the type, not the prompt" upgrades from a one-field
  observation to a demonstrated property of closed categorical types. *Strongest paper.*
- **PARTIAL / MODERATED** (slot wins on one field but instruction also works on another):
  reported as a **scoped** finding — the dissociation holds for fields with strong model
  priors (geography, legal form) but weakens where the model lacks a confident prior. The
  scoping is itself a contribution and is reported honestly, not hidden.
- **NEGATIVE** (dissociation does not replicate): reported as "the governing-law result is
  field-specific," which **refines** the principle rather than killing the paper. The
  headline NDA result and the system stand on their own.

## 11. Honesty traps being guarded against (from the red-team)

1. **Retrofitting the enum to the probe** — enums frozen in §3 before probes (§4).
2. **Testing a free-String field as-is** — every field is first converted to a real
   `@Abstainable` enum; we never "ablate" a field that was never typed.
3. **Cherry-picking the field after results** — the exact headline fields are fixed in
   §2; all recorded fields are reported.
4. **Passing an in-axis replication off as independent** — A1 (JV governingLaw) is
   reported separately and never counted toward field-generality (§2).
5. **Naive pooled n** — clustered/mixed-effects only (§8).
6. **Model-version drift** — `governingLaw` is **re-recorded in the same pass** as the
   new fields (the new enums change the NDA schema hash, so all NDA cassettes are
   re-recorded together on the same model versions); we never compare fresh new-field
   cassettes against stale governing-law cassettes.
7. **Scorer false-negatives from surface variants** (esp. `disputeForum`: "SIAC" vs
   "Singapore International Arbitration Centre") — each field gets a normalizer in
   `_values_match` before any cell is scored; the normalizer is committed before recording.
8. **Hidden over-abstention** — §7 guard is a hard invalidator, not a footnote.

## 12. Amendments

**Amendment 1 (2026-06-12) — re-recording variance on the governing-law ablation.**
After the NDA schema gained the cross-field `entityType`/`disputeForum` fields (commit
`01c627d`), the governing-law conditions were re-recorded in the same pass, exactly as
honesty-trap #6 (§11) requires — the new enums change the NDA schema hash, so stale
governing-law cassettes could not be compared against fresh cross-field ones. Between
the two recordings, the slot-only (`other_only`) `governingLaw` ablation cell for
gpt-5.5 moved **5/6 → 6/6**. The "4–5/6" stated in §1 describes the earlier recording;
the current committed results are **6/6, 5/6, 5/6, 5/6** (gpt-5.5, deepseek-v4-pro,
deepseek-v4-flash, claude-sonnet-4-6). This is recording-to-recording variance at n=6
under a schema-context change — a single probe flipping moves a cell by 0.17 — not a
new effect. It is disclosed in the paper, which reports the current committed numbers
(5–6/6). No frozen section (§2–§9) is altered by this; the §1 claim summary simply
predates the re-recording.

**Amendment 2 (2026-06-12) — the A1 axis control was not executed this cycle.**
The pre-registered A1 axis control (§2: `governingLaw` on the joint-venture template —
the "one template / one prompt" rebuttal) was NOT executed in this cycle: there are no
joint-venture cases in `hard_suite.json` and no JV cassette was recorded. It is
deferred to future work. A1 was never part of the field-generality count (§2's
independence rule), so the headline replication (F1, F2) is unaffected; the open
question A1 addresses — template/prompt invariance of the same field — remains open,
and the paper's limitations section discloses that the template/prompt axis is held
fixed.
