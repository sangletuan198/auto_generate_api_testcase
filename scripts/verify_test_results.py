#!/usr/bin/env python3
"""
verify_test_results.py
══════════════════════
Đọc newman-report.json từ bundle mới nhất (hoặc chỉ định), rồi phân loại
từng test case thành 4 nhóm:

  ✅ DOC+PASS        Có trong tài liệu, test PASS đúng kỳ vọng
  ⚠️  DOC+FAIL        Có trong tài liệu, nhưng test FAIL (server chưa đúng doc)
  🔵 ASSUMPTION+PASS  Script-added (not in doc), test PASS
  ❌ ASSUMPTION+FAIL  Script tự thêm, test FAIL (assumption sai hoặc server lỗi)

Also analyses each validate item in the script:
  [FROM_DOC]   HTTP status / error code / error message từ tài liệu HTML
  [ASSUMPTION] Giá trị script tự suy luận / suy đoán

Usage:
  python3 verify_test_results.py                     # bundle mới nhất, tất cả API
  python3 verify_test_results.py --bundle 20260227-094007
  python3 verify_test_results.py --api getAccountByCif
  python3 verify_test_results.py --target corrected
  python3 verify_test_results.py --output verification_report.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR   = SCRIPT_DIR.parent
OUTPUT_DIR = ROOT_DIR / "output"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
CONTRACTS_FILE = SCRIPT_DIR / "contracts_from_html.json"

# ---------------------------------------------------------------------------
# Assertion source classification
# ---------------------------------------------------------------------------

# Assertions mà giá trị kỳ vọng (expected status / error code) đến TRỰC TIẾP
# từ tài liệu HTML được parse vào contracts_from_html.json
DOC_CATEGORIES = {
    # Các loại TC mà expected_status đến từ doc error mapping
    "Positive",            # 200 — doc nói API trả 200 khi đúng
    "Error Handling",      # 4xx — error codes lấy từ doc
    "Authentication",      # 401/403 — common errors trong doc
    "HTTP Status Codes",   # doc liệt kê các HTTP status
}

# Các TC category mà giá trị hoàn toàn do script ASSUMPTION
ASSUMPTION_CATEGORIES = {
    "Boundary Value Analysis",    # 260 chars → 400: doc does not specify length limit
    "Equivalence Partitioning",   # doc does not list specific invalid partitions
    "Header Validation",          # doc usually does not specify 415 / 400 cho header
    "HTTP Method",                # 405 khi gọi GET/PUT — doc usually does not mention
    "Security",                   # XSS, SQLi → 400: not in doc
    "Edge Cases",                 # Unicode, null byte: not in doc
    "Rate Limiting",              # doc thường không có
    "Data Consistency",           # not in doc functional spec
    "Idempotency",                # thường not in doc
    "Performance",                # doc has no threshold 5000ms
    "Response Header",            # doc hiếm khi nói Content-Type response
    "Negative Validation",        # một phần từ doc, một phần assumption
    "Schema Validation",          # schema fields từ doc nhưng type assertion = assumption
    "Business Rules",             # từ doc nhưng cách test cụ thể = assumption
}

# Assertions with specific names are considered FROM_DOC if they have error codes in contracts
_DOC_ASSERTION_PATTERNS = [
    r"HTTP status is \d+",              # status code từ doc
    r"Error code is DT\.",              # error code cụ thể từ doc
    r"Error messageKey is ",            # messageKey từ doc
]

# Assertions tên cụ thể luôn là ASSUMPTION
_ASSUMPTION_ASSERTION_PATTERNS = [
    r"Response time under",             # performance threshold
    r"valid JSON",                      # script adds
    r"Content-Type is application/json", # response header check
    r"envelope has standard fields",    # envelope structure assumption
    r"data has required fields",        # schema
    r"is a string",                     # type check
    r"is an array",                     # type check
    r"is an object",                    # type check
    r"Array items",                     # array structure
]


def classify_assertion_source(assertion_name: str, contracts_errors: set) -> str:
    """
    Phân loại 1 assertion là FROM_DOC hay ASSUMPTION.

    Returns: 'FROM_DOC' | 'ASSUMPTION'
    """
    # Check explicit DOC patterns
    for pat in _DOC_ASSERTION_PATTERNS:
        if re.search(pat, assertion_name, re.IGNORECASE):
            # Further check: if it contains a specific DT. code, it's from doc
            if "DT." in assertion_name:
                return "FROM_DOC"
            # HTTP status check: only FROM_DOC if status matches a known doc error
            m = re.search(r"HTTP status is (\d+)", assertion_name)
            if m:
                status = int(m.group(1))
                # 200 → positive scenario from doc; 401/403/408/500 → common errors in doc
                if status in (200, 401, 403, 408, 500):
                    return "FROM_DOC"
                # 4xx that matches a specific API error → may be doc
                return "FROM_DOC" if status in contracts_errors else "ASSUMPTION"
            return "FROM_DOC"

    # Check explicit ASSUMPTION patterns
    for pat in _ASSUMPTION_ASSERTION_PATTERNS:
        if re.search(pat, assertion_name, re.IGNORECASE):
            return "ASSUMPTION"

    return "ASSUMPTION"


def classify_tc_source(tc_name: str, tc_cid: str, contracts: dict, slug: str) -> str:
    """
    Phân loại toàn bộ test case là FROM_DOC hay ASSUMPTION dựa vào tên / prefix.
    """
    # AUTH prefix: error codes UNAUTHORIZED, FORBIDDEN từ COMMON_ERRORS = doc
    if tc_cid.startswith("AUTH-"):
        return "FROM_DOC"

    # POS-001/002: positive scenario — doc nói API phải trả 200 khi đúng
    if tc_cid.startswith("POS-"):
        return "FROM_DOC"

    # ERR- prefix: error handling — error codes từ doc
    if tc_cid.startswith("ERR-"):
        # ERR-001, ERR-002 check error structure — partially doc (code/message fields)
        return "FROM_DOC"

    # HSC- HTTP status codes — từ doc
    if tc_cid.startswith("HSC-"):
        return "FROM_DOC"

    # NEG- Negative validation — doc nói field là mandatory nhưng threshold cụ thể là assumption
    if tc_cid.startswith("NEG-"):
        # "Missing mandatory field" → mandatory flag từ doc → FROM_DOC
        if "Missing mandatory field" in tc_name or "Empty string" in tc_name:
            return "FROM_DOC"
        return "ASSUMPTION"

    # API-specific error test cases (generated from api_errors in contracts)
    contract = contracts.get(slug, {})
    active_errors = {e["key"] for e in contract.get("active_errors", [])}
    for err_key in active_errors:
        if err_key.lower().replace("_", "") in tc_name.lower().replace("_", "").replace(" ", ""):
            return "FROM_DOC"

    # Tất cả còn lại → ASSUMPTION
    return "ASSUMPTION"


# ---------------------------------------------------------------------------
# Result classification (4 quadrants)
# ---------------------------------------------------------------------------

def classify_result(source: str, passed: bool) -> str:
    """Return one of 4 classification labels."""
    if source == "FROM_DOC" and passed:
        return "DOC+PASS"
    elif source == "FROM_DOC" and not passed:
        return "DOC+FAIL"
    elif source == "ASSUMPTION" and passed:
        return "ASSUMPTION+PASS"
    else:
        return "ASSUMPTION+FAIL"


RESULT_ICONS = {
    "DOC+PASS":        "✅",
    "DOC+FAIL":        "⚠️ ",
    "ASSUMPTION+PASS": "🔵",
    "ASSUMPTION+FAIL": "❌",
}

RESULT_LABELS = {
    "DOC+PASS":        "Đúng tài liệu và PASS",
    "DOC+FAIL":        "Đúng tài liệu nhưng FAIL (server chưa tuân doc)",
    "ASSUMPTION+PASS": "Tài liệu chưa nói, script assumption đúng → PASS",
    "ASSUMPTION+FAIL": "Tài liệu chưa nói, script assumption cũng sai → FAIL",
}

# ---------------------------------------------------------------------------
# Parse TC id and name from item name
# ---------------------------------------------------------------------------

def parse_tc_parts(item_name: str):
    """
    Parse from Postman item name like:
      "GETACCOUNTBYCIF()-POS-001 - Valid request with all parameters"
    Returns (slug_prefix, cid, display_name)
    """
    # Pattern: <SLUG>-<CID> - <name>  OR  <CID> - <name>
    m = re.match(r'^(.+?)\(?\)?\s*[-–]\s*(.+)$', item_name, re.IGNORECASE)
    if m:
        left = m.group(1).strip()
        right = m.group(2).strip()
    else:
        left = item_name
        right = item_name

    # Try to extract CID like POS-001, NEG-003, AUTH-001, etc.
    cid_m = re.search(r'\b(POS|NEG|BVA|EP|AUTH|HDR|MTH|SCH|ERR|SEC|IDM|EDG|HSC|RAT|DAT|PER|BR|OTC)-(\d+)\b', item_name, re.IGNORECASE)
    if cid_m:
        cid = f"{cid_m.group(1).upper()}-{cid_m.group(2)}"
        # Display name = everything after the CID pattern
        after_cid = item_name[cid_m.end():].strip(' -–')
        display = after_cid if after_cid else item_name
    else:
        cid = "UNKNOWN"
        display = item_name

    return cid, display


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_contracts() -> dict:
    if CONTRACTS_FILE.exists():
        with open(CONTRACTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def find_latest_bundle() -> Optional[Path]:
    if not BUNDLES_DIR.exists():
        return None
    bundles = sorted([d for d in BUNDLES_DIR.iterdir() if d.is_dir()], reverse=True)
    return bundles[0] if bundles else None


def load_newman_report(report_path: Path) -> dict:
    with open(report_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Find slug from folder name (fuzzy match contracts key)
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r'[()\s\-_]+', '', s).lower()


def match_slug(folder_name: str, contracts: dict) -> str:
    """Match a bundle folder name to a contracts slug."""
    norm_folder = _normalize(folder_name)
    for slug in contracts:
        if _normalize(slug) == norm_folder:
            return slug
        if _normalize(slug) in norm_folder or norm_folder in _normalize(slug):
            return slug
    return folder_name  # fallback


# ---------------------------------------------------------------------------
# Process one API report
# ---------------------------------------------------------------------------

def process_api_report(api_dir: Path, contracts: dict, target: str) -> dict:
    """
    Đọc newman-report.json trong api_dir, phân loại từng assertion.
    Returns summary dict.
    """
    report_path = api_dir / "newman-report.json"
    if not report_path.exists():
        return {"api": api_dir.name, "error": "no report"}

    folder_name = api_dir.name
    slug = match_slug(folder_name, contracts)
    contract = contracts.get(slug, {})

    # Set of HTTP status codes that appear in doc errors
    doc_http_statuses = {200, 401, 403, 408, 500}
    for err in contract.get("active_errors", []):
        code = err.get("code", "")
        if "401" in code: doc_http_statuses.add(401)
        elif "403" in code: doc_http_statuses.add(403)
        elif "404" in code or ".000" in code: doc_http_statuses.add(404)
        elif "400" in code or ".001" in code: doc_http_statuses.add(400)

    contracts_errors = doc_http_statuses

    data = load_newman_report(report_path)
    executions = data.get("run", {}).get("executions", [])

    results = []
    for ex in executions:
        item_name = ex.get("item", {}).get("name", "UNKNOWN")
        assertions = ex.get("assertions", [])

        cid, display_name = parse_tc_parts(item_name)

        # Classify TC source
        tc_source = classify_tc_source(display_name, cid, contracts, slug)

        # Aggregate pass/fail across all assertions in this TC
        tc_passed = all(a.get("error") is None for a in assertions)
        tc_result = classify_result(tc_source, tc_passed)

        # Per-assertion breakdown
        assertion_details = []
        for a in assertions:
            a_name = a.get("assertion", "?")
            a_err = a.get("error")
            a_passed = a_err is None
            a_source = classify_assertion_source(a_name, contracts_errors)
            assertion_details.append({
                "name":   a_name,
                "source": a_source,
                "passed": a_passed,
                "error":  a_err.get("message", "") if a_err else None,
                "result": classify_result(a_source, a_passed),
            })

        results.append({
            "tc_id":       f"{slug.upper()}-{cid}" if cid != "UNKNOWN" else item_name,
            "display_name": display_name,
            "cid":         cid,
            "item_name":   item_name,
            "tc_source":   tc_source,
            "tc_passed":   tc_passed,
            "tc_result":   tc_result,
            "assertions":  assertion_details,
        })

    # Summary stats
    stats = {"DOC+PASS": 0, "DOC+FAIL": 0, "ASSUMPTION+PASS": 0, "ASSUMPTION+FAIL": 0}
    for r in results:
        stats[r["tc_result"]] += 1

    return {
        "api":      folder_name,
        "slug":     slug,
        "target":   target,
        "total":    len(results),
        "stats":    stats,
        "results":  results,
        "contract": contract,
    }


# ---------------------------------------------------------------------------
# Build DIFF section: label each validate item FROM_DOC vs ASSUMPTION
# ---------------------------------------------------------------------------

def build_diff_section(api_data: dict) -> list[str]:
    """
    Sinh phần DIFF cho 1 API: liệt kê mã lỗi, message, validate rules
    với nhãn [FROM_DOC] hoặc [ASSUMPTION].
    """
    lines = []
    contract = api_data.get("contract", {})
    slug = api_data.get("slug", "?")
    lines.append(f"\n### Validate Items — `{slug}`")
    lines.append("")
    lines.append("| # | Item | Giá trị kỳ vọng | Nguồn |")
    lines.append("|---|------|----------------|-------|")

    row = 1

    # --- HTTP Method ---
    doc_method = contract.get("doc_method", "?")
    actual_method = contract.get("method", "POST")
    if doc_method != actual_method and doc_method not in ("?", "NOT FOUND"):
        lines.append(f"| {row} | HTTP Method | doc: `{doc_method}` → thực tế: `{actual_method}` | [FROM_DOC] |")
    else:
        lines.append(f"| {row} | HTTP Method | `{actual_method}` | [FROM_DOC] |")
    row += 1

    # --- Request fields ---
    for f in contract.get("active_request_fields", []):
        fname = f.get("name", "?")
        ftype = f.get("type", "?")
        mandatory = "Y" if f.get("mandatory", "N").upper() == "Y" else "N"
        note = f.get("note", "") or ""
        note_short = note[:60] + "…" if len(note) > 60 else note
        lines.append(
            f"| {row} | Request field `{fname}` | type={ftype}, mandatory={mandatory}"
            f"{', note: ' + note_short if note_short else ''} | [FROM_DOC] |"
        )
        row += 1

    # Struck fields (removed from corrected)
    for fname in contract.get("struck_request_fields", []):
        lines.append(f"| {row} | Request field `{fname}` (struck in doc) | — bỏ trong corrected | [FROM_DOC] |")
        row += 1

    # --- Error codes ---
    lines.append(f"| | **Error Codes** | | |")
    for err in contract.get("active_errors", []):
        key  = err.get("key", "?")
        code = err.get("code", "?")
        # Common error codes (4xx/5xx UNAUTHORIZED etc.) are always in doc
        lines.append(f"| {row} | Error `{key}` | code=`{code}` | [FROM_DOC] |")
        row += 1

    for code in contract.get("struck_errors", []):
        lines.append(f"| {row} | Error code `{code}` (struck in doc) | — removed | [FROM_DOC] |")
        row += 1

    # --- Assumptions (what the script adds on top) ---
    lines.append(f"| | **Script Assumptions** | | |")
    assumptions = [
        ("HTTP status 415 khi Content-Type sai",     "HDR-001/002",  "Doc does not specify"),
        ("HTTP status 405 khi sai HTTP method",       "MTH-001~004",  "Doc does not specify"),
        ("HTTP status 400 cho BVA (260 chars, etc.)", "BVA-001~004",  "Doc has no length limit"),
        ("HTTP status 400 cho EP invalid partition",  "EP-002~003",   "Doc does not list invalid partitions"),
        ("Response time < 5000ms",                    "PER-001",      "KPI target từ baseline/coverage_requirements.json"),
        ("Response Content-Type là application/json", "Tất cả",       "REST convention"),
        ("Response envelope: code, message, data",    "SCH-001~003",  "REST convention"),
        ("XSS / SQLi / null-byte → 400",              "SEC-001~004",  "Security best practice"),
        ("Idempotency: repeat call = same result",    "IDM-001~002",  "REST convention"),
        ("Data consistency: repeat call = same data", "DAT-001",      "REST convention"),
        ("Missing channel header → 400",              "HDR-003",      "Assumption, not explicitly stated in doc"),
        ("Missing x-trace-id header → 400",          "HDR-005",      "Assumption, not explicitly stated in doc"),
        ("Empty body → 400",                          "NEG-*",        "Assumption"),
        ("Whitespace-only field → 400",               "NEG-*",        "Assumption"),
        ("Unicode chars trong field → 400",           "EDG-001",      "Assumption"),
        ("Extra unknown fields trong body → 200",     "EDG-002",      "Assumption (lenient parsing)"),
        ("Numeric type thay string → 400",            "EDG-003",      "Assumption (strict typing)"),
    ]
    for desc, tc_ref, reason in assumptions:
        lines.append(f"| {row} | {desc} | TC: {tc_ref} | [ASSUMPTION] — {reason} |")
        row += 1

    return lines


# ---------------------------------------------------------------------------
# Format markdown report
# ---------------------------------------------------------------------------

def format_api_report(d: dict, bundle_ts: str) -> str:
    """Format verification report for a single API."""
    lines = []
    if "error" in d:
        lines.append(f"# Verification Report — {d['api']}")
        lines.append(f"> ⚠️ Không tìm thấy report: {d['error']}")
        return "\n".join(lines)

    lines.append(f"# Verification Report — {d['api']}")
    lines.append(f"")
    lines.append(f"> Bundle: `{bundle_ts}`  ")
    lines.append(f"> Target: `{d['target']}`  ")
    lines.append(f"> Generated: script `verify_test_results.py`")
    lines.append(f"")
    lines.append(f"## Legend")
    lines.append(f"")
    lines.append(f"| Icon | Loại | Ý nghĩa |")
    lines.append(f"|------|------|---------|")
    lines.append(f"| ✅ | DOC+PASS | Có trong tài liệu, test PASS đúng kỳ vọng |")
    lines.append(f"| ⚠️  | DOC+FAIL | Có trong tài liệu, test FAIL (server chưa đúng doc) |")
    lines.append(f"| 🔵 | ASSUMPTION+PASS | Script tự thêm (not in doc), assumption đúng → PASS |")
    lines.append(f"| ❌ | ASSUMPTION+FAIL | Script-added, assumption wrong or server does not follow convention → FAIL |")
    lines.append(f"")

    s = d["stats"]
    lines.append(f"## Summary")
    lines.append(f"")
    lines.append(
        f"- **Total**: {d['total']} TC  |  "
        f"✅ DOC+PASS: {s['DOC+PASS']}  |  "
        f"⚠️ DOC+FAIL: {s['DOC+FAIL']}  |  "
        f"🔵 ASS+PASS: {s['ASSUMPTION+PASS']}  |  "
        f"❌ ASS+FAIL: {s['ASSUMPTION+FAIL']}"
    )
    lines.append(f"")

    d_copy = dict(d)  # reuse per-API block rendering below
    _append_api_detail(lines, d_copy)
    return "\n".join(lines)


def _append_api_detail(lines: list, d: dict):
    """Append all TC detail sections for one API into `lines` in-place."""
    # DIFF section
    lines.extend(build_diff_section(d))
    lines.append(f"")

    # TC detail table
    lines.append(f"## Test Case Results")
    lines.append(f"")
    lines.append(f"| TC ID | Tên | Nguồn | Kết quả | Lý do FAIL |")
    lines.append(f"|-------|-----|-------|---------|-----------|")

    for r in d["results"]:
        icon = RESULT_ICONS[r["tc_result"]]
        label = r["tc_result"]
        fails = [
            f"`{a['name']}`: {a['error']}"
            for a in r["assertions"]
            if not a["passed"]
        ]
        fail_str = "; ".join(fails[:2])
        if len(fails) > 2:
            fail_str += f" (+{len(fails)-2} more)"
        lines.append(
            f"| `{r['cid']}` | {r['display_name'][:60]} "
            f"| [{r['tc_source']}] | {icon} {label} "
            f"| {fail_str} |"
        )

    # DOC+PASS
    doc_passes = [r for r in d["results"] if r["tc_result"] == "DOC+PASS"]
    if doc_passes:
        lines.append(f"## ✅ DOC+PASS — Chi tiết")
        lines.append(f"")
        lines.append("> Các TC này có expected value từ TÀI LIỆU và server trả đúng kỳ vọng.")
        lines.append(f"")
        for r in doc_passes:
            lines.append(f"### `{r['cid']}` — {r['display_name']}")
            for a in r["assertions"]:
                lines.append(f"- [{a['source']}] ✅ `{a['name']}`")
            lines.append(f"")

    # DOC+FAIL
    doc_fails = [r for r in d["results"] if r["tc_result"] == "DOC+FAIL"]
    if doc_fails:
        lines.append(f"")
        lines.append(f"## ⚠️ DOC+FAIL — Chi tiết (cần hành động)")
        lines.append(f"")
        lines.append(
            "> Các TC này có expected value từ TÀI LIỆU nhưng server FAIL.\n"
            "> → Cần xác nhận: server bug? hay tài liệu chưa cập nhật?"
        )
        lines.append(f"")
        for r in doc_fails:
            lines.append(f"### `{r['cid']}` — {r['display_name']}")
            for a in r["assertions"]:
                if not a["passed"]:
                    lines.append(f"- [{a['source']}] ❌ `{a['name']}`  ")
                    lines.append(f"  → {a['error']}")
            lines.append(f"")

    # ASSUMPTION+PASS
    ass_passes = [r for r in d["results"] if r["tc_result"] == "ASSUMPTION+PASS"]
    if ass_passes:
        lines.append(f"## 🔵 ASSUMPTION+PASS — Chi tiết")
        lines.append(f"")
        lines.append("> Script-added (not in doc), assumption correct — server responded as expected.")
        lines.append(f"")
        for r in ass_passes:
            lines.append(f"### `{r['cid']}` — {r['display_name']}")
            for a in r["assertions"]:
                lines.append(f"- [{a['source']}] ✅ `{a['name']}`")
            lines.append(f"")

    # ASSUMPTION+FAIL
    ass_fails = [r for r in d["results"] if r["tc_result"] == "ASSUMPTION+FAIL"]
    if ass_fails:
        lines.append(f"## ❌ ASSUMPTION+FAIL — Chi tiết")
        lines.append(f"")
        lines.append(
            "> Script assumption does not match server behaviour.\n"
            "> → Có thể cần điều chỉnh expected_status trong script."
        )
        lines.append(f"")
        for r in ass_fails:
            lines.append(f"### `{r['cid']}` — {r['display_name']}")
            for a in r["assertions"]:
                if not a["passed"]:
                    lines.append(f"- [{a['source']}] ❌ `{a['name']}`  ")
                    lines.append(f"  → {a['error']}")
            lines.append(f"")


def format_report(all_api_data: list[dict], bundle_ts: str) -> str:
    """Kept for backward compatibility — builds combined report string."""
    parts = []
    for d in all_api_data:
        parts.append(format_api_report(d, bundle_ts))
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Verify and classify Newman test results by source (DOC vs ASSUMPTION)"
    )
    parser.add_argument("--bundle", help="Bundle timestamp (e.g. 20260227-094007). Default: latest")
    parser.add_argument("--api", help="Filter by API name (partial match)")
    parser.add_argument("--target", default="corrected", choices=["corrected", "doc_literal", "all"],
                        help="Which target to analyze. Default: corrected")
    parser.add_argument("--output", help="Output markdown file path. Default: print to stdout")
    args = parser.parse_args()

    contracts = load_contracts()

    # Resolve bundle
    if args.bundle:
        bundle_dir = BUNDLES_DIR / args.bundle
        if not bundle_dir.exists():
            print(f"[ERROR] Bundle not found: {bundle_dir}", file=sys.stderr)
            sys.exit(1)
    else:
        bundle_dir = find_latest_bundle()
        if not bundle_dir:
            print("[ERROR] Không tìm thấy bundle nào trong output/bundles/", file=sys.stderr)
            sys.exit(1)
    bundle_ts = bundle_dir.name
    print(f"[INFO] Bundle: {bundle_ts}", file=sys.stderr)

    targets = ["corrected", "doc_literal"] if args.target == "all" else [args.target]

    all_api_data = []
    for target in targets:
        target_dir = bundle_dir / target
        if not target_dir.exists():
            print(f"[SKIP] Không có thư mục: {target_dir}", file=sys.stderr)
            continue
        for api_dir in sorted(target_dir.iterdir()):
            if not api_dir.is_dir():
                continue
            if args.api and args.api.lower() not in api_dir.name.lower():
                continue
            print(f"[INFO] Processing: {target}/{api_dir.name}", file=sys.stderr)
            api_data = process_api_report(api_dir, contracts, target)
            all_api_data.append(api_data)

            # Write per-API verification_report.md immediately
            per_api_report = format_api_report(api_data, bundle_ts)
            per_api_path = api_dir / "verification_report.md"
            per_api_path.write_text(per_api_report, encoding="utf-8")
            print(f"[INFO] Report saved: {per_api_path.relative_to(bundle_dir)}", file=sys.stderr)

    if not all_api_data:
        print("[WARN] No APIs to analyse.", file=sys.stderr)
        sys.exit(0)

    print(f"[INFO] Done. {len(all_api_data)} API(s) processed. Per-API reports written.", file=sys.stderr)


if __name__ == "__main__":
    main()
