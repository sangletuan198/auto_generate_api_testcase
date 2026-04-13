#!/usr/bin/env python3
"""
verify_contract_isolation.py  (v2 — reduced false positives)
─────────────────────────────────────────────────────────────
Cross-checks each generated Postman collection against its source contract
to ensure NO contract data leaks between APIs.

KEY INSIGHT:
  Body templates often contain structural fields beyond what active_request_fields
  lists (e.g. nested wrappers like functionCode, messageId, xferInfo).
  These are NOT cross-contamination — they're part of the API's own sampler body.

Checks performed:
  1. URL isolation       — every request URL matches only this API's URL
  2. Slug prefix         — request name prefixes don't match other API slugs
  3. Body format         — XML API ↔ XML body, JSON API ↔ JSON body
                           (skip intentional negative TCs: NEG-*, HDR-*, MTH-*)
  4. Content-Type match  — header matches body_format
                           (skip intentional HDR-* negative TCs)
  5. NEG field targeting — NEG-* cases that remove/null a field only target
                           fields from THIS API's active_request_fields
  6. Error code script   — test scripts only reference error codes from this API
                           or COMMON_ERRORS
  7. Body template leak  — body_template_xml from one API must not appear
                           inside another API's bodies
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_PATH = ROOT / "scripts" / "contracts_from_html.json"
CORRECTED_DIR  = ROOT / "output" / "corrected"

# ── Load contracts ────────────────────────────────────────────────────────────
with open(CONTRACTS_PATH, encoding="utf-8") as f:
    contracts = json.load(f)

# ── Load project_config for COMMON_ERRORS ─────────────────────────────────────
with open(ROOT / "baseline" / "project_config.json", encoding="utf-8") as f:
    pcfg = json.load(f)
COMMON_ERROR_KEYS = set(pcfg.get("common_errors", {}).keys())

# ── Build per-API metadata ────────────────────────────────────────────────────
api_meta = {}
for slug, c in contracts.items():
    field_names = {f["name"] for f in c.get("active_request_fields", [])}
    error_keys  = {e["key"] for e in c.get("active_errors", [])}
    error_codes = {e["code"] for e in c.get("active_errors", [])}
    url         = c.get("url", "?")
    body_format = c.get("sampler_body_format", "json")

    # Sampler body field names (includes nested template fields)
    sam_body = c.get("sampler_body", {})
    sam_fields = set()
    if isinstance(sam_body, dict):
        def _collect_keys(d):
            for k, v in d.items():
                sam_fields.add(k)
                if isinstance(v, dict):
                    _collect_keys(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            _collect_keys(item)
        _collect_keys(sam_body)

    # For XML APIs, also extract element names from raw XML template
    raw_xml = c.get("sampler_body_raw_xml", "")
    if raw_xml:
        for m in re.finditer(r'<(?:\w+:)?(\w+)(?:\s|>)', raw_xml):
            sam_fields.add(m.group(1))

    api_meta[slug] = {
        "fields":       field_names,
        "sam_fields":   sam_fields,
        "all_fields":   field_names | sam_fields,
        "error_keys":   error_keys,
        "error_codes":  error_codes,
        "url":          url,
        "body_format":  body_format,
        "body_raw_xml": raw_xml or None,
    }

# ── Build error key ownership (unique to one API only, not in COMMON_ERRORS) ──
all_error_keys = {}
for slug, meta in api_meta.items():
    for k in meta["error_keys"]:
        if k and k not in COMMON_ERROR_KEYS:
            all_error_keys.setdefault(k, set()).add(slug)

unique_errors = {}
for key, slugs in all_error_keys.items():
    if len(slugs) == 1:
        owner = next(iter(slugs))
        unique_errors.setdefault(owner, set()).add(key)

# ── All doc fields for reporting ──────────────────────────────────────────────
all_doc_fields = {}
for slug, meta in api_meta.items():
    for f in meta["fields"]:
        all_doc_fields.setdefault(f, set()).add(slug)


def flatten_items(items):
    """Recursively flatten Postman folder structure → list of request items."""
    result = []
    for itm in items:
        if "item" in itm:
            result.extend(flatten_items(itm["item"]))
        elif "request" in itm:
            result.append(itm)
    return result


def _is_intentional_negative(req_name: str) -> bool:
    """Return True if this TC deliberately breaks format/header/method."""
    upper = req_name.upper()
    if "MALFORMED" in upper or "INVALID" in upper:
        return True
    for prefix in ("-HDR-", "-MTH-", "-SEC-", "-SCH-"):
        if prefix in upper:
            return True
    return False


def _extract_targeted_field(req_name: str) -> str:
    """Extract the field name targeted by a NEG test case from its name."""
    for pattern in [
        r'field:\s*(\w+)',
        r'Missing.*?:\s*(\w+)',
        r'Null.*?:\s*(\w+)',
        r'Empty.*?:\s*(\w+)',
    ]:
        m = re.search(pattern, req_name, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


# ── Verification ──────────────────────────────────────────────────────────────
total_issues = 0
total_checks = 0
results = []

for slug, meta in api_meta.items():
    coll_path = CORRECTED_DIR / slug / f"{slug}_Postman_Collection.json"
    if not coll_path.exists():
        print(f"⚠️  [{slug}] Collection not found: {coll_path.name}")
        continue

    with open(coll_path, encoding="utf-8") as f:
        coll = json.load(f)

    items = flatten_items(coll.get("item", []))
    issues = []

    for itm in items:
        req_name = itm.get("name", "?")
        r = itm.get("request", {})
        is_neg = _is_intentional_negative(req_name)

        # ════ CHECK 1: URL isolation ═════════════════════════════════════════
        total_checks += 1
        url_obj = r.get("url", {})
        raw_url = url_obj.get("raw", "") if isinstance(url_obj, dict) else str(url_obj)
        if meta["url"] and meta["url"] != "?" and raw_url:
            expected_path = urlparse(meta["url"]).path
            for other_slug, other_meta in api_meta.items():
                if other_slug == slug:
                    continue
                other_url = other_meta["url"]
                if other_url and other_url != "?":
                    other_path = urlparse(other_url).path
                    if (other_path and expected_path
                            and other_path != expected_path
                            and len(other_path) > 5
                            and other_path in raw_url):
                        issues.append(
                            f"  🔴 URL LEAK [{req_name}]: "
                            f"uses path '{other_path}' from {other_slug}")

        # ════ CHECK 2: Slug prefix ═══════════════════════════════════════════
        total_checks += 1
        dash_parts = req_name.split("-")
        if len(dash_parts) >= 2:
            name_prefix = dash_parts[0].strip()
            my_prefix = slug.upper().replace("(", "").replace(")", "").replace(" ", "_")
            for other_slug in api_meta:
                if other_slug == slug:
                    continue
                other_prefix = other_slug.upper().replace("(", "").replace(")", "").replace(" ", "_")
                if name_prefix == other_prefix and name_prefix != my_prefix:
                    issues.append(
                        f"  🔴 SLUG LEAK [{req_name}]: "
                        f"prefix matches {other_slug}")

        # ════ CHECK 3: Body format (skip intentional negatives) ══════════════
        total_checks += 1
        body = r.get("body", {})
        body_raw = body.get("raw", "")

        if not is_neg:
            if meta["body_format"] == "xml":
                if body_raw.strip() and not body_raw.strip().startswith("<"):
                    issues.append(
                        f"  🔴 FORMAT MISMATCH [{req_name}]: "
                        f"XML API has non-XML body")
            else:
                if body_raw.strip().startswith("<soapenv:"):
                    issues.append(
                        f"  🔴 FORMAT MISMATCH [{req_name}]: "
                        f"JSON API has SOAP XML body")

        # ════ CHECK 4: Content-Type (skip intentional negatives) ═════════════
        total_checks += 1
        if not is_neg:
            headers = {h["key"].lower(): h["value"] for h in r.get("header", [])}
            ct = headers.get("content-type", "")
            if meta["body_format"] == "xml":
                if ct and "xml" not in ct.lower() and "text/" not in ct.lower():
                    issues.append(
                        f"  🟡 HEADER MISMATCH [{req_name}]: "
                        f"XML API has Content-Type='{ct}'")

        # ════ CHECK 5: NEG field targeting ═══════════════════════════════════
        total_checks += 1
        upper_name = req_name.upper()
        if "-NEG-" in upper_name:
            targeted = _extract_targeted_field(req_name)
            if targeted and targeted not in meta["all_fields"]:
                for other_slug, other_meta in api_meta.items():
                    if other_slug == slug:
                        continue
                    if targeted in other_meta["fields"]:
                        issues.append(
                            f"  🔴 NEG FIELD LEAK [{req_name}]: "
                            f"targets field '{targeted}' from {other_slug}")
                        break

        # ════ CHECK 6: Error code in test scripts ════════════════════════════
        total_checks += 1
        for ev in itm.get("event", []):
            script_text = "\n".join(ev.get("script", {}).get("exec", []))
            if not script_text.strip():
                continue
            for other_slug, other_unique_errs in unique_errors.items():
                if other_slug == slug:
                    continue
                for ue in other_unique_errs:
                    if not ue:
                        continue
                    if f'"{ue}"' in script_text:
                        issues.append(
                            f"  🔴 ERROR LEAK [{req_name}]: "
                            f"test script references '{ue}' (unique to {other_slug})")

        # ════ CHECK 7: XML template cross-contamination ══════════════════════
        total_checks += 1
        if body_raw.strip():
            for other_slug, other_meta in api_meta.items():
                if other_slug == slug:
                    continue
                other_xml = other_meta.get("body_raw_xml")
                if other_xml and len(other_xml) > 50:
                    m = re.search(r'<\w+:(\w+)>', other_xml)
                    if m:
                        op_name = m.group(1)
                        if op_name != slug and f"<{op_name}" in body_raw:
                            if len(op_name) > 8:
                                issues.append(
                                    f"  🔴 XML TEMPLATE LEAK [{req_name}]: "
                                    f"body contains <{op_name}> from {other_slug}")

    total_issues += len(issues)
    status = "✅ PASS" if not issues else f"❌ FAIL ({len(issues)})"
    results.append({
        "slug": slug,
        "tc_count": len(items),
        "format": meta["body_format"],
        "doc_fields": len(meta["fields"]),
        "sam_fields": len(meta["sam_fields"]),
        "errors": len(meta["error_keys"]),
        "issues": issues,
        "status": status,
    })


# ── Print Report ──────────────────────────────────────────────────────────────
W = 90
print(f"\n{'═'*W}")
print(f"  CONTRACT ISOLATION VERIFICATION REPORT (v2)")
print(f"  {len(api_meta)} APIs • {total_checks} checks • {total_issues} issue(s)")
print(f"{'═'*W}\n")

print(f"  {'API':<42} {'TCs':>4}  {'Fmt':>4}  {'DocF':>4}  {'SamF':>4}  {'Errs':>4}  Status")
print(f"  {'─'*42} {'─'*4}  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*15}")

for r in results:
    print(
        f"  {r['slug']:<42} {r['tc_count']:>4}  {r['format']:>4}  "
        f"{r['doc_fields']:>4}  {r['sam_fields']:>4}  {r['errors']:>4}  {r['status']}"
    )
    for iss in r["issues"]:
        print(f"    {iss}")

# ── Cross-API summary (info only) ────────────────────────────────────────────
shared = {f: s for f, s in all_doc_fields.items() if len(s) > 1}
if shared:
    print(f"\n  ℹ️  SHARED DOC FIELDS ({len(shared)} fields appear in 2+ APIs — not a leak):")
    for f, slugs in sorted(shared.items(), key=lambda x: (-len(x[1]), x[0])):
        print(f"    {f:<28}  → {', '.join(sorted(slugs))}")

print(f"\n{'═'*W}")
overall = (
    "✅ ALL PASS — No contract cross-contamination detected"
    if total_issues == 0
    else f"❌ {total_issues} ISSUE(S) FOUND — review above"
)
print(f"  RESULT: {overall}")
print(f"{'═'*W}\n")

sys.exit(1 if total_issues > 0 else 0)
