#!/usr/bin/env python3
"""
Shared utility: parse SoapUI project XML → normalised request items
compatible with the Postman JSON structure used throughout the pipeline.

Each returned request item looks like:
{
    "name": "<request name>",
    "request": {
        "method": "POST",
        "header": [ {"key": "Content-Type", "value": "application/json", ...}, ... ],
        "body": { "mode": "raw", "raw": "...", "options": {"raw":{"language":"json"}} },
        "url": {
            "raw": "https://host/path",
            "protocol": "https",
            "host": ["host"],
            "path": ["seg1","seg2"]
        }
    },
    "event": []   # pre-request / test scripts (empty for SoapUI import)
}

Supports both:
  - SoapUI REST projects  (con:restResource / con:restMethod / con:restRequest)
  - SoapUI SOAP projects  (con:interface / con:operation / con:request)

Usage:
    from soapui_parser import parse_soapui_xml
    items = parse_soapui_xml('/path/to/project.xml')
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse


# ── Namespace handling ────────────────────────────────────────────────────
# SoapUI uses 'con' namespace: http://eviware.com/soapui/config
_NS = {'con': 'http://eviware.com/soapui/config'}


def _strip_ns(tag: str) -> str:
    """Remove namespace prefix from an XML tag: {ns}tag → tag."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _build_url_obj(raw_url: str) -> dict:
    """Build a Postman-style URL object from a raw URL string."""
    parsed = urlparse(raw_url)
    host_parts = parsed.hostname.split('.') if parsed.hostname else []
    path_parts = [p for p in parsed.path.strip('/').split('/') if p]
    return {
        'raw': raw_url,
        'protocol': parsed.scheme or 'https',
        'host': host_parts,
        'path': path_parts,
    }


def _extract_headers(elem) -> list:
    """Extract HTTP headers from a SoapUI request/config element."""
    headers = []
    # <con:setting> with id like "...#request-headers"
    for setting in elem.iter():
        tag = _strip_ns(setting.tag)
        sid = setting.get('id', '')
        if tag == 'setting' and 'request-headers' in sid:
            val = (setting.text or '').strip()
            if val:
                for line in val.split('\n'):
                    line = line.strip()
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers.append({'key': k.strip(), 'value': v.strip()})
    # <con:header> elements (REST style)
    for h in elem.iter():
        tag = _strip_ns(h.tag)
        if tag == 'header':
            name = h.get('name', '') or h.get('key', '')
            val = h.text or h.get('value', '') or ''
            if name:
                headers.append({'key': name.strip(), 'value': val.strip()})
    # <con:entry> inside <con:parameters> (older SoapUI)
    for entry in elem.iter():
        tag = _strip_ns(entry.tag)
        if tag == 'entry':
            k = entry.get('key', '')
            v = entry.get('value', '') or (entry.text or '')
            if k.lower() in ('content-type', 'accept', 'authorization', 'apikey',
                             'x-api-key', 'x-ibm-client-id', 'functioncode'):
                headers.append({'key': k, 'value': v})
    return headers


def _extract_body(elem) -> dict:
    """Extract request body from a SoapUI request element."""
    # <con:request> text content (SOAP body or raw JSON)
    body_text = ''
    for child in elem:
        tag = _strip_ns(child.tag)
        if tag == 'request':
            body_text = (child.text or '').strip()
            break
    if not body_text:
        # Direct text content of the element itself
        body_text = (elem.text or '').strip()
    if not body_text:
        return {'mode': 'raw', 'raw': '', 'options': {'raw': {'language': 'json'}}}

    # Detect language
    lang = 'json'
    if body_text.lstrip().startswith('<'):
        lang = 'xml'
    elif body_text.lstrip().startswith('{') or body_text.lstrip().startswith('['):
        lang = 'json'

    return {
        'mode': 'raw',
        'raw': body_text,
        'options': {'raw': {'language': lang}}
    }


# ── REST project parsing ──────────────────────────────────────────────────

def _parse_rest_resources(root, base_endpoints: dict) -> list:
    """Parse REST-style SoapUI project into Postman-like request items."""
    items = []

    # Collect endpoints: <con:endpoint>https://...</con:endpoint>
    default_endpoint = ''
    for ep_elem in root.iter():
        tag = _strip_ns(ep_elem.tag)
        if tag == 'endpoint':
            default_endpoint = (ep_elem.text or '').strip()
            if default_endpoint:
                break

    # Traverse: restResource → restMethod → restRequest
    def _walk_resources(parent, parent_path=''):
        for child in parent:
            tag = _strip_ns(child.tag)
            if tag in ('restResource', 'resource'):
                res_path = child.get('path', '') or child.get('resourcePath', '')
                full_path = (parent_path.rstrip('/') + '/' + res_path.lstrip('/')).rstrip('/')
                # Find methods
                for method_elem in child:
                    mtag = _strip_ns(method_elem.tag)
                    if mtag in ('restMethod', 'method'):
                        http_method = method_elem.get('method', 'GET').upper()
                        method_name = method_elem.get('name', '')
                        # Find requests
                        for req_elem in method_elem:
                            rtag = _strip_ns(req_elem.tag)
                            if rtag in ('restRequest', 'request'):
                                req_name = req_elem.get('name', method_name or full_path)
                                # Get endpoint override
                                req_endpoint = ''
                                for sub in req_elem:
                                    if _strip_ns(sub.tag) == 'endpoint':
                                        req_endpoint = (sub.text or '').strip()
                                        break
                                ep_base = req_endpoint or default_endpoint
                                raw_url = (ep_base.rstrip('/') + full_path) if ep_base else full_path

                                items.append({
                                    'name': req_name,
                                    'request': {
                                        'method': http_method,
                                        'header': _extract_headers(req_elem),
                                        'body': _extract_body(req_elem),
                                        'url': _build_url_obj(raw_url),
                                    },
                                    'event': [],
                                })
                # Recurse into child resources
                _walk_resources(child, full_path)

    # Find all interface/restService elements containing resources
    for iface in root.iter():
        tag = _strip_ns(iface.tag)
        if tag in ('interface', 'restService'):
            iface_endpoints = {}
            for ep_el in iface:
                if _strip_ns(ep_el.tag) == 'endpoint':
                    iface_endpoints[ep_el.text or ''] = True
            _walk_resources(iface, '')

    # Direct resources under project root
    _walk_resources(root, '')

    return items


# ── SOAP project parsing ──────────────────────────────────────────────────

def _parse_soap_interfaces(root) -> list:
    """Parse SOAP-style SoapUI project into Postman-like request items."""
    items = []

    for iface in root.iter():
        tag = _strip_ns(iface.tag)
        if tag != 'interface':
            continue
        iface_type = iface.get('type', '')
        if 'rest' in iface_type.lower():
            continue  # handled by REST parser

        # Get WSDL endpoint
        wsdl_endpoint = ''
        for ep_el in iface:
            if _strip_ns(ep_el.tag) == 'endpoint':
                wsdl_endpoint = (ep_el.text or '').strip()
                break

        # Operations
        for op in iface:
            if _strip_ns(op.tag) != 'operation':
                continue
            op_name = op.get('name', '')

            for req_el in op:
                if _strip_ns(req_el.tag) not in ('request', 'call'):
                    continue
                req_name = req_el.get('name', op_name)
                # SOAP endpoint override
                req_endpoint = ''
                for sub in req_el:
                    if _strip_ns(sub.tag) == 'endpoint':
                        req_endpoint = (sub.text or '').strip()
                        break
                raw_url = req_endpoint or wsdl_endpoint

                items.append({
                    'name': req_name,
                    'request': {
                        'method': 'POST',  # SOAP is always POST
                        'header': _extract_headers(req_el),
                        'body': _extract_body(req_el),
                        'url': _build_url_obj(raw_url),
                    },
                    'event': [],
                })

    return items


# ── TestSuite / TestCase parsing ──────────────────────────────────────────

def _parse_test_suites(root) -> list:
    """Parse SoapUI TestSuite/TestCase/TestStep for REST or SOAP requests."""
    items = []

    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag != 'testStep':
            continue
        step_type = elem.get('type', '')
        step_name = elem.get('name', '')

        # REST request test step
        if 'rest' in step_type.lower() or 'http' in step_type.lower():
            for config in elem:
                if _strip_ns(config.tag) not in ('config', 'restRequest', 'httpRequest'):
                    continue
                method = config.get('method', 'GET').upper()
                # Endpoint
                raw_url = ''
                for sub in config.iter():
                    stag = _strip_ns(sub.tag)
                    if stag == 'endpoint':
                        raw_url = (sub.text or '').strip()
                        break
                    if stag == 'resourcePath':
                        raw_url = (sub.text or '').strip()
                # Service + resourcePath
                service = config.get('service', '')
                res_path = config.get('resourcePath', '')
                if res_path and not raw_url:
                    raw_url = res_path

                if raw_url:
                    items.append({
                        'name': step_name or raw_url,
                        'request': {
                            'method': method,
                            'header': _extract_headers(config),
                            'body': _extract_body(config),
                            'url': _build_url_obj(raw_url),
                        },
                        'event': [],
                    })

        # SOAP/Groovy test steps with request content
        elif 'request' in step_type.lower():
            for config in elem:
                ctag = _strip_ns(config.tag)
                if ctag != 'config':
                    continue
                iface = config.get('interface', '')
                operation = config.get('operation', '')
                raw_url = ''
                for sub in config.iter():
                    if _strip_ns(sub.tag) == 'endpoint':
                        raw_url = (sub.text or '').strip()
                        break
                if raw_url or operation:
                    items.append({
                        'name': step_name or operation,
                        'request': {
                            'method': 'POST',
                            'header': _extract_headers(config),
                            'body': _extract_body(config),
                            'url': _build_url_obj(raw_url or f'/{operation}'),
                        },
                        'event': [],
                    })

    return items


# ── Main entry point ──────────────────────────────────────────────────────

def parse_soapui_xml(xml_path) -> list:
    """Parse a SoapUI project XML file and return Postman-compatible request items.

    Args:
        xml_path: Path to .xml SoapUI project file.

    Returns:
        list of Postman-compatible request item dicts.
        Each has: name, request {method, header, body, url}, event.
    """
    xml_path = Path(xml_path)
    if not xml_path.exists():
        print(f'  ⚠️  SoapUI file not found: {xml_path}')
        return []

    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as e:
        print(f'  ⚠️  Cannot parse SoapUI XML ({xml_path.name}): {e}')
        return []

    root = tree.getroot()

    # Collect base endpoints
    base_endpoints = {}
    for ep_el in root:
        if _strip_ns(ep_el.tag) == 'endpoint':
            base_endpoints[ep_el.text or ''] = True

    # Parse REST resources
    items = _parse_rest_resources(root, base_endpoints)

    # Parse SOAP interfaces (skip if already found as REST)
    soap_items = _parse_soap_interfaces(root)
    existing_names = {it['name'] for it in items}
    for si in soap_items:
        if si['name'] not in existing_names:
            items.append(si)
            existing_names.add(si['name'])

    # Parse test suites
    test_items = _parse_test_suites(root)
    for ti in test_items:
        if ti['name'] not in existing_names:
            items.append(ti)
            existing_names.add(ti['name'])

    # Deduplicate by endpoint path
    seen_endpoints = set()
    deduped = []
    for it in items:
        url_raw = it.get('request', {}).get('url', {}).get('raw', '')
        ep = urlparse(url_raw).path.rstrip('/') if '://' in url_raw else url_raw
        key = (it['name'], ep)
        if key not in seen_endpoints:
            deduped.append(it)
            seen_endpoints.add(key)

    if deduped:
        print(f'  ✅  SoapUI: parsed {len(deduped)} request(s) from {xml_path.name}')
    else:
        print(f'  ⚠️  SoapUI: no requests found in {xml_path.name}')

    return deduped


def soapui_to_postman_collection(xml_path, output_path=None) -> dict:
    """Convert a SoapUI XML project to a Postman Collection v2.1 JSON structure.

    If output_path is given, writes the JSON file as well.
    """
    xml_path = Path(xml_path)
    items = parse_soapui_xml(xml_path)

    collection = {
        'info': {
            'name': xml_path.stem,
            '_postman_id': '',
            'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
        },
        'item': items,
        'variable': [],
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(collection, f, indent=2, ensure_ascii=False)
        print(f'  ✅  Exported Postman collection → {output_path}')

    return collection


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: python soapui_parser.py <soapui-project.xml> [output.postman_collection.json]')
        sys.exit(1)
    xml_file = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) > 2 else None
    col = soapui_to_postman_collection(xml_file, out_file)
    if not out_file:
        print(json.dumps(col, indent=2, ensure_ascii=False))
