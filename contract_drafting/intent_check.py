"""
intent_check.py -- the intent-consistency gate (PR1 of the silent-substitution hardening).

The drafting pipeline can produce a WELL-TYPED but WRONG contract. When a user asks
for a jurisdiction the schema cannot represent (e.g. "the laws of Ontario"), an LLM
filling the field under an enum constraint silently substitutes a valid-but-wrong
value, or omits it and lets the .cto default (Washington) render. The deterministic +
structured-request paths already fail closed (validate_template_data rejects
out-of-enum), but a value DERIVED from free text under a constraint escapes that net.

This gate compares the jurisdiction the instruction ASKED for against the value the
pipeline FILLED, resolving representability against the schema governingLaw enum.
Extraction is LLM-AUTHORITATIVE when the LLM is available (the only reliable extractor
for ambiguous phrasing) and consulted unconditionally -- no regex pre-gate stands in
front of it; a deterministic regex path is the offline/no-LLM fallback, scope-limited
to its own vocabulary (_MIGHT_NAME_LAW).

Measured by MANUAL analysis of the recorded Gauntlet hard-suite (a committed,
replayable harness + a catch-rate regression test are a tracked follow-up, NOT yet in
place): on the 6 governing-law cases (Ontario/Scotland/DIFC/Macao/US) the gate flags
the silent substitution in every model+arm, with 0 false flags on faithful
(correctly-filled) drafts on the LLM path. SCOPE + caveats: (1) "catch" =
flagged fail-closed for human review, NOT auto-corrected; (2) coverage is the
governing-law (enum-constrained) field only — a numeric/intent case like survivalYears
"life of the trade secret" is out of scope and relies on validate_semantics bounds,
which miss in-range values; (3) the online path detects un-representability directly,
while the offline path conservatively fails closed on unverifiable governing-law
phrasing (so offline over-flags some correct-but-unextractable mentions for review).

  instruction ──extract (LLM authoritative; regex offline)──► asked jurisdiction(s)
       │                                                          │
       │                       resolve to schema governingLaw enum
       ▼                                                          ▼
  filled governingLaw ──resolve──► filled_id   ── compare ──► warnings[]
       (un-representable ask, filled != asked, or >1 asked  =>  silent substitution)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from contract_drafting import jurisdiction_map
from contract_drafting import schema_validator

log = logging.getLogger(__name__)

# Patterns that name a governing-law jurisdiction in a drafting instruction. The
# spike used only the first two and missed "under Washington law"; this set adds
# the "under X law" / "X law governs" / "governing law: X" forms.
_JURISDICTION_PATTERNS = [
    r"[Gg]overning law\s*(?:=|:|is|shall be)\s*([A-Za-z][\w .,()&'/-]+?)(?:[,.;\n]| venue| with | and (?=[a-z])|$)",
    r"governed by(?: and construed in accordance with)? the laws? of (?:the )?([A-Za-z][\w .,()&'-]+?)(?:[,.;\n]| with| having| for | venue| and (?=[a-z])|$)",
    r"\b(?i:under|apply|applying|use|using|per|subject to|pursuant to|in accordance with) (?:the )?([A-Za-z][\w .,()&'-]+?) law\b",
    r"the laws? of (?:the )?([A-Za-z][\w .,()&'-]+?)(?:[,.;\n]| with| having| venue| and (?=[a-z])|$)",
    r"governed by ([A-Za-z][\w .,()&'-]+?) law\b",
    r"\b([A-Za-z][\w .,()&'-]+?) law (?:governs|shall govern|applies|shall apply|will apply|should apply|controls)\b",
]

_STOPWORDS = {"the", "this", "that", "such", "applicable", "local", "relevant", "state"}

# Legal prefixes stripped when canonicalizing a raw candidate ("state of New York"
# -> "New York", "the laws of Delaware" -> "Delaware") before mapping to an enum
# identifier. Deliberately strips ONLY universal qualifiers + "state of" (every US
# state can be "State of X"). EXCLUDES "republic of"/"province of"/"commonwealth of":
# those are type-specific and stripping them fail-opens a distinct/un-representable ask
# onto a state ("Republic of Texas"/"Commonwealth of Texas" -> Texas, "Province of
# Ontario" -> Ontario). The legit enum countries + the 4 real commonwealths exact-match
# (via _ABBREV); anything else fails closed.
_PREFIX_RE = re.compile(r"^(the |state of |laws? of (the )?)+", re.I)


class IntentSubstitutionError(RuntimeError):
    """Raised (fail-closed) when a drafted value does not faithfully match the
    jurisdiction the instruction asked for -- a silent substitution."""


def _jurisdiction_spans(instruction: str) -> list[tuple[str, int]]:
    """[(phrase, capture_end_index)] for each governing-law capture, via finditer so
    the fast-path can inspect the text immediately AFTER the actual capture -- not a
    coincidental earlier occurrence of the same words (instruction.find was wrong)."""
    if not instruction:
        return []
    out: list[tuple[str, int]] = []
    for pat in _JURISDICTION_PATTERNS:
        for m in re.finditer(pat, instruction):
            phrase = (m.group(1) or "").strip().rstrip(",.; ")
            if phrase and phrase.lower() not in _STOPWORDS:
                out.append((phrase, m.end(1)))
    return out


def extract_jurisdiction(instruction: str) -> list[str]:
    """Best-effort, deterministic (no LLM): pull candidate governing-law
    jurisdiction phrases from an instruction. Returns [] if none found."""
    seen: set[str] = set()
    out: list[str] = []
    for phrase, _ in _jurisdiction_spans(instruction):
        if phrase.lower() not in seen:
            seen.add(phrase.lower())
            out.append(phrase)
    return out


# LOOSE scope limiter for the DETERMINISTIC (offline / LLM-unavailable) path ONLY:
# does the instruction use vocabulary the regex extractor can see at all? The LLM path
# does NOT consult it -- it used to pre-gate the whole function, which fail-opened on
# out-of-vocabulary phrasings ("Apply Ontario legislation.") by returning [] before the
# LLM was ever asked. A false fire here is harmless (at worst a conservative
# fail-closed review flag via _GOVERNING_LAW_SIGNAL); a miss leaves an offline residual
# band (e.g. "under Ontario rules") documented in tests/test_intent_gate_failclosed.py.
_MIGHT_NAME_LAW = re.compile(
    r"govern|laws?\s+of|jurisdiction|\blaw\b|legislation|statutes?\b|"
    r"legal\s+system|construed",
    re.I,
)

# STRONG signal: used ONLY to decide whether to FAIL CLOSED when the LLM is
# unavailable. Deliberately excludes bare "jurisdiction"/"law" (forum/venue language,
# e.g. "exclusive jurisdiction in King County courts") so a venue-only instruction is
# not blocked when there's no LLM to disambiguate. Includes governing-law vocabulary
# the span patterns cannot parse (legislation / statutes of X / legal system /
# construed) so evasive phrasings fail closed offline instead of fail-open; "statute of
# limitations" is excluded (a claims clause, not a choice of law).
_GOVERNING_LAW_SIGNAL = re.compile(
    r"govern(?:ed|ing)\s+(?:by|law)|governing\s+law|laws?\s+of\b|"
    r"\blaw\s+(?:applies|governs|shall|will|should|controls)|"
    r"\blegislation\b|statutes?\s+of\s+(?!limitations?\b)|legal\s+system|\bconstrued\b",
    re.I,
)

# A "," or "and" immediately after a captured jurisdiction, followed by a capitalized
# word, may be an un-captured COMPOUND ("...laws of New York, Ontario") the regex
# truncated -- so the deterministic fast-path must not trust it (defer to the LLM).
_COMPOUND_TAIL = re.compile(r"\s*(?:,|and)\s+[A-Z]")

# The abstain sentinel (PR 2): the model emits governingLaw=OTHER (+ governingLawRaw)
# when the requested law is not representable, instead of silently substituting. It is
# a valid enum value but NOT a resolvable jurisdiction, so it is excluded from
# representability resolution and handled explicitly as an (honest) abstention.
_ABSTAIN = "OTHER"


# Short forms / abbreviations / common names -> their official enum DISPLAY name. This is
# an EXPLICIT allowlist, NOT an open-ended token heuristic. The heuristic (token subset,
# then equality) repeatedly FAIL-OPENED by conflating DISTINCT jurisdictions that share a
# core name after dropping "generic" words: Mexico->New_Mexico (subset), and under equality
# Democratic-People's-Republic-of-Korea -> Republic_of_Korea, Republic-of-China(Taiwan) ->
# People's_Republic_of_China. An explicit allowlist fails CLOSED on anything unlisted
# (None -> human review), so a missing entry is a false-positive (safe), never a silent sub.
_ABBREV = {
    # abbreviations
    "dc": "District of Columbia", "washington dc": "District of Columbia",
    "uae": "United Arab Emirates", "prc": "People's Republic of China",
    "rok": "Republic of Korea", "ksa": "Kingdom of Saudi Arabia",
    # common country short names -> official enum display (US states resolve by exact match)
    "england": "England and Wales",
    "singapore": "Republic of Singapore",
    "china": "People's Republic of China",
    "india": "Republic of India",
    "indonesia": "Republic of Indonesia",
    "kenya": "Republic of Kenya",
    "korea": "Republic of Korea", "south korea": "Republic of Korea",
    "saudi arabia": "Kingdom of Saudi Arabia",
    "south africa": "Republic of South Africa",
    "nigeria": "Federal Republic of Nigeria",
    "hong kong": "Hong Kong SAR",
    # the 4 US states that are officially "Commonwealth of X" (others are not commonwealths)
    "commonwealth of massachusetts": "Massachusetts",
    "commonwealth of kentucky": "Kentucky",
    "commonwealth of pennsylvania": "Pennsylvania",
    "commonwealth of virginia": "Virginia",
}


def _dealias(phrase: str) -> str:
    """Expand a known abbreviation ('D.C.', 'UAE') to its official display name; else
    return the phrase unchanged."""
    key = re.sub(r"[^a-z0-9]+", " ", (phrase or "").lower().replace(".", "")).strip()
    return _ABBREV.get(key, phrase)


# Enum display names that are ALSO sovereign countries. A BARE name resolves to the
# (representable) US state, but a COUNTRY-qualified form ("Republic of Georgia",
# "country of Georgia") is the sovereign nation -- un-representable, must NOT collapse
# onto the state. NB "State of Georgia" is the US state (handled by NOT matching here).
_STATE_COUNTRY_HOMONYMS = ("georgia",)
_HOMONYM_COUNTRY = re.compile(
    r"\b(?:republic|country|nation|sovereign(?:\s+(?:state|nation))?)\s+of\s+(?:the\s+)?(" +
    "|".join(_STATE_COUNTRY_HOMONYMS) + r")\b", re.I)


def _is_homonym_country(phrase: str) -> bool:
    """True if the phrase is a COUNTRY-qualified form of a US-state/country homonym
    (e.g. 'Republic of Georgia') -- the sovereign nation, not the enum's US state."""
    return bool(_HOMONYM_COUNTRY.search(phrase or ""))


def _resolve_clean(phrase: str, template_name: str) -> Optional[str]:
    """Resolve a phrase to a value in the template's governingLaw enum, or None.
    Source of truth is the SCHEMA enum. Resolution is EXACT/case-insensitive (with the
    jurisdiction_map identifier<->display + prefix-strip normalization) plus the explicit
    _ABBREV allowlist for common short forms. NO open-ended token heuristic -- it fail-
    opened by conflating distinct jurisdictions sharing a core name. None for anything
    else (fail closed -> human review)."""
    enum = schema_validator.governing_law_enum(template_name) - {_ABSTAIN}
    if not enum:
        return None
    # A country-qualified homonym ("Republic of Georgia") is the sovereign nation, NOT
    # the US-state enum value -- reject it before resolution so prefix-stripping
    # ("republic of" -> "Georgia") can't collapse it onto the state (a fail-open).
    if _is_homonym_country(phrase):
        return None
    # Expand a known short form ONCE on the original phrase (never on the prefix-stripped
    # form -- that would let "Republic of China" strip to "China" then alias to the PRC).
    phrase = _dealias(phrase)
    for form in (phrase.strip().rstrip(",. "),
                 _PREFIX_RE.sub("", phrase).strip().rstrip(",. ")):
        if not form:
            continue
        # nda path: normalize a display name to its identifier, then check membership.
        ident = jurisdiction_map.resolve_to_identifier(form, template_name=template_name)
        if ident and ident in enum:
            return ident
        # string-enum path: case-insensitive match against the enum values directly.
        for ev in enum:
            if ev.lower() == form.lower():
                return ev
    return None


def _llm_extract_jurisdictions(instruction: str, *, provider: str, model: Optional[str]) -> tuple[bool, list[str]]:
    """Authoritative extraction of ALL governing-law jurisdictions (the law that
    GOVERNS, not venue/forum/court). Returns (available, names):
    (True, ["New York", "Ontario"]) ran and found those; (True, []) ran and found
    none; (False, []) could NOT run (no provider/key/error) -> caller fails closed."""
    try:
        from contract_drafting.llm import call_llm
        raw = call_llm(
            question=("List EVERY governing-law jurisdiction this contract instruction specifies "
                      "-- the law that GOVERNS the agreement, NOT venue/forum/court/county. "
                      "Put ONE jurisdiction PER LINE, keeping each name intact (e.g. "
                      "'Washington, D.C.' on a single line). For a name shared by a US state "
                      "and a sovereign country (e.g. Georgia), DISAMBIGUATE: write the country "
                      "as 'Country of <name>' and the US state as just '<name>'. Or the single "
                      "word NONE."),
            context=instruction,
            provider=provider,
            model=model,
            system_prompt="You extract governing-law jurisdictions. Output one name per line, or NONE.",
        )
        v = (raw or "").strip().strip('".')
        if not v or v.upper() == "NONE":
            return (True, [])
        # One per line (not comma-split: an intra-name comma like "Washington, D.C."
        # must survive as a single jurisdiction).
        names = [ln.strip(" \t-*•.").strip() for ln in v.splitlines()]
        return (True, [n for n in names if n and n.upper() != "NONE"])
    except Exception as e:  # noqa: BLE001 -- couldn't run; caller fails closed
        log.warning("LLM jurisdiction extraction unavailable: %s", e)
        return (False, [])


def _fast_path(spans: list[tuple[str, int]], instruction: str, template_name: str) -> Optional[set[str]]:
    """Deterministic best-effort: if the governing-law mention is unambiguous -- every
    captured phrase resolves cleanly AND none is followed by a compound continuation
    (which the regex may have truncated) -- return the resolved ids. Return None for
    anything the regex cannot be trusted on, so the caller uses the LLM or fails
    closed. Avoids an LLM call on the common 'governed by the laws of <state>' case."""
    if not spans:
        return None
    # If there are MORE governing-law mentions than captured spans, a second clause may
    # be uncaptured ("Washington law governs, and Ontario law also applies" -- the
    # 'also' defeats the verb pattern) -> don't trust the fast-path; defer to the LLM.
    if len(re.findall(r"\blaws?\b", instruction or "", re.I)) > len(spans):
        return None
    ids: set[str] = set()
    for phrase, end in spans:
        ident = _resolve_clean(phrase, template_name)
        if not ident or _COMPOUND_TAIL.match(instruction[end:]):
            return None
        ids.add(ident)
    return ids


def verify_intent(
    instruction: str,
    fields: dict,
    *,
    template_name: str = "nda-mutual",
    allow_llm_fallback: bool = True,
    provider: str = "anthropic",
    model: Optional[str] = None,
) -> list[str]:
    """Compare the governing law(s) the instruction asked for against the filled
    governingLaw. Returns warning strings (empty = consistent).

    A warning means the contract does NOT faithfully represent the ask: an
    un-representable jurisdiction (silently substituted/defaulted), a representable one
    the fill didn't match, or MULTIPLE governing laws asked that one field can't hold.

    The LLM is AUTHORITATIVE whenever available -- it is the only reliable extractor
    for ambiguous phrasing (compound jurisdictions, venue vs governing law, casual
    casing), the user opted into it, and with allow_llm_fallback=True it is consulted
    UNCONDITIONALLY on any non-empty instruction: no regex pre-gate stands in front of
    it. (Two prior fail-opens motivate this: a partial regex capture kept masking a
    second jurisdiction, and the _MIGHT_NAME_LAW pre-gate returned [] on phrasings
    outside its vocabulary -- "Apply Ontario legislation." -- before the LLM was ever
    asked.) The deterministic regex path runs ONLY when the LLM is unavailable or
    disabled (offline best-effort), scoped by the _MIGHT_NAME_LAW vocabulary pre-gate:
    clean single jurisdictions resolve via the fast-path, and it fails closed if a
    governing-law PATTERN matched but didn't resolve, or on a STRONG governing-law
    signal -- never on bare forum/venue language. Offline coverage is therefore
    bounded by the regex vocabulary: an out-of-vocabulary ask ("under Ontario rules")
    passes unverified offline -- the documented residual band. Callers producing real
    contracts treat a non-empty result as fail-closed (guard_intent).
    """
    # The gate only applies where governingLaw is ENUM-constrained -- that's where a
    # constrained fill can silently substitute an in-enum value. If governingLaw is a
    # free string (no enum in the schema), there is nothing to substitute against, so
    # the gate is a no-op rather than false-blocking. NB: uses the SCHEMA enum, not the
    # jurisdictions.map -- the non-nda templates have an enum but no map.
    if not schema_validator.governing_law_enum(template_name):
        return []

    if not (instruction or "").strip():
        return []  # no instruction -> nothing asked, nothing to verify

    filled = fields.get("governingLaw") if isinstance(fields, dict) else None
    asked_ids: set[str] = set()
    unrep_names: list[str] = []

    spans = _jurisdiction_spans(instruction)
    # A governing-law pattern matched (spans) OR a strong signal => fail closed when we
    # cannot extract; bare forum/venue ("jurisdiction in King County") does neither.
    _should_failsafe = bool(spans) or bool(_GOVERNING_LAW_SIGNAL.search(instruction or ""))

    # The LLM is AUTHORITATIVE whenever available -- consulted UNCONDITIONALLY, never
    # short-circuited by a regex. A partial regex capture repeatedly masked a second
    # jurisdiction introduced by glue the regex can't model (', Ontario', 'Ontario law
    # also applies'), and the old _MIGHT_NAME_LAW pre-gate returned [] on evasive
    # phrasings outside its vocabulary ("Apply Ontario legislation.") before the LLM
    # ran at all -- a fail-open. The regexes now scope only the deterministic path.
    available, names = (_llm_extract_jurisdictions(instruction, provider=provider, model=model)
                        if allow_llm_fallback else (False, []))
    if available:
        for n in names:
            ident = _resolve_clean(n, template_name)
            (asked_ids.add(ident) if ident else unrep_names.append(n))
    else:
        # deterministic-only (LLM disabled or unavailable) -- scope-limited by the
        # _MIGHT_NAME_LAW vocabulary, then best-effort fast-path, else fail closed.
        if not (spans or _MIGHT_NAME_LAW.search(instruction or "")):
            return []  # names no governing law the regex path can see -> nothing to verify
        fast = _fast_path(spans, instruction, template_name)
        if fast is None:
            if not _should_failsafe:
                return []
            return [_ABSTAIN_UNVERIFIED] if filled == _ABSTAIN else [_FAILSAFE]
        asked_ids = fast

    # PR2 abstain hatch: the model emitted OTHER (an honest abstention) rather than
    # silently substituting. This is the CORRECT outcome when the ask was
    # un-representable (the hatch worked); it is only wrong if the model abstained on a
    # representable jurisdiction it should have filled.
    if filled == _ABSTAIN:
        if asked_ids:
            return [f"model abstained (governingLaw=OTHER) but the requested jurisdiction "
                    f"{sorted(asked_ids)} IS supported -- fill it instead of abstaining."]
        return []  # un-representable (or no) ask -> abstention is the right outcome

    if unrep_names:
        return [f"requested governing law {unrep_names[0]!r} is not a supported jurisdiction; "
                f"drafted value {filled!r} is a silent substitution -- human review required."]
    if not asked_ids:
        return []  # the instruction named no governing-law jurisdiction
    if len(asked_ids) > 1:
        return [f"instruction names multiple governing laws {sorted(asked_ids)} but the contract "
                f"has a single governingLaw field ({filled!r}) -- human review required."]
    filled_id = _resolve_clean(str(filled), template_name) if filled else None
    if filled_id not in asked_ids:
        return [f"requested governing law {sorted(asked_ids)} but drafted {filled_id!r}; "
                f"value does not match the instruction -- human review required."]
    return []


_FAILSAFE = ("instruction names a governing law the gate could not extract or verify "
             "(LLM extractor unavailable) -- human review required.")

# Offline, when the model abstained (OTHER) but the deterministic path cannot PROVE the
# ask was un-representable (e.g. a compound 'Delaware, New York'), do NOT accept the
# abstention as correct -- it may be a wrong abstention on a representable law. Fail closed.
_ABSTAIN_UNVERIFIED = ("model abstained (governingLaw=OTHER) but the requested governing law "
                       "could not be verified offline -- human review required.")


def guard_intent(instruction: str, fields: dict, *, allow_substitution: bool = False, **kw) -> list[str]:
    """verify_intent + fail-closed: raise IntentSubstitutionError on any warning
    unless allow_substitution=True (explicit override). Returns the warnings either
    way so report-only callers (demo/eval) can pass allow_substitution=True."""
    warnings = verify_intent(instruction, fields, **kw)
    if warnings and not allow_substitution:
        raise IntentSubstitutionError("; ".join(warnings))
    return warnings
