#!/usr/bin/env python3
import sys
sys.path.insert(0, "scripts")
import generate_outputs as gen
import regen_from_contracts as regen

contracts = regen.load_contracts()
api_defs = regen.build_api_defs_from_contracts(contracts, use_doc_method=False)

total = 0
for api_def in api_defs:
    slug = api_def["slug"]
    cases = gen.generate_all_cases(api_def)
    cats = {}
    for c in cases:
        cat = c["category"]
        cats[cat] = cats.get(cat, 0) + 1
    total += len(cases)
    pos_count = cats.get("Positive", 0)
    ep_count = cats.get("Equivalence Partitioning", 0)
    neg_count = cats.get("Negative Validation", 0)
    bva_count = cats.get("Boundary Value Analysis", 0)
    print(f"{slug}: POS={pos_count}, EP={ep_count}, NEG={neg_count}, BVA={bva_count}, total={len(cases)}")
    for c in cases:
        if c["category"] == "Positive":
            cid = c.get("cid", "???")
            name = c.get("name", "???")
            print(f"    {cid}: {name}")

print(f"\nGRAND TOTAL: {total}")
