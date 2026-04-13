#!/usr/bin/env python3
"""
Refresh _available_request_names in baseline/manual_prerequisites.json.

Quét toàn bộ collections trong postman/ (hỗ trợ cả Postman JSON
và SoapUI XML) → trích xuất endpoint (URL path) của mỗi request
→ ghi lại vào config.

Usage:
    python3 scripts/refresh_prerequisites.py
"""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

# SoapUI XML parser (shared utility)
try:
    from soapui_parser import parse_soapui_xml
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from soapui_parser import parse_soapui_xml

ROOT     = Path(__file__).resolve().parent.parent
POSTMAN  = ROOT / 'postman'
BASELINE = ROOT / 'baseline'
CONFIG   = BASELINE / 'manual_prerequisites.json'


def _collect_all_requests(items: list) -> list:
    """Recursively collect all request items from Postman folder tree."""
    reqs = []
    for it in items:
        if 'request' in it:
            reqs.append(it)
        if 'item' in it:
            reqs.extend(_collect_all_requests(it['item']))
    return reqs


def _extract_endpoint(raw_url: str) -> str:
    """Extract the URL path (endpoint) from a raw Postman URL.
    Handles both full URLs and {{baseURL}}-style templates."""
    if not raw_url:
        return ''
    # Strip Postman variable prefix like {{baseURL}}
    cleaned = re.sub(r'\{\{[^}]+\}\}', '', raw_url).lstrip('/')
    if cleaned:
        cleaned = '/' + cleaned
    # Try parsing as regular URL
    parsed = urlparse(raw_url)
    if parsed.scheme and parsed.path:
        return parsed.path
    # Fallback: use cleaned path
    return cleaned or raw_url


def _endpoint_to_slug(endpoint: str) -> str:
    """Convert endpoint path to camelCase slug.
    '/int-esb/open-saving-service/v1/open-flex-saving' → 'openFlexSaving'
    '/esbuat7801/accountService/v1/callOFS'             → 'callOFS'
    """
    # Take the last path segment
    last = endpoint.rstrip('/').rsplit('/', 1)[-1]
    # kebab-case → camelCase
    parts = re.split(r'[-_]+', last)
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def refresh():
    """Scan all Postman JSON & SoapUI XML collections and update _available_request_names."""
    json_files = sorted(POSTMAN.glob('*.postman_collection.json'))
    xml_files  = sorted(POSTMAN.glob('*.xml'))
    sampler_files = json_files  # keep for backward compat

    if not json_files and not xml_files:
        print('⚠️  Không tìm thấy file collection nào trong postman/ (.postman_collection.json hoặc .xml)')
        return

    # Collect: endpoint → request name (for display)
    endpoints = {}  # endpoint → request_name

    # ── 1. Postman JSON collections ──────────────────────────────────────
    for sf in json_files:
        with open(sf, encoding='utf-8') as f:
            col = json.load(f)
        for ri in _collect_all_requests(col.get('item', [])):
            r = ri.get('request', {})
            url_obj = r.get('url', {})
            raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
            endpoint = _extract_endpoint(raw_url)
            name = ri.get('name', '')
            if endpoint and endpoint not in endpoints:
                endpoints[endpoint] = name

    # ── 2. SoapUI XML collections ────────────────────────────────────────
    for xf in xml_files:
        soap_items = parse_soapui_xml(xf)
        for ri in soap_items:
            r = ri.get('request', {})
            url_obj = r.get('url', {})
            raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
            endpoint = _extract_endpoint(raw_url)
            name = ri.get('name', '')
            if endpoint and endpoint not in endpoints:
                endpoints[endpoint] = name

    # Sort by endpoint
    sorted_endpoints = sorted(endpoints.keys())

    # Load or create config
    _default_cfg = {
        "_comment": "Khai báo thủ công API trung gian (prerequisite) cho từng API. "
                    "Key = tên API (kebab-case hoặc camelCase đều được). "
                    "Value = danh sách endpoint cần chạy trước (theo thứ tự)."
    }
    if CONFIG.exists():
        text = CONFIG.read_text(encoding='utf-8').strip()
        if text:
            try:
                cfg = json.loads(text)
                if not isinstance(cfg, dict):
                    print('⚠️  manual_prerequisites.json is not a dict — resetting')
                    cfg = dict(_default_cfg)
            except json.JSONDecodeError as e:
                print(f'⚠️  manual_prerequisites.json invalid JSON: {e} — resetting')
                cfg = dict(_default_cfg)
        else:
            print('⚠️  manual_prerequisites.json is empty — resetting')
            cfg = dict(_default_cfg)
    else:
        cfg = dict(_default_cfg)

    old_list = cfg.get('_available_request_names', [])
    cfg['_available_request_names'] = sorted_endpoints

    # ── Auto-generate camelCase slug keys for all endpoints ───────────────
    # Collect existing user-defined keys (normalised lowercase for matching)
    existing_norm = set()
    for k in cfg:
        if k.startswith('_'):
            continue
        existing_norm.add(k.lower().replace('-', '').replace('_', '').replace('(', '').replace(')', ''))

    # Generate stub entries for endpoints not yet configured
    new_slugs = []
    for ep in sorted_endpoints:
        slug = _endpoint_to_slug(ep)
        norm = slug.lower()
        if norm not in existing_norm:
            cfg[slug] = []
            existing_norm.add(norm)
            new_slugs.append(slug)

    # Write back
    with open(CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write('\n')

    # Report
    added = set(sorted_endpoints) - set(old_list)
    removed = set(old_list) - set(sorted_endpoints)

    print(f'✅  Scanned {len(json_files)} JSON + {len(xml_files)} XML collection(s), found {len(sorted_endpoints)} endpoint(s)')
    print(f'    Config: {CONFIG.relative_to(ROOT)}')

    # Show available endpoints
    print(f'\n📋  Available endpoints (_available_request_names):')
    for ep in sorted_endpoints:
        slug = _endpoint_to_slug(ep)
        tag = '  🆕' if ep in added else ''
        print(f'  {ep:60s}  → key: {slug}{tag}')
    if removed:
        print(f'\n  🗑️  Removed {len(removed)} stale endpoint(s):')
        for ep in sorted(removed):
            print(f'    - {ep}')

    # Show API key stubs
    if new_slugs:
        print(f'\n🔑  Auto-generated {len(new_slugs)} new API key(s) (fill in prerequisites):')
        for s in new_slugs:
            print(f'  "{s}": []')

    # Show existing configured prerequisites
    configured = {k: v for k, v in cfg.items()
                  if not k.startswith('_') and isinstance(v, list) and v}
    if configured:
        print(f'\n✅  Configured prerequisites ({len(configured)}):')
        for k, v in configured.items():
            short = [ep.rsplit('/', 1)[-1] for ep in v]
            print(f'  "{k}": {short}')

    unconfigured = {k: v for k, v in cfg.items()
                    if not k.startswith('_') and isinstance(v, list) and not v}
    if unconfigured:
        print(f'\n⏳  Chưa cấu hình ({len(unconfigured)}):')
        for k in sorted(unconfigured):
            print(f'  "{k}": []  ← thêm endpoint prereq tại đây')

    print(f'\n👉  Mở {CONFIG.name} và điền endpoint prereq cho các API cần thiết.')


if __name__ == '__main__':
    refresh()
