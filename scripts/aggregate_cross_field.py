#!/usr/bin/env python3
"""Aggregate the cross-field replication results (reproduces the paper's cross-field ablation).

Reads the committed gauntlet_results.<model>.hard.json reports and computes, per
(field x model), the pre-registered primary metric: the within-guardrail ablation
dissociation Delta = abstain-rate(slot-only) - abstain-rate(instruction-only), plus the
headline before/after (constrained silent-wrong -> hatch abstained) and the
over-abstention control rate. Applies the pre-registered decision rule.

Offline only -- reads committed JSON, no API.
"""
import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"
MODELS = [
    ("gpt-5.5", "gauntlet_results.openai.hard.json"),
    ("v4-pro", "gauntlet_results.deepseek.hard.json"),
    ("v4-flash", "gauntlet_results.deepseek-flash.hard.json"),
    ("sonnet-4.6", "gauntlet_results.anthropic.hard.json"),
]
# Headline replication fields (the 2 NEW non-geographic fields) + the governingLaw anchor.
HEADLINE_FIELDS = ["governingLaw", "disclosingEntityType", "receivingEntityType", "disputeForum"]
NEW_FIELDS = ["disclosingEntityType", "receivingEntityType", "disputeForum"]  # the cross-field claim

SUPPORT_CONTROLS = {"supported-law-control", "supported-entity-control", "supported-forum-control"}


def rate(n, d):
    return round(n / d, 3) if d else None


def load():
    out = {}
    for label, fn in MODELS:
        p = EVAL / fn
        if not p.exists():
            print(f"MISSING: {p}", file=sys.stderr)
            continue
        out[label] = json.loads(p.read_text())
    return out


def field_probes(report, field):
    """Un-representable probe cases for `field` (adjudicated, expected_correct NONE)."""
    return [c for c in report["cases"]
            if c["field"] == field and c["defect_class"] not in SUPPORT_CONTROLS
            and c["field"] != "(all)" and c.get("expected_correct") in (None, "", "NONE")]


def main():
    reports = load()
    if not reports:
        print("No results found -- run the recording first.")
        return

    print("# Cross-field replication: ablation Delta (slot-only minus instruction-only)\n")
    print("Per (field x model): slot-only abstain / instruction-only abstain over the field's")
    print("un-representable probes; Delta = slot - instr. Pre-reg decision rule: Delta>=0.40 with")
    print("instr<=0.34, holding in >=7/8 headline (entityType+disputeForum) cells.\n")
    header = "| field | " + " | ".join(lbl for lbl, _ in MODELS) + " |"
    print(header)
    print("|" + "---|" * (len(MODELS) + 1))

    # Display table (per sub-field, for transparency).
    for field in HEADLINE_FIELDS:
        row = [field]
        for lbl, _ in MODELS:
            rep = reports.get(lbl)
            ab = (rep or {}).get("ablation", {}).get(field) if rep else None
            if not ab:
                row.append("--")
                continue
            oo, io = ab["other_only"], ab["instr_only"]
            sr = rate(oo["abstained"], oo["n"])
            ir = rate(io["abstained"], io["n"])
            d = round(sr - ir, 3) if (sr is not None and ir is not None) else None
            row.append(f"slot {oo['abstained']}/{oo['n']} ({sr}) vs instr {io['abstained']}/{io['n']} ({ir}); d={d}")
        print("| " + " | ".join(str(x) for x in row) + " |")

    # Decision cells over the PRE-REGISTERED headline fields: entityType (disclosing+receiving
    # COMBINED as one field F1, per the frozen plan) and disputeForum (F2), x 4 models = 8 cells.
    HEADLINE_GROUPS = {"entityType": ["disclosingEntityType", "receivingEntityType"],
                       "disputeForum": ["disputeForum"]}
    cells = []  # (field, model, delta, instr_rate, slot_rate)
    for fname, subs in HEADLINE_GROUPS.items():
        for lbl, _ in MODELS:
            ab = (reports.get(lbl) or {}).get("ablation", {})
            slot_a = slot_n = instr_a = instr_n = 0
            ok_cell = True
            for sf in subs:
                e = ab.get(sf)
                if not e:
                    ok_cell = False
                    break
                slot_a += e["other_only"]["abstained"]; slot_n += e["other_only"]["n"]
                instr_a += e["instr_only"]["abstained"]; instr_n += e["instr_only"]["n"]
            if not ok_cell or slot_n == 0:
                continue
            sr = round(slot_a / slot_n, 3)
            ir = round(instr_a / instr_n, 3) if instr_n else 0.0
            cells.append((fname, lbl, round(sr - ir, 3), ir, sr))

    # Decision rule over the 8 pre-registered headline cells.
    # FIXED pre-registered denominator: 8 headline cells (entityType + disputeForum x 4 models),
    # FULL requires >= 7/8. Never scale the threshold to the number of cells present -- an
    # incomplete artifact set (a missing model report) must NOT be able to print FULL.
    EXPECTED_CELLS = 8
    FULL_THRESHOLD = 7
    print(f"\n# Pre-registered decision rule (headline = {EXPECTED_CELLS} cells: "
          f"entityType + disputeForum x 4 models; FULL requires >= {FULL_THRESHOLD}/{EXPECTED_CELLS})\n")
    ok = [(f, m, d, ir, sr) for (f, m, d, ir, sr) in cells if d >= 0.40 and ir <= 0.34]
    print(f"cells meeting (Delta>=0.40 AND instr<=0.34): {len(ok)} / {len(cells)} present "
          f"(of {EXPECTED_CELLS} expected)")
    if len(cells) != EXPECTED_CELLS:
        verdict = (f"INCOMPLETE -- only {len(cells)}/{EXPECTED_CELLS} headline cells present; "
                   f"cannot declare a verdict (re-record the missing model(s)).")
    elif len(ok) >= FULL_THRESHOLD:
        verdict = "FULL replication"
    elif ok:
        verdict = "PARTIAL/MODERATED"
    else:
        verdict = "NEGATIVE"
    print(f"VERDICT: {verdict}")
    falsified = [c for c in cells if c[3] >= 0.5]
    if falsified:
        print(f"FALSIFIER hit: instruction-only abstained >=0.5 in {len(falsified)} cell(s): "
              + ", ".join(f"{f}/{m}" for (f, m, _, _, _) in falsified))
    else:
        print("FALSIFIER: none (instruction-only never abstained >=0.5; the 'instruction is inert' "
              "half of the principle holds universally).")
    # Necessity (instruction-only ~0 everywhere) vs sufficiency (slot-only) per field:
    print("\n# By field: slot-only abstain rate range across models (sufficiency), instr-only (necessity)")
    by_field = {}
    for (f, m, d, ir, sr) in cells:
        by_field.setdefault(f, []).append(sr)
    for f, srs in by_field.items():
        print(f"  {f}: slot-only abstain {min(srs):.2f}-{max(srs):.2f}; instr-only 0.00 (all models)")

    # Headline before/after per field + over-abstention controls
    print("\n# Headline before/after (constrained silent-wrong -> hatch abstained) + over-abstention\n")
    for field in HEADLINE_FIELDS:
        print(f"## {field}")
        for lbl, _ in MODELS:
            rep = reports.get(lbl)
            if not rep:
                continue
            probes = field_probes(rep, field)
            cpw = [c for c in probes if c["arm"] == "constrained"]
            hpw = [c for c in probes if c["arm"] == "constrained_hatch"]
            c_silent = sum(1 for c in cpw if c["outcome"] in ("wrong_sub", "omit"))
            h_abst = sum(1 for c in hpw if c["outcome"] == "abstained")
            # over-abstention on this field's supported controls (hatch arm)
            ctrl = [c for c in rep["cases"] if c["field"] == field
                    and c["defect_class"] in SUPPORT_CONTROLS and c["arm"] == "constrained_hatch"]
            over = sum(1 for c in ctrl if c["outcome"] == "over_abstain")
            print(f"  {lbl}: constrained silent-wrong {c_silent}/{len(cpw)} -> "
                  f"hatch abstained {h_abst}/{len(hpw)}; over-abstain {over}/{len(ctrl)}")
        print()

    print("# Out-of-band over-abstention (gratuitous sentinel in an UNREQUESTED field; in a rendered\n"
          "# abstainable field it fails closed, in captured-only disputeForum it is flagged for review)\n")
    for lbl, _ in MODELS:
        rep = reports.get(lbl)
        if not rep:
            continue
        h = rep["summary"]["constrained_hatch"]
        print(f"  {lbl}: control over-abstain {h['over_abstain']}/{h['over_abstain_n']} | "
              f"out-of-band {h.get('over_abstain_offfield', 0)}")


if __name__ == "__main__":
    main()
