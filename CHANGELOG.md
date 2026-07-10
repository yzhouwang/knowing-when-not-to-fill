# Changelog

## [1.0.1] — 2026-07-11

Documentation and licensing patch — no code, cassette, or result changes.

- Cite the paper by its final title: *"Knowing When Not to Fill: Contract
  Drafting on the Accord Standard"* (EMNLP 2026 System Demonstrations).
- Re-anchor the Table 1 reproduction docs to the paper's headline metric:
  `silent-wrong` /6 over the six un-representable governing-law asks
  (`c01`-`c04`, `c06`, `c08`); the numeric/intent cases and the 0/3
  supported-law controls are reported separately, never pooled (previously
  described as /9).
- Publish the frozen pre-registration:
  [`PREREGISTRATION_cross-field.md`](PREREGISTRATION_cross-field.md) — the
  cross-field decision rule and dated amendments the paper cites.
- Make the cassette license boundary explicit: Apache-2.0 covers the code,
  docs, and hand-authored fixtures; rights in the recorded provider outputs
  are scoped by the redistribution note in
  [`data/eval/DATACARD.md`](data/eval/DATACARD.md).
- Disclose live-mode write artifacts in the bring-your-own-key docs (audit
  rows in `data/contract_drafting.db`, rendered drafts, `--record`
  cassettes); key material is never written anywhere.

## [1.0.0] — 2026-07-09

First public release.

A typed-abstention guardrail for constrained contract drafting on the open
[Accord Project](https://accordproject.org) / Concerto standard. One Concerto
`.cto` model drives JSON-Schema validation, provider-native constrained
generation (Anthropic tool `input_schema`, OpenAI strict `json_schema`), and the
typed-abstention policy; the render path is a deterministic, LLM-free
Cicero/Mustache pass.

### Guardrail
- An in-schema `OTHER` sentinel, a free-text raw-capture field, an abstain
  instruction, and a prompt-independent intent-consistency gate turn silent,
  valid-but-wrong substitutions on un-representable requests into honest, typed
  abstentions.
- Six Concerto-typed contract templates (NDA, consulting, intermediary,
  partnership, strategic-cooperation, joint-venture). The JSON Schema, TypeScript
  types, and the abstain policy are generated from the `.cto` models via
  `concerto-codegen`.

### The Gauntlet (evaluation)
- A record/replay benchmark — four arms × four models (gpt-5.5, deepseek-v4-pro,
  deepseek-v4-flash, claude-sonnet-4-6) — that replays entirely offline from
  committed cassettes and fails closed on a cache miss. The full test suite runs
  with no API key.

Backs the EMNLP 2026 System Demonstrations paper *"Knowing When Not to Fill: A
Typed-Abstention Guardrail for High-Stakes Structured Generation on the Accord
Contract Standard."*
