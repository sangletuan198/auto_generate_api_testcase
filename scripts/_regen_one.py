#!/usr/bin/env python3
"""
_regen_one.py — Regenerate a single API to a custom output folder.

Usage:
    python3 scripts/_regen_one.py <api_slug> <output_subdir>

Example:
    python3 scripts/_regen_one.py createResource output/corrected_draft
"""
import sys
import json
from pathlib import Path


def _prompt_json_body(prompt_text: str):
    """Prompt user for a JSON string (single or multi-line). Return parsed dict or None."""
    print(prompt_text)
    print("  (Paste JSON rồi nhấn Enter 2 lần để kết thúc, hoặc bỏ trống để bỏ qua)")
    lines = []
    try:
        while True:
            line = input()
            if not line and lines:
                break
            lines.append(line)
    except EOFError:
        pass
    raw = '\n'.join(lines).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  ⚠️  Invalid JSON: {exc}")
        return None

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_REPO  = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import regen_from_contracts as _regen
import generate_outputs as _gen

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    target_slug = sys.argv[1]
    out_subdir  = sys.argv[2]

    # Load contracts
    contracts = _regen.load_contracts()
    if target_slug not in contracts:
        print(f"❌  API '{target_slug}' not found in contracts_from_html.json")
        print(f"   Available: {', '.join(contracts.keys())}")
        sys.exit(1)

    # Find & load sampler (largest collection)
    postman_dir = ROOT_REPO / "postman"
    candidates = [p for p in sorted(postman_dir.iterdir())
                  if p.name.endswith('.postman_collection.json')] if postman_dir.is_dir() else []
    SAMPLER_PATH = max(candidates, key=_regen._count_postman_requests) if candidates else None
    SAMPLER_EXISTS = SAMPLER_PATH is not None and SAMPLER_PATH.exists()

    print(f"\n  ℹ️  Sampler: {SAMPLER_PATH.name if SAMPLER_EXISTS else 'NONE'}")
    sampler_meta = _regen.read_sampler_metadata(SAMPLER_PATH) if SAMPLER_EXISTS else None

    # Build manual setup items map
    _manual_prereq_path = ROOT_REPO / "baseline" / "manual_prerequisites.json"
    all_sampler_paths = candidates if SAMPLER_EXISTS else []
    manual_setup_items_map = _regen._build_manual_setup_items_map(
        all_sampler_paths, _manual_prereq_path
    ) if SAMPLER_EXISTS else {}

    # Build api_def for this single API
    contract = contracts[target_slug]
    api_def = _regen._build_generic_api_def(
        target_slug, contract,
        use_doc_method=False,
        sampler_meta=sampler_meta,
        manual_setup_items_map=manual_setup_items_map,
    )

    # ── Interactive prompt: ask for response body if not auto-detected ───────
    if not api_def.get("response_data_fields") and sys.stdin.isatty():
        print(f"\n  ⚠️  No sample response body found for '{target_slug}' (sampler has no example).")
        print("  Nhập response JSON để tạo assertions cấu trúc response body.")

        success_body = _prompt_json_body("\n  📋 SUCCESS response JSON (HTTP 200):")
        failure_body = _prompt_json_body("\n  📋 FAILURE response JSON (HTTP 4xx/5xx):")

        if success_body:
            data_fields = _gen._extract_fields_from_response_body(success_body)
            if data_fields:
                api_def["response_data_fields"] = data_fields
                print(f"  ✅  Đã extract {len(data_fields)} fields: {list(data_fields.keys())}")
            else:
                print("  ⚠️  Không extract được fields từ success body.")

        # Persist to contracts_from_html.json
        if success_body or failure_body:
            contracts_path = SCRIPT_DIR / "contracts_from_html.json"
            contracts[target_slug]["response_data_fields"] = api_def.get("response_data_fields", {})
            if success_body:
                contracts[target_slug]["response_success_body"] = success_body
            if failure_body:
                contracts[target_slug]["response_failure_body"] = failure_body
            contracts_path.write_text(
                json.dumps(contracts, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"  💾  Đã lưu response structure vào contracts_from_html.json")
    elif not api_def.get("response_data_fields"):
        print(f"  ℹ️  Không có response_data_fields — bỏ qua assertions cấu trúc data.")

    # Output path
    out_dir = ROOT_REPO / out_subdir / target_slug
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate
    cases = _gen.generate_all_cases(api_def)
    print(f"\n  Generating {len(cases)} TCs → {out_dir.relative_to(ROOT_REPO)}/")

    label = out_subdir.rstrip('/').rsplit('/', 1)[-1]
    _gen.create_csv(target_slug, api_def, cases, out_dir / f"TestCases_{target_slug}.csv")
    _gen.create_collection(target_slug, api_def, cases,
                           out_dir / f"{target_slug}_Postman_Collection.json",
                           sampler_meta=sampler_meta, collection_label=label)
    _gen.create_coverage_summary(target_slug, api_def, cases,
                                 out_dir / "Test_Coverage_Summary.md", is_new=True)
    _gen.create_traceability_file(target_slug, api_def, cases,
                                  out_dir / f"API-{target_slug}.test-case-traceability.md")
    _gen.create_excel(target_slug, api_def, cases, out_dir / f"TestCases_{target_slug}.xlsx")

    print(f"\n  ✅  Done → {out_dir.relative_to(ROOT_REPO)}/")

if __name__ == "__main__":
    main()
