#!/usr/bin/env python3
"""
Merge all Postman collections into ONE master collection and replace
hardcoded values with environment variable references ({{var}}).

Output:
  - master_collection.json  (single collection for import into Postman)
  - Designed to be used with UAT_Environment.postman_environment.json

Usage:
    python3 merge_all_collections.py
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'output'))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.normpath(os.path.join(SCRIPT_DIR, '..'))

# Import sampler metadata reader from generate_outputs
sys.path.insert(0, SCRIPT_DIR)
from generate_outputs import read_sampler_metadata

# Known group descriptions (for labelling). Unknown groups get auto-description.
_KNOWN_GROUPS = {
    'corrected':   'Bản chuẩn đúng (POST, struck fields removed)',
    'doc_literal': 'Bản theo doc (GET — giữ nguyên lỗi để so sánh)',
}


def _discover_groups() -> dict:
    """Auto-discover output groups from subdirectories that contain collections."""
    groups = {}
    if not os.path.isdir(ROOT):
        return groups
    for entry in sorted(os.scandir(ROOT), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        name = entry.name
        # Skip hidden dirs and files like master_collection.json
        if name.startswith('.'):
            continue
        desc = _KNOWN_GROUPS.get(name, f'Group: {name}')
        groups[name] = desc
    return groups


def _find_sampler() -> Optional[str]:
    """Auto-detect the first .postman_collection.json in postman/ folder."""
    postman_dir = os.path.join(REPO_ROOT, 'postman')
    if not os.path.isdir(postman_dir):
        return None
    for fname in sorted(os.listdir(postman_dir)):
        if fname.endswith('.postman_collection.json'):
            return os.path.join(postman_dir, fname)
    return None

# Map of hardcoded values → env variable names
# Only generic patterns here; real values are auto-loaded from sampler at runtime.
ENV_REPLACEMENTS = {}


def _build_env_replacements() -> dict:
    """Auto-build ENV_REPLACEMENTS from sampler collection body values.

    Scans the sampler for hardcoded URLs, body values, and header values,
    then maps them to environment variable placeholders.
    """
    replacements = {}
    sampler_path = _find_sampler()
    if not sampler_path:
        return replacements
    try:
        with open(sampler_path, 'r', encoding='utf-8') as f:
            col = json.load(f)
    except Exception:
        return replacements

    def _collect_requests(items):
        reqs = []
        for it in items:
            if 'request' in it:
                reqs.append(it)
            if 'item' in it:
                reqs.extend(_collect_requests(it['item']))
        return reqs

    for req_item in _collect_requests(col.get('item', [])):
        r = req_item.get('request', {})
        # Extract URL host → {{baseURL}}
        url_obj = r.get('url', {})
        raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
        if raw_url:
            m = re.match(r'(https?://[^/]+)', raw_url)
            if m:
                replacements[m.group(1)] = '{{baseURL}}'
        # Extract header values for apikey-like headers
        for h in r.get('header', []):
            k = h.get('key', '').lower()
            v = h.get('value', '')
            if not v or v.startswith('{{'):
                continue
            if k in ('apikey', 'x-api-key'):
                replacements[v] = '{{apiKey}}'
            elif k == 'sessionid':
                replacements[v] = '{{sessionId}}'

    return replacements

# Also replace in header values
HEADER_VAR_MAP = {
    "apiKey": "{{apiKey}}",
    "apikey": "{{apiKey}}",
}


def find_collections_in(group_dir: str) -> list:
    """Find all *_Postman_Collection*.json directly in <group_dir>/<api>/ subfolders."""
    raw = {}
    if not os.path.isdir(group_dir):
        return []
    for entry in os.scandir(group_dir):
        if not entry.is_dir():
            continue
        for fname in os.listdir(entry.path):
            m = re.match(r'(.+)_Postman_Collection(?:_v(\d+))?\.json$', fname)
            if m:
                full = os.path.join(entry.path, fname)
                api  = entry.name
                ver  = int(m.group(2)) if m.group(2) else 0
                if api not in raw or ver > raw[api][0]:
                    raw[api] = (ver, api, full)
    return sorted((api, full) for _, api, full in raw.values())


def merge_all():
    global ENV_REPLACEMENTS
    ENV_REPLACEMENTS = _build_env_replacements()
    now = datetime.now(timezone.utc).isoformat()
    group_folders = []
    total_requests = 0

    GROUPS = _discover_groups()
    if not GROUPS:
        print("⚠️  Không tìm thấy group nào trong output/. Chạy regen_from_contracts.py trước.")
        return

    # --- Read collection-level vars & auth from sampler ---
    sampler_path = _find_sampler()
    sampler_meta = read_sampler_metadata(sampler_path) if sampler_path else None

    for group_name, group_desc in GROUPS.items():
        group_dir = os.path.join(ROOT, group_name)
        collections = find_collections_in(group_dir)
        if not collections:
            print(f"  [SKIP] {group_name} — no collections found")
            continue

        api_items = []
        group_reqs = 0
        for api_name, full_path in collections:
            col = load_and_process(full_path)
            if col is None:
                continue
            reqs = sum(len(cat.get('item', [])) for cat in col.get('item', []))
            group_reqs += reqs
            api_items.append({
                "name": api_name,
                "item": col.get('item', []),
            })
            print(f"  [{group_name} / {api_name}] {reqs} requests")

        total_requests += group_reqs
        group_folders.append({
            "name": group_name,
            "description": group_desc,
            "item": api_items,
        })

    # --- Build master with inherited variables & auth ---
    variables = (sampler_meta or {}).get("variable", [])
    auth_obj  = (sampler_meta or {}).get("auth")

    master = {
        "info": {
            "_postman_id": "master-collection-all-apis",
            "name": "[master] All APIs — Merged Collection",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": (
                f"2 groups: corrected (chuẩn đúng) + doc_literal (mirror doc).\n"
                f"{total_requests} total requests. Generated at {now}\n\n"
                "Variables inherited from sampler collection."
            ),
        },
        "item": group_folders,
        "event": [],
        "variable": variables,
    }
    if auth_obj:
        master["auth"] = auth_obj

    output_path = os.path.join(ROOT, "master_collection.json")
    import sys
    for i, arg in enumerate(sys.argv):
        if arg == '--output' and i + 1 < len(sys.argv):
            output_path = sys.argv[i + 1]
            break

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(master, f, indent=2, ensure_ascii=False)

    # --- Discover environment file ---
    env_file = None
    env_dir = os.path.join(REPO_ROOT, 'postman')
    if os.path.isdir(env_dir):
        for fname in sorted(os.listdir(env_dir)):
            if fname.endswith('.postman_environment.json'):
                env_file = os.path.join(env_dir, fname)
                break
    env_label = os.path.basename(env_file) if env_file else "<no environment file found>"

    print(f"\n{'='*60}")
    print(f"  Master collection: {output_path}")
    print(f"  {len(group_folders)} groups, {total_requests} total requests")
    print(f"  Environment: {env_label}")
    print(f"{'='*60}")
    env_arg = f" -e postman/{os.path.basename(env_file)}" if env_file else ""
    print(f"\nNewman command:")
    print(f"  newman run {output_path}{env_arg}")


def replace_env_vars_in_string(s: str) -> str:
    """Replace hardcoded values with {{env_var}} placeholders in a string."""
    if not isinstance(s, str):
        return s
    for literal, var in ENV_REPLACEMENTS.items():
        s = s.replace(literal, var)
    return s


def replace_env_vars_in_obj(obj):
    """Recursively replace hardcoded values with env vars throughout JSON."""
    if isinstance(obj, str):
        return replace_env_vars_in_string(obj)
    elif isinstance(obj, list):
        return [replace_env_vars_in_obj(item) for item in obj]
    elif isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            new[k] = replace_env_vars_in_obj(v)
        return new
    return obj


def patch_headers(headers: list) -> list:
    """Replace apiKey header value with env var reference."""
    for h in headers:
        key = h.get('key', '').lower()
        if key in ('apikey',):
            h['value'] = '{{apiKey}}'
        elif key == 'channel':
            # Keep as-is or use var if desired
            pass
    return headers


def patch_url(url_obj):
    """Replace host/path in URL with {{baseURL}} reference."""
    if isinstance(url_obj, dict):
        raw = url_obj.get('raw', '')
        url_obj['raw'] = replace_env_vars_in_string(raw)
        # Update host to reference baseURL
        if 'host' in url_obj:
            url_obj['host'] = ['{{baseURL}}']
            url_obj.pop('protocol', None)
        # Update query param values
        if 'query' in url_obj:
            for q in url_obj['query']:
                q['value'] = replace_env_vars_in_string(q.get('value', ''))
        # Fix raw to use {{baseURL}}
        if '{{baseURL}}' in url_obj.get('raw', ''):
            # Ensure raw uses {{baseURL}} prefix (without protocol)
            raw = url_obj['raw']
            # Replace full URL pattern
            raw = re.sub(r'https?://[^/]+', '{{baseURL}}', raw)
            url_obj['raw'] = raw
    return url_obj


def process_request_item(item: dict) -> dict:
    """Process a single request item: replace hardcoded values with env vars."""
    if 'request' in item:
        req = item['request']
        # Patch headers
        if 'header' in req:
            req['header'] = patch_headers(req['header'])
        # Patch URL
        if 'url' in req:
            req['url'] = patch_url(req['url'])
        # Patch body
        if 'body' in req and req['body'].get('mode') == 'raw':
            raw = req['body'].get('raw', '')
            req['body']['raw'] = replace_env_vars_in_string(raw)
    # Recurse into sub-items
    if 'item' in item:
        item['item'] = [process_request_item(sub) for sub in item['item']]
    return item


def load_and_process(path: str) -> Optional[dict]:
    """Load a collection and process all requests to use env vars."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"  [SKIP] {path}: {e}")
        return None

    # Process all items recursively
    items = data.get('item', [])
    data['item'] = [process_request_item(item) for item in items]
    return data


if __name__ == '__main__':
    merge_all()
