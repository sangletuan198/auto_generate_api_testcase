#!/usr/bin/env python3
import sys
sys.path.insert(0, "scripts")
import generate_outputs as gen
import regen_from_contracts as regen

contracts = regen.load_contracts()
api_defs = regen.build_api_defs_from_contracts(contracts, use_doc_method=False)

COMMON_CODES = {"DT.005.00.401", "DT.005.00.403", "DT.005.00.408", "DT.005.00.500"}
COMMON_SKIP = {"UNAUTHORIZED", "FORBIDDEN", "SYSTEM_TIMEOUT", "INTERNAL_SERVER_ERROR"}

for api_def in api_defs:
    slug = api_def["slug"]
    errors = api_def.get("api_errors", {})
    cases = gen.generate_all_cases(api_def)
    err_cases = [c for c in cases if c["category"] == "Error Handling"]
    err_keys_covered = set()
    for c in cases:
        ek = c.get("expected_error_key")
        if ek:
            err_keys_covered.add(ek)
    missing = set(errors.keys()) - err_keys_covered - COMMON_SKIP
    missing = {k for k in missing if errors[k].get("code", "") not in COMMON_CODES}

    total = len(cases)
    print(f"{slug}: ERR={len(err_cases)}, total={total}, missing={missing or 'none'}")
    for c in err_cases:
        tc_id = c.get("id", "?")
        name = c.get("name", "?")
        print(f"    {tc_id}: {name}")
    print()
