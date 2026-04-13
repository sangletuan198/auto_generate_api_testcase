#!/usr/bin/env python3
"""
regen_from_contracts.py
━━━━━━━━━━━━━━━━━━━━━━
Đọc contracts_from_html.json (parsed từ 3 HTML Confluence) và sinh ra:

    output/doc_literal/   → Bản THEO DOC (method = GET như doc ghi, kể cả sai)
    output/corrected/     → Bản CHUẨN ĐÚNG (method = POST đúng thực tế, struck fields bỏ ra)

Không sửa generate_outputs.py; import trực tiếp các hàm gen từ đó.
"""

import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).resolve().parent
ROOT_REPO   = SCRIPT_DIR.parent
CONTRACTS   = SCRIPT_DIR / "contracts_from_html.json"
KNOWN_ENUMS = ROOT_REPO / "baseline" / "known_enums.json"

sys.path.insert(0, str(SCRIPT_DIR))

# SoapUI XML parser
from soapui_parser import parse_soapui_xml

# SOAP XML body utilities
from soap_body_utils import detect_soap_body, apply_soap_body_mod, soap_body_to_flat_dict

# ---------------------------------------------------------------------------
# Import generator infrastructure from generate_outputs
# We only import functions/constants – main() is NOT called (guarded by __main__)
# ---------------------------------------------------------------------------
import generate_outputs as _gen

# bring all gen helpers into local namespace
build_url_obj            = _gen.build_url_obj
build_default_body       = _gen.build_default_body
apply_body_modifications = _gen.apply_body_modifications
postman_test_script      = _gen.postman_test_script
generate_common_cases    = _gen.generate_common_cases
generate_all_cases       = _gen.generate_all_cases
create_collection        = _gen.create_collection
create_csv               = _gen.create_csv
create_coverage_summary  = _gen.create_coverage_summary
create_traceability_file = _gen.create_traceability_file
create_excel             = _gen.create_excel
read_sampler_metadata    = _gen.read_sampler_metadata
STANDARD_HEADERS         = _gen.STANDARD_HEADERS
RESPONSE_ENVELOPE        = _gen.RESPONSE_ENVELOPE
CATEGORIES               = _gen.CATEGORIES
KPI_TARGETS              = _gen.KPI_TARGETS


# ---------------------------------------------------------------------------
# Build API definitions from contracts_from_html.json
# ---------------------------------------------------------------------------
def _kebab_to_camel(s: str) -> str:
    """Convert 'create-resource' or 'createResource' to camelCase."""
    s = s.strip('/ ')
    parts = re.split(r'[-_]+', s)
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _build_setup_item_from_raw(req_item: dict) -> dict:
    """Convert a raw Postman request item to the setup_item format
    expected by generate_outputs._build_setup_prerequest_js()."""
    r = req_item.get('request', {})
    url_obj = r.get('url', {})
    raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
    body_raw = r.get('body', {}).get('raw', '{}') if isinstance(r.get('body'), dict) else '{}'
    try:
        body = json.loads(body_raw)
    except (json.JSONDecodeError, ValueError):
        fixed = re.sub(r'(?<!")(\{\{[^}]+\}\})(?!")', r'"\1"', body_raw)
        try:
            body = json.loads(fixed)
        except Exception:
            body = {}
    headers = {h['key']: h['value'] for h in r.get('header', [])}
    method  = r.get('method', 'POST')
    name    = req_item.get('name', 'Setup Request')
    prerequest_script = None
    test_script       = None
    for ev in req_item.get('event', []):
        lines = [ln for ln in ev.get('script', {}).get('exec', []) if ln.strip()]
        if ev.get('listen') == 'prerequest' and lines:
            prerequest_script = lines
        elif ev.get('listen') == 'test' and lines:
            test_script = lines
    return {
        'name': name,
        'prerequest_script': prerequest_script,
        'test_script': test_script,
        'request': {
            'method':  method,
            'url':     raw_url,
            'headers': headers,
            'body':    body,
        },
    }


def _build_manual_setup_items_map(all_sampler_paths: list,
                                  manual_prereq_path: Path) -> dict:
    """Build slug → list[setup_item] from manual_prerequisites.json.

    For each API slug that has a non-empty endpoint list in that file,
    looks up matching Postman requests in the sampler collections and
    converts them to setup_item dicts ready for _build_setup_prerequest_js.

    Returns {} when the file is missing or all lists are empty.
    """
    if not manual_prereq_path.exists():
        return {}
    text = manual_prereq_path.read_text(encoding='utf-8').strip()
    if not text:
        print('  ⚠️  manual_prerequisites.json is empty — skipped')
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        print(f'  ⚠️  manual_prerequisites.json invalid JSON: {e} — skipped')
        return {}
    if not isinstance(raw, dict):
        print(f'  ⚠️  manual_prerequisites.json is not a dict — skipped')
        return {}

    # Build endpoint-path → Postman request-item map from all samplers
    endpoint_to_req: dict = {}
    for sp in all_sampler_paths:
        if not sp.exists():
            continue
        # ── SoapUI XML ────────────────────────────────────────────────────
        if sp.suffix == '.xml':
            soap_items = parse_soapui_xml(sp)
            for it in soap_items:
                req = it.get('request', {})
                url_obj = req.get('url', {})
                r_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
                if r_url and '://' in r_url:
                    ep = urlparse(r_url).path.rstrip('/')
                    if ep and ep not in endpoint_to_req:
                        endpoint_to_req[ep] = it
            continue
        # ── Postman JSON ──────────────────────────────────────────────────
        with open(sp, encoding='utf-8') as f:
            col = json.load(f)
        def _collect(items):
            for it in items:
                if 'item' in it:
                    _collect(it['item'])
                else:
                    req = it.get('request', {})
                    url_obj = req.get('url', {})
                    r_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
                    if r_url and '://' in r_url:
                        ep = urlparse(r_url).path.rstrip('/')
                        if ep and ep not in endpoint_to_req:
                            endpoint_to_req[ep] = it
        _collect(col.get('item', []))

    result: dict = {}
    for raw_key, endpoints in raw.items():
        if raw_key.startswith('_') or not isinstance(endpoints, list) or not endpoints:
            continue
        slug = _kebab_to_camel(raw_key)
        setup_items: list = []
        for ep in endpoints:
            ri = endpoint_to_req.get(ep)
            if not ri:
                # Fallback: match by last URL path segment
                ep_last = ep.rstrip('/').rsplit('/', 1)[-1]
                for k, v in endpoint_to_req.items():
                    if k.rstrip('/').rsplit('/', 1)[-1] == ep_last:
                        ri = v
                        break
            if ri:
                setup_items.append(_build_setup_item_from_raw(ri))
            else:
                print(f'  ⚠️  manual_prerequisites: "{ep}" not found in sampler — skipped')
        if setup_items:
            result[slug] = setup_items
            names = [si['name'] for si in setup_items]
            print(f'  ✅  manual_prerequisites: {slug} → {len(setup_items)} setup step(s): {names}')

    return result


def _load_known_enums() -> dict:
    """Load baseline/known_enums.json — common field enums learned from docs."""
    if not KNOWN_ENUMS.exists():
        return {}
    with open(KNOWN_ENUMS, encoding="utf-8") as f:
        data = json.load(f)
    # Strip _comment keys
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}

_KNOWN_ENUMS = _load_known_enums()


def load_contracts() -> dict:
    if not CONTRACTS.exists():
        print(f"❌  contracts_from_html.json not found: {CONTRACTS}")
        print("   → Chạy parse_html_docs.py trước (Bước 3).")
        sys.exit(1)
    with open(CONTRACTS, encoding="utf-8") as f:
        return json.load(f)


# ── Header normalization: loaded from baseline/project_config.json ────
_HEADER_SKIP = _gen._HEADER_SKIP
_HEADER_VARS = _gen._HEADER_VARS


def _normalize_sampler_headers(raw_headers: dict) -> list:
    """Convert raw sampler headers dict → Postman header list with variables.

    * Drops Cookie (session artifact).
    * Replaces hardcoded secrets/tokens with env-variable placeholders.
    * Ensures Content-Type is always present.
    """
    result = []
    has_content_type = False
    for key, val in raw_headers.items():
        k_lower = key.lower()
        if k_lower in _HEADER_SKIP:
            continue
        new_val = _HEADER_VARS.get(k_lower, val)
        result.append({'key': key, 'value': new_val})
        if k_lower == 'content-type':
            has_content_type = True
    if not has_content_type:
        result.insert(0, {'key': 'Content-Type', 'value': 'application/json'})
    return result


# ---------------------------------------------------------------------------
#  Helpers: walk nested body → field_map + value lookup
# ---------------------------------------------------------------------------

def _walk_leaves(obj, prefix=''):
    """Yield (dot_path, value) for every leaf (non-dict, non-list-of-dicts) in a nested dict."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f'{prefix}.{k}' if prefix else k
            if isinstance(v, dict):
                yield from _walk_leaves(v, path)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                # Array of objects — walk first item as representative
                yield (path, v)  # also yield the array itself
                yield from _walk_leaves(v[0], path)
            else:
                yield (path, v)


def _build_field_map_from_body(sampler_body: dict, field_names: list) -> dict:
    """Build {flat_field_name: dot.path} by matching active_request_field names
    to leaf keys in the nested sampler body."""
    # Build a lookup: leaf_key → full_dot_path (last occurrence wins for dupes)
    leaf_index = {}  # key_name → [dot_path, ...]
    for dot_path, _ in _walk_leaves(sampler_body):
        leaf_key = dot_path.rsplit('.', 1)[-1]
        leaf_index.setdefault(leaf_key, []).append(dot_path)

    field_map = {}
    for name in field_names:
        # Try exact match first
        if name in leaf_index:
            field_map[name] = leaf_index[name][0]  # first occurrence
        # Also handle dotted field names like "property.type" — map to deepest match
        elif '.' in name:
            leaf = name.rsplit('.', 1)[-1]
            if leaf in leaf_index:
                field_map[name] = leaf_index[leaf][0]
    return field_map


def _nested_lookup(obj, key):
    """DFS search for *key* in nested dict/list, return its value or _SENTINEL."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = _nested_lookup(v, key)
            if result is not _SENTINEL:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _nested_lookup(item, key)
            if result is not _SENTINEL:
                return result
    return _SENTINEL

_SENTINEL = object()  # unique marker for "not found"


def _is_nested_body(body: dict) -> bool:
    """Return True if the body has nested dicts/lists (not a flat key-value map)."""
    return any(isinstance(v, (dict, list)) for v in body.values()) if body else False


def _infer_http_status(code: str, http_map: dict) -> int:
    """Infer HTTP status from an error code using the configured map and patterns."""
    if code in http_map:
        return http_map[code]
    for pat in _gen._HTTP_STATUS_PATTERNS:
        if pat["pattern"] in code:
            return pat["status"]
    return 400


def _build_generic_api_def(slug: str, contract: dict, use_doc_method: bool,
                           sampler_meta: Optional[dict] = None,
                           manual_setup_items_map: Optional[dict] = None) -> dict:
    """
    Build a minimal api_def dict from contract data alone.
    Used for APIs not in the hardcoded base_apis list.
    sampler_meta: output of read_sampler_metadata() — used to auto-inherit
                  per-request pre-request scripts when 'sampler_prerequest'
                  is not declared in the contract.
    manual_setup_items_map: output of _build_manual_setup_items_map() — used
                  as fallback when 'sampler_setup_items' is not in the contract.
    """
    # -- Path: prefer full URL from sampler, fallback to doc_path from HTML --
    url = contract.get("url", "?")
    full_url = None
    if url and url != "?" and url.startswith("http"):
        path = urlparse(url).path
        if not use_doc_method:
            full_url = url   # preserve original domain from sampler
    else:
        doc_path = contract.get("doc_path", "")
        # doc_path is the last segment only (e.g. /get-my-api).
        # Prefix with a generic base path; user can override BASE_URL env.
        path = doc_path if doc_path else f"/{slug}"

    raw_method = contract.get("doc_method", "GET") if use_doc_method else contract.get("method", "POST")
    # Fallback: if no sampler entry exists for this API, method == '?' — use doc_method instead
    method = contract.get("doc_method", "POST") if raw_method == "?" else raw_method

    # -- Build request_body --
    sam_body = contract.get("sampler_body", {})  # actual values from sampler
    nested = _is_nested_body(sam_body)
    request_body = {}

    # Build doc field lookup for type/mandatory/enum enrichment
    _doc_field_lookup = {}
    for field in contract.get("active_request_fields", []):
        _doc_field_lookup[field["name"]] = field

    if not use_doc_method and sam_body and isinstance(sam_body, dict):
        # ── CORRECTED variant: use sampler body as source of truth ──
        # Each leaf field in sampler body → request_body entry,
        # enriched with type/mandatory/enum from doc fields where available.
        for dot_path, val in _walk_leaves(sam_body):
            leaf_key = dot_path.rsplit('.', 1)[-1] if '.' in dot_path else dot_path
            # Look up doc field metadata (try full dot_path first, then leaf key)
            doc_field = _doc_field_lookup.get(dot_path) or _doc_field_lookup.get(leaf_key) or {}
            ftype = doc_field.get("type", "String")
            mandatory = doc_field.get("mandatory", "N").upper() == "Y"
            enum_values = doc_field.get("enum_values", [])
            # Determine example value: use sampler value directly
            if isinstance(val, (dict, list)):
                example = val
            elif isinstance(val, str) and val.startswith('{{'):
                example = val  # preserve Postman variable references
            elif val is not None:
                example = str(val) if not isinstance(val, str) else val
            else:
                example = None
            request_body[leaf_key] = {"value": example, "mandatory": mandatory,
                                      "type": ftype, "enum_values": enum_values}
    else:
        # ── DOC_LITERAL variant: use active_request_fields from doc ──
        for field in contract.get("active_request_fields", []):
            name = field["name"]
            ftype = field.get("type", "String")
            mandatory = field.get("mandatory", "N").upper() == "Y"
            note = field.get("note", "") or ""

            # For Object/Array types: parse JSON from note to get the actual structure
            if ftype in ("Object", "Array") and note:
                try:
                    example = json.loads(note)
                except (json.JSONDecodeError, ValueError):
                    example = note if len(note) < 80 else None
            else:
                # Use note as example value if it looks like a short literal (not a sentence)
                example = note if note and len(note) < 80 and not any(
                    kw in note.lower() for kw in _gen._VN_KEYWORD_FILTER
                ) else None
            # Sampler Postman-variable references ({{varName}}) always take priority
            _sam_var = None
            if nested:
                leaf_key = name.rsplit('.', 1)[-1] if '.' in name else name
                _sv = _nested_lookup(sam_body, leaf_key)
                if _sv is not _SENTINEL and isinstance(_sv, str) and _sv.startswith('{{'):
                    _sam_var = _sv
            elif name in sam_body and isinstance(sam_body[name], str) and sam_body[name].startswith('{{'):
                _sam_var = sam_body[name]
            if _sam_var is not None:
                example = _sam_var  # override note with {{varName}}

            # Fallback: use actual value from sampler body if note didn't give a good example
            elif example is None:
                if nested:
                    leaf_key = name.rsplit('.', 1)[-1] if '.' in name else name
                    sam_val = _nested_lookup(sam_body, leaf_key)
                    if sam_val is not _SENTINEL and sam_val not in (None, ""):
                        example = sam_val if isinstance(sam_val, (dict, list)) else str(sam_val)
                elif name in sam_body and sam_body[name] not in (None, ""):
                    example = str(sam_body[name])
            enum_values = field.get("enum_values", [])
            request_body[name] = {"value": example, "mandatory": mandatory, "type": ftype,
                                  "enum_values": enum_values}

    # -- Build api_errors from active_errors --
    common_keys = _gen._COMMON_ERROR_SKIP
    http_map = _gen._ERROR_CODE_HTTP_MAP
    api_specific = {}
    is_soap = contract.get('is_soap', False)
    for err in contract.get("active_errors", []):
        key  = err["key"]
        code = err["code"]
        if key in common_keys:
            continue
        # SOAP APIs always return HTTP 200; error is indicated in response body
        http_status = 200 if is_soap else _infer_http_status(code, http_map)
        entry = {"code": code, "http": http_status}
        desc = err.get('description', '')
        if desc:
            entry['description'] = desc
        api_specific[key] = entry

    # Enrich SOAP errors with body_mod_hint and messages_text from error samples
    if is_soap:
        for sample in contract.get('soap_error_samples', []):
            key = sample.get('normalized_key', '')
            if key and key in api_specific:
                if sample.get('empty_fields'):
                    api_specific[key]['body_mod_hint'] = {
                        f: '' for f in sample['empty_fields']
                    }
                if sample.get('messages_text'):
                    api_specific[key]['messages_text'] = sample['messages_text']

    # -- Custom headers from sampler (for corrected builds) --
    custom_headers = None
    raw_sam_h = contract.get('sampler_headers', {})
    if not use_doc_method and raw_sam_h:
        custom_headers = _normalize_sampler_headers(raw_sam_h)

    # -- Pre-request script from sampler (for corrected builds) --
    # Priority: 1) explicit 'sampler_prerequest' in contract  2) auto-extracted from
    # the matching sampler request (sampler_meta['prerequest_by_url']).
    prerequest_script = None
    if not use_doc_method:
        lines = contract.get('sampler_prerequest', [])
        if lines:
            prerequest_script = lines
        else:
            # Fallback: look up the API's own URL in the sampler's per-request scripts
            sam_prereq_map = (sampler_meta or {}).get('prerequest_by_url', {})
            if sam_prereq_map:
                contract_url = contract.get('url', '').split('?')[0].rstrip('/')
                if contract_url and contract_url in sam_prereq_map:
                    prerequest_script = sam_prereq_map[contract_url]
                    print(f"  ✅  Auto-inherited pre-request script from sampler for {contract_url.rsplit('/', 1)[-1]}")

    # -- Setup items (e.g. inquiryBankDate → callOFS → nopRutTienNHNN) --
    # Priority: 1) contract's 'sampler_setup_items'  2) manual_prerequisites.json
    setup_items = None
    extra_variables = None
    body_template = None
    if not use_doc_method:
        raw_setup = contract.get('sampler_setup_items', [])
        if raw_setup:
            setup_items = raw_setup
        elif manual_setup_items_map:
            manual_items = (manual_setup_items_map or {}).get(slug) or []
            # Also try camelCase normalisation of slug for lookup
            if not manual_items:
                manual_items = (manual_setup_items_map or {}).get(
                    _kebab_to_camel(slug), []
                )
            if manual_items:
                setup_items = manual_items
                print(f'  ✅  Auto-applied manual_prerequisites setup chain for "{slug}" '
                      f'({len(setup_items)} step(s))')
        raw_extra = contract.get('sampler_extra_variables', {})
        if raw_extra:
            extra_variables = raw_extra
        # body_template: when provided, build_default_body() returns this
        # structure instead of building a flat dict from request_body fields.
        # For corrected variant, ALWAYS use sampler body as template
        # (preserves real tested values, Postman variables like {{cifNo}}, etc.)
        raw_tmpl = contract.get('sampler_body_template')
        if raw_tmpl and isinstance(raw_tmpl, dict):
            body_template = raw_tmpl
        elif sam_body and isinstance(sam_body, dict):
            body_template = sam_body

    # body_field_map: maps flat field names → dot-paths in the body_template
    body_field_map = contract.get('sampler_body_field_map') or {}
    # Auto-build field_map from body_template when not explicitly provided
    if not body_field_map and body_template:
        field_names = [f['name'] for f in contract.get('active_request_fields', [])]
        body_field_map = _build_field_map_from_body(body_template, field_names)

    # -- Build enums from contract data --
    # 1) Collect enum_values from each request field
    enums = {}
    for field in contract.get("active_request_fields", []):
        evs = field.get("enum_values", [])
        # filter out meta-values and single-example values that aren't real enums
        real_evs = [v for v in evs if v not in ("null", "not_null")]
        if len(real_evs) >= 2:
            enums[field["name"]] = real_evs
    # 2) Merge with contract-level enums (manually enriched or from parser)
    contract_enums = contract.get("enums", {})
    for k, v in contract_enums.items():
        if k not in enums:
            enums[k] = v
        else:
            # merge unique values
            existing = set(enums[k])
            for val in v:
                if val not in existing:
                    enums[k].append(val)

    # 3) Enrich from known_enums.json — apply to fields that exist in request_body
    #    but have <2 enum values (i.e. only sample or no enum detected)
    for field_name in request_body:
        known = _KNOWN_ENUMS.get(field_name, [])
        if not known:
            continue

        if field_name in enums:
            # Already have real enums from doc → merge known values into enums dict
            existing = set(enums[field_name])
            for val in known:
                if val not in existing:
                    enums[field_name].append(val)
        else:
            # No enum from doc → use known_enums if ≥2 values
            if len(known) >= 2:
                enums[field_name] = list(known)
                print(f'    ℹ️  Known enum enriched for "{field_name}": '
                      f'{len(known)} values from known_enums.json')

        # Always sync request_body[field].enum_values with the final enums
        if field_name in enums:
            field_evs = request_body[field_name].get("enum_values", [])
            existing_evs = set(field_evs)
            for val in enums[field_name]:
                if val not in existing_evs:
                    field_evs.append(val)
                    existing_evs.add(val)
            request_body[field_name]["enum_values"] = field_evs

    # -- Use response_data_fields parsed from doc (not a copy of request fields) --
    # contracts_from_html.json stores response_data_fields parsed from the
    # "Successful Respond" section (Pattern A) or Request/Response JSON sample (Pattern B).
    # Only fall back to empty dict — never copy request fields as assumed response.
    resp_data_fields: dict = contract.get("response_data_fields") or {}

    # Auto-detect from sampler response examples if contract has no response_data_fields
    if not resp_data_fields and sampler_meta:
        resp_body_by_url = sampler_meta.get('response_body_by_url', {})
        for url, bodies in resp_body_by_url.items():
            url_tail = url.rsplit('/', 1)[-1].lower()
            slug_tail = slug.lower().replace('_', '-')
            path_tail = (path or '').rsplit('/', 1)[-1].lower()
            if url_tail in slug_tail or slug_tail in url_tail or url_tail == path_tail:
                success_body = bodies.get('success')
                if success_body and isinstance(success_body, dict):
                    from generate_outputs import _extract_fields_from_response_body
                    resp_data_fields = _extract_fields_from_response_body(success_body)
                    if resp_data_fields:
                        print(f'    ✅  Response fields tự động từ sampler ({len(resp_data_fields)}): '
                              f'{list(resp_data_fields.keys())}')
                break

    resp_array_field = None
    resp_array_item_fields = {}

    if resp_data_fields:
        print(f'    ℹ️  Response data fields ({len(resp_data_fields)}): '
              f'{list(resp_data_fields.keys())}')
    else:
        print(f'    ℹ️  Không có response data fields — sẽ hỏi người dùng khi chạy _regen_one.py')

    # Detect array response: slug contains "list", "search", "history", "insight"
    slug_lower = slug.lower()
    if any(kw in slug_lower for kw in ('list', 'search', 'history', 'insight', 'query')):
        # Guess array field name from common patterns
        for candidate in ('itemList', 'transactionList', 'list', 'items', 'data', 'records', 'results'):
            resp_array_field = candidate
            break
        # Array items likely have similar fields to response data fields
        resp_array_item_fields = dict(resp_data_fields)

    # -- SOAP / XML body detection --
    body_format = contract.get('sampler_body_format', 'json')
    body_template_xml = contract.get('sampler_body_raw_xml')

    # Auto-detect: if body_format not explicitly set to 'xml', check raw XML field
    # and also check if sampler_body is a raw XML string (not a dict)
    if body_format != 'xml':
        _raw_xml_candidate = body_template_xml or (
            sam_body if isinstance(sam_body, str) else None
        )
        if _raw_xml_candidate and detect_soap_body(_raw_xml_candidate):
            body_format = 'xml'
            body_template_xml = _raw_xml_candidate
            print(f'    ℹ️  Auto-detected SOAP/XML body for "{slug}" — body_format set to "xml"')

    if body_format == 'xml' and body_template_xml:
        print(f'    ℹ️  SOAP/XML body detected — body_format="xml"')

    return {
        "slug":                    slug,
        "method":                  method,
        "path":                    path,
        "full_url":                full_url,           # original sampler URL (incl. domain)
        "description":             f"API {slug}",
        "request_body":            request_body,
        "response_data_fields":    resp_data_fields,
        "response_array_field":    resp_array_field,
        "response_array_item_fields": resp_array_item_fields,
        "enums":                   enums,
        "api_errors":              api_specific,
        "business_rules":          contract.get('business_conditions', []),
        "custom_headers":          custom_headers,
        "prerequest_script":       prerequest_script,  # inject into POS-001
        "setup_items":             setup_items,         # pre-test setup requests
        "extra_variables":         extra_variables,     # additional collection variables
        "body_template":           body_template,       # nested body structure (JSON)
        "body_field_map":          body_field_map,      # flat name → dot-path
        "body_format":             body_format,         # 'json' or 'xml'
        "body_template_xml":       body_template_xml,   # raw SOAP XML template string
        "is_soap":                 is_soap,             # True for SOAP/XML APIs
    }


def build_api_defs_from_contracts(contracts: dict, use_doc_method: bool,
                                  sampler_meta: Optional[dict] = None,
                                  manual_setup_items_map: Optional[dict] = None) -> list:
    """
    Build ALL_APIS list from parsed HTML contracts.

    use_doc_method=True  → bản theo doc (method=GET như doc ghi, kể cả sai)
    use_doc_method=False → bản chuẩn đúng (method=POST theo thực tế)

    Struck fields (e.g. cifNo trong API3) sẽ KHÔNG được đưa vào request_body.
    Error codes lấy ĐÚNG từ HTML doc.
    """

    result = []
    for slug, contract in contracts.items():
        result.append(_build_generic_api_def(slug, contract, use_doc_method,
                                             sampler_meta=sampler_meta,
                                             manual_setup_items_map=manual_setup_items_map))

    return result


# ---------------------------------------------------------------------------
# Process & generate
# ---------------------------------------------------------------------------

def process_api(api_def: dict, root_dir: Path, sampler_meta: Optional[dict] = None,
                collection_label: str = ""):
    slug  = api_def["slug"]
    cases = generate_all_cases(api_def)
    out_dir = root_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [{slug}]  {len(cases)} TCs  → {out_dir.relative_to(ROOT_REPO)}")

    create_csv(slug, api_def, cases, out_dir / f"TestCases_{slug}.csv")
    create_collection(slug, api_def, cases, out_dir / f"{slug}_Postman_Collection.json",
                      sampler_meta=sampler_meta, collection_label=collection_label)
    metrics = create_coverage_summary(slug, api_def, cases, out_dir / "Test_Coverage_Summary.md", is_new=True)
    create_traceability_file(slug, api_def, cases, out_dir / f"API-{slug}.test-case-traceability.md")
    create_excel(slug, api_def, cases, out_dir / f"TestCases_{slug}.xlsx")
    return metrics


def _print_coverage_report(all_metrics: list, label: str) -> None:
    """Print a coverage comparison table across all APIs after generation."""
    if not all_metrics:
        return
    W = 70
    print(f"\n{'─'*W}")
    print(f"  COVERAGE REPORT — {label}")
    print(f"{'─'*W}")
    print(f"  {'API':<35} {'TCs':>5}  {'Prompt%':>8}  {'P1%':>6}  {'P1+P2%':>7}  {'HTTPcodes':>9}  Result")
    print(f"  {'─'*35} {'─'*5}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*9}  {'─'*6}")
    kpi      = KPI_TARGETS
    any_fail = False
    for m in all_metrics:
        # SOAP APIs only use HTTP 200 — threshold is 1 distinct code
        _min_http = 1 if m.get('is_soap') else kpi['min_http_status_codes']
        pass_all = (
            m['prompt_pct']   >= kpi['prompt_coverage_pct']
            and m['p1_pct']   >= kpi['min_p1_pct']
            and m['p1p2_pct'] >= kpi['min_p1p2_pct']
            and m['status_count'] >= _min_http
        )
        result = '✅ PASS' if pass_all else '❌ FAIL'
        if not pass_all:
            any_fail = True
        print(
            f"  {m['slug']:<35} {m['total_tcs']:>5}  "
            f"{m['prompt_covered']}/{m['prompt_total']} {m['prompt_pct']:>4.1f}%  "
            f"{m['p1_pct']:>5.1f}%  {m['p1p2_pct']:>6.1f}%  "
            f"{m['status_count']:>9}  {result}"
        )
    print(f"{'─'*W}")
    overall = '✅ ALL PASS' if not any_fail else '❌ SOME FAIL — xem chi tiết trong Test_Coverage_Summary.md'
    print(f"  Overall: {overall}")
    print(f"{'─'*W}\n")


def run(use_doc_method: bool, out_subdir: str, label: str,
        sampler_meta: Optional[dict] = None,
        manual_setup_items_map: Optional[dict] = None):
    contracts = load_contracts()
    apis      = build_api_defs_from_contracts(contracts, use_doc_method=use_doc_method,
                                              sampler_meta=sampler_meta,
                                              manual_setup_items_map=manual_setup_items_map)

    # For corrected builds, skip APIs that have no matching request in postman/ folder.
    # Two-check approach: direct postman scan (primary) + contract url (fallback for
    # SAMPLER_URL_TO_SLUG overrides where slug ≠ URL last-segment).
    if not use_doc_method:
        sampler_slugs = set(_load_sampler_bodies().keys())
        no_sampler = [
            a['slug'] for a in apis
            if a['slug'] not in sampler_slugs
            and contracts.get(a['slug'], {}).get('url', '?') == '?'
        ]
        if no_sampler:
            print(f"  ⚠️  Skipping corrected for APIs without sampler: {', '.join(no_sampler)}")
        apis = [a for a in apis if a['slug'] not in set(no_sampler)]

    root_dir  = ROOT_REPO / out_subdir
    root_dir.mkdir(parents=True, exist_ok=True)

    # Derive short collection label from out_subdir (e.g. "output/corrected" → "corrected")
    collection_label = out_subdir.rstrip('/').rsplit('/', 1)[-1]

    total = sum(len(generate_all_cases(a)) for a in apis)
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  Total TCs: {total}")
    print(f"  Output  : {root_dir.relative_to(ROOT_REPO)}/")
    print(f"{'='*70}")

    all_metrics = []
    for a in apis:
        m = process_api(a, root_dir, sampler_meta=sampler_meta,
                        collection_label=collection_label)
        if m:
            all_metrics.append(m)

    _print_coverage_report(all_metrics, label)


# ---------------------------------------------------------------------------
# Known sampler field discrepancies (from compare analysis)
# key = field name, value = explanation label
# ---------------------------------------------------------------------------
SAMPLER_OUTDATED_FIELDS = _gen._SAMPLER_OUTDATED_FIELDS
SAMPLER_ONLY_HEADERS = _gen._SAMPLER_ONLY_HEADERS
GEN_EXTRA_HEADERS    = _gen._GEN_EXTRA_HEADERS


def _count_postman_requests(path: Path) -> int:
    """Return the total number of leaf requests in a Postman collection file.
    Supports both .postman_collection.json and SoapUI .xml files."""
    try:
        if path.suffix == '.xml':
            items = parse_soapui_xml(path)
            return len(items)
        with open(path, encoding='utf-8') as f:
            col = json.load(f)
        def _count(items):
            n = 0
            for it in items:
                if 'item' in it:
                    n += _count(it['item'])
                else:
                    n += 1
            return n
        return _count(col.get('item', []))
    except Exception:
        return 0


def _find_sampler_path() -> Optional[Path]:
    """Auto-detect ALL collection files in postman/ (.postman_collection.json and .xml).
    Returns the collection with the most requests (most comprehensive sampler).
    Falls back to alphabetical-first for tie-breaking."""
    postman_dir = ROOT_REPO / "postman"
    if not postman_dir.is_dir():
        return None
    candidates = [p for p in sorted(postman_dir.iterdir())
                  if p.name.endswith('.postman_collection.json') or p.suffix == '.xml']
    if not candidates:
        return None
    return max(candidates, key=_count_postman_requests)


def _find_all_sampler_paths() -> list:
    """Return ALL collection files in postman/ (.postman_collection.json and .xml)."""
    postman_dir = ROOT_REPO / "postman"
    result = []
    if postman_dir.is_dir():
        for p in sorted(postman_dir.iterdir()):
            if p.name.endswith('.postman_collection.json') or p.suffix == '.xml':
                result.append(p)
    return result


def _load_sampler_bodies() -> dict:
    """Load request body fields from ALL sampler collections (first-match wins)."""
    sampler_paths = _find_all_sampler_paths()
    if not sampler_paths:
        return {}

    def _collect_requests(items):
        """Recursively collect all request items (handles any folder structure)."""
        reqs = []
        for it in items:
            if "request" in it:
                reqs.append(it)
            if "item" in it:
                reqs.extend(_collect_requests(it["item"]))
        return reqs

    def _url_path_to_slug(url_path: str) -> str:
        segment = url_path.rstrip('/').rsplit('/', 1)[-1]
        parts = segment.split('-')
        return parts[0] + ''.join(p.capitalize() for p in parts[1:])

    bodies = {}
    for sampler_path in sampler_paths:
        if not sampler_path.exists():
            continue
        with open(sampler_path, encoding="utf-8") as f:
            col = json.load(f)
        for req in _collect_requests(col.get("item", [])):
            r = req.get("request", {})
            url_obj = r.get("url", {})
            raw_url = url_obj.get("raw", "") if isinstance(url_obj, dict) else str(url_obj)
            path = raw_url
            if '://' in path:
                path = path.split('://', 1)[1]
                path = '/' + path.split('/', 1)[1] if '/' in path else path
            slug = _url_path_to_slug(path)
            if slug not in bodies:  # first-match wins
                body_raw = r.get("body", {}).get("raw", "{}")
                try:
                    bodies[slug] = set(json.loads(body_raw).keys())
                except Exception:
                    # Postman variables like {{var}} without quotes → fix & retry
                    fixed = re.sub(r'(?<!")(\{\{[^}]+\}\})(?!")', r'"\1"', body_raw)
                    try:
                        bodies[slug] = set(json.loads(fixed).keys())
                    except Exception:
                        bodies[slug] = set()
    return bodies


# ---------------------------------------------------------------------------
# Write DIFF_REPORT.md
# ---------------------------------------------------------------------------

def _build_diff_lines_for_api(slug: str, c: dict, sampler_bodies: dict,
                               sampler_label: str, now: str) -> list:
    """Build markdown lines for a single API's DIFF_REPORT."""
    lines = []
    lines += [
        f"# DIFF_REPORT — {slug}",
        "",
        f"> Auto-generated: {now}  ",
        f"> Source: `contracts_from_html.json` vs `{sampler_label}`  ",
        f"> Bản corrected: `output/corrected/{slug}/`",
        "",
        "---",
        "",
    ]
    return lines


def write_diff_report_md(contracts: dict, out_file: Path):
    """Write per-API DIFF_REPORT.md into each output/corrected/<api>/ folder.
    The `out_file` argument is kept for backward compatibility but is ignored;
    reports are written next to the collection files."""
    sampler_bodies = _load_sampler_bodies()
    sampler_name = _find_sampler_path()
    sampler_label = f"postman/{sampler_name.name}" if sampler_name else "postman/<sampler not found>"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    corrected_root = ROOT_REPO / "output" / "corrected"

    # --- Write one DIFF_REPORT.md per API ---
    written = []
    for slug, c in contracts.items():
        api_dir = corrected_root / slug
        if not api_dir.exists():
            continue
        per_api_lines = _build_diff_lines_for_api(slug, c, sampler_bodies, sampler_label, now)
        _write_single_api_diff(slug, c, sampler_bodies, per_api_lines)
        api_report_path = api_dir / "DIFF_REPORT.md"
        api_report_path.write_text("\n".join(per_api_lines), encoding="utf-8")
        written.append(api_report_path)
        print(f"  📄  DIFF_REPORT.md → output/corrected/{slug}/DIFF_REPORT.md")
    return


def _write_single_api_diff(slug: str, c: dict, sampler_bodies: dict, lines: list):
    """Append all diff content for one API into `lines` in-place."""
    struck_set = set(c.get("struck_request_fields", []))
    doc_fields = {f["name"] for f in c["active_request_fields"]}
    sampler_fields = sampler_bodies.get(slug, set())
    outdated_map = SAMPLER_OUTDATED_FIELDS.get(slug, {})
    only_doc = doc_fields - sampler_fields

    def _http_from_code(code: str) -> int:
        if "401" in code: return 401
        if "403" in code: return 403
        if "408" in code: return 408
        if "500" in code: return 500
        if ".000" in code: return 404
        return 400

    # Endpoint Path
    doc_path    = c.get("doc_path", "").rstrip("/") or f"/{slug}"
    sampler_url = c.get("url", "")
    sam_path    = ("/" + sampler_url.split("/", 3)[-1].rstrip("/")) if sampler_url else "?"
    # Compare only last path segment (doc_path is usually just the endpoint name)
    doc_last = doc_path.rstrip("/").rstrip("()").rsplit("/", 1)[-1].lower()
    sam_last = sam_path.rstrip("/").rsplit("/", 1)[-1].lower()
    path_is_doc_error = bool(sampler_url) and doc_last != sam_last

    lines += ["## Endpoint Path", ""]
    if path_is_doc_error:
        lines += [
            f"> ❌ **`[DOC ERROR]`** — Tài liệu khai báo endpoint `{doc_path}` nhưng sampler dùng `.../{sam_last}`.",
            f"> → Cần sửa tài liệu hoặc xác nhận lại với team backend.",
            "",
            "| Nguồn | Endpoint | Ghi chú |",
            "|-------|----------|---------|",
            f"| Tài liệu Confluence | `{doc_path}` | ❌ Khác sampler |",
            f"| Collection mẫu (sampler) | `{sam_path}` | ✅ Thực tế |",
            f"| `output/corrected/{slug}/` | `{sam_path}` | Dùng path từ sampler |",
        ]
    elif sampler_url:
        lines += [f"✅ Endpoint `{sam_last}` khớp giữa doc và sampler."]
    else:
        lines += [f"⚠️ Không có sampler URL — dùng doc path: `{doc_path}`"]
    lines.append("")

    # Method
    lines += ["## Phương thức (Method)", ""]
    if c["method_is_doc_error"]:
        lines += [
            f"> ❌ **`[DOC ERROR]`** — Tài liệu khai báo `{c['doc_method']}` nhưng server thực nhận `{c['method']}`.",
            f"> Collection mẫu (sampler) chứng minh method đúng là `{c['method']}`. Cần sửa tài liệu.",
            "",
            "| Nguồn | Giá trị | Ghi chú |",
            "|-------|---------|---------|",
            f"| Tài liệu Confluence | `{c['doc_method']}` | ❌ Sai |",
            f"| Collection mẫu (sampler) | `{c['method']}` | ✅ Đúng |",
            f"| `output/doc_literal/{slug}/` | `{c['doc_method']}` | Mirror doc (giữ nguyên lỗi) |",
            f"| `output/corrected/{slug}/` | `{c['method']}` | ✅ Đúng |",
        ]
    else:
        lines += [f"✅ Method `{c['method']}` khớp giữa doc và thực tế."]
    lines.append("")

    # Request fields
    lines += [
        "## Request Fields (theo doc HTML)", "",
        "> Tất cả các field dưới đây đều có **nguồn `[FROM_DOC]`** — lấy trực tiếp từ HTML Confluence.", "",
        "| Field | Level | Type | Mandatory | Nguồn | Nhãn |",
        "|-------|:-----:|------|:---------:|-------|------|",
    ]
    for f in c["active_request_fields"]:
        note_short = (f.get("note") or "")[:50]
        note_col = f" — {note_short}" if note_short else ""
        lines.append(f"| `{f['name']}` | {f['level']} | {f['type']} | {f['mandatory']} | `[FROM_DOC]` | {note_col} |")
    for fname in struck_set:
        lines.append(f"| ~~`{fname}`~~ | — | — | — | `[FROM_DOC]` | `[STRUCK]` Bị gạch bỏ trong doc |")
    lines.append("")

    # Sampler comparison
    if sampler_fields:
        lines += [
            "## So sánh Request Fields: Doc vs Collection mẫu", "",
            "| Field | Trong doc | Trong sampler | Nhãn |",
            "|-------|:---------:|:-------------:|------|",
        ]
        all_fields = sorted(doc_fields | sampler_fields | struck_set)
        for fname in all_fields:
            in_doc = "✅" if fname in doc_fields else "❌"
            in_sampler = "✅" if fname in sampler_fields else "❌"
            if fname in struck_set:
                label = "`[STRUCK]` Bị gạch bỏ trong doc"
            elif fname in outdated_map:
                label = f"`[SAMPLER OUTDATED]` {outdated_map[fname]}"
            elif fname in only_doc:
                label = "`[GEN EXTRA]` Có trong spec, sampler bỏ qua (optional)"
            else:
                label = "✅"
            lines.append(f"| `{fname}` | {in_doc} | {in_sampler} | {label} |")
        lines.append("")

    # Error codes
    lines += [
        "## Error Codes (theo doc HTML)", "",
        "> Tất cả error codes bên dưới đều có **nguồn `[FROM_DOC]`** — parse từ HTML Confluence.",
        "> Các assertions kiểm tra `code`, `message`, HTTP status mapping đều là FROM_DOC.", "",
        "| Code | Key | HTTP Status | Nguồn | Mô tả |",
        "|------|-----|:-----------:|-------|-------|",
    ]
    for e in c["active_errors"]:
        desc = e["reason"].replace("|", "\\|")[:80]
        http_s = _http_from_code(e["code"])
        lines.append(f"| `{e['code']}` | `{e['key']}` | {http_s} | `[FROM_DOC]` | {desc} |")
    if c.get("struck_errors"):
        for e in c["struck_errors"]:
            if isinstance(e, dict):
                lines.append(f"| ~~`{e['code']}`~~ | ~~`{e['key']}`~~ | — | `[FROM_DOC]` | `[STRUCK]` |")
            else:
                lines.append(f"| ~~`{e}`~~ | — | — | `[FROM_DOC]` | `[STRUCK]` |")

    # Script Assumptions
    lines += [
        "",
        "## Script Assumptions (not in doc)", "",
        "> Các mục dưới đây **KHÔNG có trong tài liệu** — script tự thêm dựa trên convention.", "",
        "| TC prefix | Validate item | Expected value | Nguồn | Lý do |",
        "|-----------|--------------|---------------|-------|-------|",
        "| BVA-001 | String max length | 400 khi > 260 chars | `[ASSUMPTION]` | Doc does not specify length limit |",
        "| EP-002~003 | Invalid partition | 400 | `[ASSUMPTION]` | Doc does not list invalid cases |",
        "| HDR-001~005 | Missing/invalid headers | 401/415/400 | `[ASSUMPTION]` | Doc does not specify |",
        "| MTH-001~004 | Wrong HTTP method → 405 | 405 | `[ASSUMPTION]` | Doc does not mention |",
        "| SCH/SEC/IDM/DAT/PER/EDG | Convention checks | varies | `[ASSUMPTION]` | REST convention / KPI |",
        "",
    ]


def diff_report(contracts: dict):
    print(f"\n{'='*70}")
    print("  SO SÁNH: bản theo doc ←→ bản chuẩn đúng")
    print(f"{'='*70}")
    for slug, c in contracts.items():
        print(f"\n  ── {slug} ──")
        if c["method_is_doc_error"]:
            print(f"  ❌ [DOC ERROR]  doc={c['doc_method']}  thực tế={c['method']}")
        struck = c.get("struck_request_fields", [])
        if struck:
            print(f"  🗑  [STRUCK] {', '.join(struck)}")
        outdated = SAMPLER_OUTDATED_FIELDS.get(slug, {})
        if outdated:
            for f, reason in outdated.items():
                print(f"  ⚠️  [SAMPLER OUTDATED] {f}: {reason}")
        print(f"  Error codes ({len(c['active_errors'])}): {', '.join(e['key'] for e in c['active_errors'])}")
    print(f"\n{'='*70}")
    print("  output/doc_literal/  → mirror doc (method=GET, kể cả sai)")
    print("  output/corrected/    → bản chuẩn đúng  +  DIFF_REPORT.md")
    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    contracts = load_contracts()

    # Auto-discover sampler — pick the most comprehensive collection (most requests)
    # Supports both .postman_collection.json and SoapUI .xml files
    SAMPLER_PATH = None
    all_sampler_paths = []
    postman_dir = ROOT_REPO / "postman"
    if postman_dir.is_dir():
        candidates = [p for p in sorted(postman_dir.iterdir())
                      if p.name.endswith('.postman_collection.json') or p.suffix == '.xml']
        all_sampler_paths = candidates
        if candidates:
            SAMPLER_PATH = max(candidates, key=_count_postman_requests)
    SAMPLER_EXISTS = SAMPLER_PATH is not None and SAMPLER_PATH.exists()
    if SAMPLER_EXISTS:
        print(f"  ℹ️  Sampler: {SAMPLER_PATH.name}")

    # --- Read collection-level variables, auth & per-request prereq scripts from sampler ---
    sampler_meta = read_sampler_metadata(SAMPLER_PATH) if SAMPLER_EXISTS else None

    # --- Build setup-items map from manual_prerequisites.json (direct read, skips parse) ---
    _manual_prereq_path = ROOT_REPO / "baseline" / "manual_prerequisites.json"
    manual_setup_items_map = _build_manual_setup_items_map(
        all_sampler_paths, _manual_prereq_path
    ) if SAMPLER_EXISTS else {}

    if not SAMPLER_EXISTS:
        print("\n⚠️  Không tìm thấy collection mẫu (sampler):")
        print(f"   {SAMPLER_PATH}")
        print("   → Chỉ sinh output/doc_literal/. Bỏ qua output/corrected/ và DIFF_REPORT.md.\n")

    # 1. Bản theo doc (method = GET như doc ghi — DOC ERROR được preserve để dễ so sánh)
    run(
        use_doc_method=True,
        out_subdir="output/doc_literal",
        label="Bản THEO DOC (method=GET, kể cả sai)",
        sampler_meta=sampler_meta,
    )

    if SAMPLER_EXISTS:
        # 2. Bản chuẩn đúng (method = POST, struck fields removed, error codes từ doc)
        run(
            use_doc_method=False,
            out_subdir="output/corrected",
            label="Bản CHUẨN ĐÚNG (method=POST, struck fields loại bỏ, error codes doc)",
            sampler_meta=sampler_meta,
            manual_setup_items_map=manual_setup_items_map,
        )

        # 3. Auto-generate per-API DIFF_REPORT.md vào output/corrected/<api>/
        write_diff_report_md(contracts, ROOT_REPO / "output" / "corrected" / "DIFF_REPORT.md")  # out_file ignored, writes per-API

    # 4. Terminal summary
    diff_report(contracts)
