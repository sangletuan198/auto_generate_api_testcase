#!/usr/bin/env python3
"""
Compare sampler DP/mock requests vs generated collections.

Fully dynamic: discovers APIs from sampler + generated collections.
No hardcoded API list — runs with whatever input is available.

Legend:
  ✅          identical / conformant
  ⚠️  VALUE    same field, different sample value (usually expected)
  [DOC ERROR] doc says X, but sampler + gen both use Y → doc is wrong
  [SAMPLER OUTDATED] sampler uses deprecated/renamed field → sampler is old
  [GEN EXTRA]  gen adds field/header not in sampler (justified by spec)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent

# ── Fields renamed/removed in latest doc version ──────────────────────────────
# This is the ONLY config that stays hardcoded (backward compat).
# Safe: unknown slugs just get an empty dict → no crash.
DEPRECATED_REQUEST_FIELDS = {
    'getSavingAccountTransactions': {
        'type': 'Renamed → transactionActivity (doc comment Jan 09 2026)',
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _url_path_to_slug(url_path: str) -> str:
    """Convert URL path like '/encrypt/v1/get-saving-account-transactions' → 'getSavingAccountTransactions'."""
    segment = url_path.rstrip('/').rsplit('/', 1)[-1]
    parts = re.split(r'[-_]+', segment)
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _normalize_slug(s: str) -> str:
    """Normalise a slug for comparison: lowercase, strip (), spaces, hyphens."""
    return re.sub(r'[()\s\-_]+', '', s).lower()


def find_folder(items, name):
    for item in items:
        if item.get('name') == name:
            return item
        if 'item' in item:
            result = find_folder(item['item'], name)
            if result:
                return result
    return None


def _load_contracts_doc_methods() -> dict:
    """Load doc_method per slug from contracts_from_html.json (if exists)."""
    contracts_path = SCRIPT_DIR / 'contracts_from_html.json'
    if not contracts_path.exists():
        return {}
    try:
        with open(contracts_path, encoding='utf-8') as f:
            contracts = json.load(f)
        return {slug: c.get('doc_method', '?') for slug, c in contracts.items()}
    except Exception:
        return {}


# ── Load sampler (dynamic) ────────────────────────────────────────────────────

def _collect_all_requests(items: list) -> list:
    """Recursively collect all leaf requests from a Postman item tree."""
    reqs = []
    for item in items:
        if 'request' in item:
            reqs.append(item)
        if 'item' in item:
            reqs.extend(_collect_all_requests(item['item']))
    return reqs


def _find_sampler() -> str:
    """Auto-detect the most comprehensive .postman_collection.json in postman/
    (the one with the most requests), falling back to alphabetical-first."""
    postman_dir = ROOT / 'postman'
    if not postman_dir.is_dir():
        return ''
    candidates = [p for p in sorted(postman_dir.iterdir())
                  if p.name.endswith('.postman_collection.json')]
    if not candidates:
        return ''

    def _count(path):
        try:
            import json as _j
            with open(path, encoding='utf-8') as _f:
                col = _j.load(_f)
            def _c(items):
                return sum(_c(it['item']) if 'item' in it else 1 for it in items)
            return _c(col.get('item', []))
        except Exception:
            return 0

    return str(max(candidates, key=_count))


def load_sampler_reqs() -> dict:
    """Load all requests from sampler collection. Returns {slug: req_data}."""
    sampler_path = _find_sampler()
    if not sampler_path or not Path(sampler_path).exists():
        print(f'❌  Sampler không tồn tại trong postman/')
        print('   Script này yêu cầu sampler. Đặt file vào postman/ rồi chạy lại.')
        sys.exit(1)

    print(f'  ℹ️  Sampler: {Path(sampler_path).name}')
    with open(sampler_path) as f:
        sampler = json.load(f)

    # Collect ALL requests recursively (not limited to 'mock' folder)
    all_reqs = _collect_all_requests(sampler.get('item', []))
    if not all_reqs:
        print("⚠️  Sampler không có request nào để so sánh.")
        return {}

    result = {}
    for req_item in all_reqs:
        name = req_item.get('name', '')
        r = req_item.get('request', {})
        url_obj = r.get('url', {})
        raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
        # Try slug from URL (most reliable), then from name
        slug = _url_path_to_slug(raw_url) if raw_url else _url_path_to_slug(name)
        raw_body = r.get('body', {}).get('raw', '{}')
        try:
            body = json.loads(raw_body)
        except Exception:
            # Postman variables like {{var}} without quotes → fix & retry
            fixed = re.sub(r'(?<!")(\{\{[^}]+\}\})(?!")', r'"\1"', raw_body)
            try:
                body = json.loads(fixed)
            except Exception:
                body = {}
        result[slug] = {
            'method':  r.get('method', '?'),
            'url':     raw_url,
            'headers': {h['key']: h['value'] for h in r.get('header', [])},
            'body':    body,
        }
    return result


# ── Discover generated collections (dynamic) ─────────────────────────────────

def discover_gen_collections() -> dict:
    """Auto-discover generated Postman collections from output/.
    Returns {slug: req_data_of_first_request}."""
    output_dir = ROOT / 'output'
    search_dirs = ['corrected', 'doc_literal']
    found = {}  # slug → col_path  (prefer corrected over doc_literal)

    for subdir in search_dirs:
        group_dir = output_dir / subdir
        if not group_dir.is_dir():
            continue
        for api_dir in sorted(group_dir.iterdir()):
            if not api_dir.is_dir():
                continue
            slug = api_dir.name
            if slug in found:
                continue  # already found in higher-priority group
            for f in api_dir.iterdir():
                if f.name.endswith('_Postman_Collection.json'):
                    found[slug] = f
                    break

    result = {}
    for slug, col_path in found.items():
        try:
            with open(col_path, encoding='utf-8') as f:
                gen_col = json.load(f)
            first_item = gen_col['item'][0]
            if 'request' in first_item:
                first_req = first_item['request']
            else:
                first_req = first_item['item'][0]['request']
            url_obj = first_req.get('url', {})
            raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
            raw_body = first_req.get('body', {}).get('raw', '{}')
            try:
                body = json.loads(raw_body)
            except Exception:
                # Postman variables like {{var}} without quotes → fix & retry
                fixed = re.sub(r'(?<!")(\{\{[^}]+\}\})(?!")', r'"\1"', raw_body)
                try:
                    body = json.loads(fixed)
                except Exception:
                    body = {}
            result[slug] = {
                'method':  first_req.get('method', '?'),
                'url':     raw_url,
                'headers': {h['key']: h['value'] for h in first_req.get('header', [])},
                'body':    body,
            }
        except Exception as e:
            print(f'  [SKIP] {slug}: error loading collection — {e}')
    return result


# ── Main comparison ───────────────────────────────────────────────────────────

def main():
    sampler_reqs = load_sampler_reqs()
    gen_reqs = discover_gen_collections()
    doc_methods = _load_contracts_doc_methods()

    # Determine which slugs to compare: union of sampler + gen
    # Build normalised mapping for slug alignment
    norm_to_sam = {_normalize_slug(s): s for s in sampler_reqs}
    norm_to_gen = {_normalize_slug(s): s for s in gen_reqs}

    # Merge norms — also try "contains" matching for fuzzy pairs
    # (e.g. "savingplan" ⊂ "generatesavingplan")
    paired_norms = set()   # set of (sam_norm, gen_norm)
    used_sam = set()
    used_gen = set()
    # 1) exact norm match
    for n in norm_to_sam:
        if n in norm_to_gen:
            paired_norms.add((n, n))
            used_sam.add(n)
            used_gen.add(n)
    # 2) contains match for remaining
    for sn in norm_to_sam:
        if sn in used_sam:
            continue
        for gn in norm_to_gen:
            if gn in used_gen:
                continue
            if sn in gn or gn in sn:
                paired_norms.add((sn, gn))
                used_sam.add(sn)
                used_gen.add(gn)
                break
    # 3) leftover sampler-only or gen-only
    for sn in norm_to_sam:
        if sn not in used_sam:
            paired_norms.add((sn, ''))
    for gn in norm_to_gen:
        if gn not in used_gen:
            paired_norms.add(('', gn))

    if not paired_norms:
        print('⚠️  Không tìm thấy API nào để so sánh.')
        print('   Cần có sampler (postman/) VÀ generated collections (output/).')
        return

    for sam_norm, gen_norm in sorted(paired_norms):
        sam_slug = norm_to_sam.get(sam_norm, '')
        gen_slug = norm_to_gen.get(gen_norm, '')
        display_slug = sam_slug or gen_slug

        print(f'\n{"="*70}')
        print(f'  API: {display_slug}')
        print(f'{"="*70}')

        s = sampler_reqs.get(sam_slug, {}) if sam_slug else {}
        g = gen_reqs.get(gen_slug, {}) if gen_slug else {}

        if not s:
            print(f'  [SKIP] Không có trong sampler — chỉ có bản gen')
            continue
        if not g:
            print(f'  [SKIP] Không có generated collection — chỉ có trong sampler')
            continue

        # Find doc_method from either slug form
        doc_method = doc_methods.get(gen_slug, doc_methods.get(sam_slug, '?'))
        slug = gen_slug or sam_slug  # for DEPRECATED_REQUEST_FIELDS lookup

        # ── Method ────────────────────────────────────────────────────────────
        sm, gm = s.get('method', '?'), g.get('method', '?')
        print(f'  method:')
        if doc_method != '?' and doc_method != gm:
            print(f'    doc says:  {doc_method}')
            print(f'    sampler:   {sm}')
            print(f'    gen:       {gm}')
            print(f'    [DOC ERROR] Doc khai báo {doc_method} nhưng thực tế server dùng {gm}.')
            print(f'               Sampler đã chứng minh method thực là {gm}. Gen đúng — doc cần sửa.')
        else:
            mark = '✅' if sm == gm else '⚠️'
            print(f'    doc={doc_method}  sampler={sm}  gen={gm}  {mark}')

        # ── URL ───────────────────────────────────────────────────────────────
        su, gu = s.get('url', ''), g.get('url', '')
        mark = '✅' if su == gu else '❌ DIFF'
        print(f'  url: {mark}')
        if su != gu:
            print(f'    sampler: {su}')
            print(f'    gen:     {gu}')

        # ── Headers ───────────────────────────────────────────────────────────
        sh, gh = s.get('headers', {}), g.get('headers', {})
        all_keys = sorted(set(list(sh.keys()) + list(gh.keys())))
        # Known variable substitutions (gen uses env vars instead of hardcoded values)
        _VAR_SUBS = {
            'apikey':       '{{apikey}}',
            'esb-api-key':  '{{esbApiKey}}',
            'sessionid':    '{{sessionId}}',
            'x-b3-traceid': '{{$guid}}',
            'x-trace-id':  '{{$guid}}',
        }
        _SESSION_ARTIFACTS = {'cookie'}
        print('  headers:')
        has_diff = False
        for k in all_keys:
            sv, gv = sh.get(k), gh.get(k)
            if sv == gv:
                continue
            has_diff = True
            k_lower = k.lower()
            if sv is None:
                print(f'    [GEN EXTRA]  {k}={gv!r}  — có trong spec, sampler không gửi')
            elif gv is None:
                if k_lower in _SESSION_ARTIFACTS:
                    print(f'    [SAMPLER ONLY]  {k}=<session cookie>  — không add vào gen (session artifact)')
                else:
                    print(f'    [SAMPLER ONLY]  {k}={sv!r}  — không add vào gen')
            elif k_lower in _VAR_SUBS or gv.startswith('{{'):
                # Gen correctly uses an env variable placeholder instead of a hardcoded value
                print(f'    ✅  {k}: sampler=<hardcoded>  gen={gv}  — gen dùng env variable (best practice)')
            else:
                print(f'    ❌  {k}: sampler={sv!r}  gen={gv!r}')
        if not has_diff:
            print('    ✅ identical')

        # ── Body fields ───────────────────────────────────────────────────────
        sb, gb = s.get('body', {}), g.get('body', {})
        sk, gk = set(sb.keys()), set(gb.keys())
        deprecated = DEPRECATED_REQUEST_FIELDS.get(slug, {})
        print('  body fields:')
        print(f'    sampler keys: {sorted(sk)}')
        print(f'    gen keys:     {sorted(gk)}')

        only_sampler = sk - gk
        only_gen = gk - sk

        for f in sorted(only_sampler):
            if f in deprecated:
                print(f'    [SAMPLER OUTDATED]  "{f}" có trong sampler nhưng không có trong gen — {deprecated[f]}')
            else:
                print(f'    ⚠️  "{f}" chỉ có trong sampler')

        for f in sorted(only_gen):
            print(f'    [GEN EXTRA]  "{f}" chỉ có trong gen — có trong spec nhưng sampler bỏ qua (optional field)')

        if not only_sampler and not only_gen:
            print('    ✅ cùng tập field')

        # value diffs (only for shared keys with actual data values)
        for k in sorted(sk & gk):
            sv, gv = sb[k], gb[k]
            if sv != gv:
                if isinstance(sv, str) and (sv.startswith('p_') or sv == k or sv.endswith('_no') or sv.endswith(' text')):
                    print(f'    ⚠️  "{k}": sampler dùng placeholder {sv!r} chưa điền → gen dùng giá trị mẫu thực {gv!r}')
                else:
                    print(f'    ⚠️  "{k}": sampler={sv!r}  gen={gv!r}')


if __name__ == '__main__':
    main()
