# Changelog

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
