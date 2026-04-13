#!/usr/bin/env python3
"""
Shared utility: parse and manipulate SOAP XML request bodies.

Provides:
  - parse_soap_body(xml_str) → flat dict of field_name → value
  - apply_soap_body_mod(xml_str, body_mod, field_map) → modified XML string
  - detect_soap_body(body_raw) → bool (is this a SOAP XML body?)

Used by the test-generation pipeline to handle SOAP API test cases
alongside the existing JSON body support.
"""

import copy
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict


def detect_soap_body(body_raw: str) -> bool:
    """Return True if body_raw looks like a SOAP/XML request body."""
    if not body_raw or not isinstance(body_raw, str):
        return False
    stripped = body_raw.strip()
    return stripped.startswith('<') and ('Envelope' in stripped or '<?xml' in stripped
                                        or '<soap' in stripped.lower()
                                        or '<soapenv:' in stripped.lower())


def extract_soap_operation(body_raw: str) -> str:
    """Extract the SOAP operation name from inside <soapenv:Body>.

    Looks for the first child element of Body that is NOT WebRequestCommon,
    OfsFunction, or a *Type container.  For example:
        <sup:GetRetailAccountsofCustomer> → 'GetRetailAccountsofCustomer'
        <sup:UpdateLoanInterestDetails>   → 'UpdateLoanInterestDetails'

    Returns '' if not found.
    """
    if not body_raw or not detect_soap_body(body_raw):
        return ''
    # Use regex for speed — avoids full XML parse
    m = re.search(r'<(?:\w+:)?(\w+)', body_raw[body_raw.find('Body'):] if 'Body' in body_raw else body_raw)
    if not m:
        return ''
    _skip = {'body', 'webrequestcommon', 'ofsfunction'}
    # Scan all elements after Body
    for match in re.finditer(r'<(?:\w+:)?([A-Za-z]\w*)', body_raw[body_raw.find('Body'):]):
        name = match.group(1)
        if name.lower() not in _skip and not name.endswith('Type'):
            return name
    return ''


def _strip_ns_prefix(tag: str) -> str:
    """Remove namespace URI from tag: {uri}local → local, prefix:local → local."""
    if '}' in tag:
        return tag.rsplit('}', 1)[-1]
    if ':' in tag:
        return tag.rsplit(':', 1)[-1]
    return tag


def _find_body_element(root):
    """Find the <Body> element in a SOAP envelope (handles any namespace)."""
    for elem in root.iter():
        local = _strip_ns_prefix(elem.tag)
        if local == 'Body':
            return elem
    return root  # fallback: treat whole doc as body


def _extract_fields_recursive(elem, parent_path='', result=None):
    """Recursively extract leaf text elements → flat dict {elem_name: text_value}."""
    if result is None:
        result = OrderedDict()
    for child in elem:
        local_name = _strip_ns_prefix(child.tag)
        current_path = f"{parent_path}.{local_name}" if parent_path else local_name
        # Check if this is a leaf node (has text, no nested elements)
        has_children = len(list(child)) > 0
        if has_children:
            _extract_fields_recursive(child, current_path, result)
        else:
            text = (child.text or '').strip()
            # Use the leaf name only (not full path) for simple lookups
            result[local_name] = text
            # Also store with full path for disambiguation
            if parent_path:
                result[current_path] = text
    return result


def parse_soap_body(xml_str: str) -> dict:
    """Parse a SOAP XML body string → dict of {field_name: value}.

    Returns field names as simple leaf names (e.g. 'company', 'userName')
    plus dotted paths for disambiguation (e.g. 'WebRequestCommon.company').
    """
    if not xml_str or not xml_str.strip():
        return {}
    try:
        # Handle namespace prefixes — register them to avoid parsing errors
        # Extract and register all xmlns declarations
        for m in re.finditer(r'xmlns:(\w+)="([^"]+)"', xml_str):
            prefix, uri = m.group(1), m.group(2)
            try:
                ET.register_namespace(prefix, uri)
            except Exception:
                pass

        root = ET.fromstring(xml_str)
        body_elem = _find_body_element(root)
        return _extract_fields_recursive(body_elem)
    except ET.ParseError as e:
        print(f'  ⚠️  Cannot parse SOAP XML body: {e}')
        return {}


def _find_element_by_name(root, name: str):
    """Find element matching a field name (ignoring namespaces).
    Searches breadth-first to find the shallowest match."""
    for elem in root.iter():
        local = _strip_ns_prefix(elem.tag)
        if local == name:
            return elem
    return None


def _find_criteria_value_by_column(root, column_value: str):
    """T24 enquiry pattern: find <criteriaValue> sibling of <columnName>X</columnName>.

    In T24 SOAP enquiry requests, fields are represented as:
        <enquiryInputCollection>
            <columnName>CUSTOMER</columnName>
            <criteriaValue>11811698</criteriaValue>
            <operand>EQ</operand>
        </enquiryInputCollection>

    This function finds the <criteriaValue> element that is a sibling of
    a <columnName> whose text matches *column_value*.
    Returns (criteriaValue_element, parent_element) or (None, None).
    """
    for parent in root.iter():
        column_elem = None
        criteria_elem = None
        for child in parent:
            local = _strip_ns_prefix(child.tag)
            if local == 'columnName' and (child.text or '').strip() == column_value:
                column_elem = child
            if local == 'criteriaValue':
                criteria_elem = child
        if column_elem is not None and criteria_elem is not None:
            return criteria_elem, parent
    return None, None


def _find_element_by_path(root, dotted_path: str):
    """Find element by dotted path like 'WebRequestCommon.company'."""
    parts = dotted_path.split('.')
    current = root
    for part in parts:
        found = None
        for child in current:
            if _strip_ns_prefix(child.tag) == part:
                found = child
                break
        if found is None:
            return None
        current = found
    return current


def apply_soap_body_mod(xml_str: str, body_mod: dict,
                        field_map: dict = None) -> str:
    """Apply body modifications to a SOAP XML body string.

    Supports:
      - __remove__: list of field names to remove from XML
      - __set_null__: list of field names to set to empty string
      - field=value: set specific element text to value
      - field="": set to empty string
      - field="   ": set to whitespace

    Returns the modified XML string.
    """
    if not xml_str or not body_mod:
        return xml_str or ''

    field_map = field_map or {}

    try:
        # Preserve original declaration and namespaces
        # Re-register namespaces to preserve them in output
        for m in re.finditer(r'xmlns:(\w+)="([^"]+)"', xml_str):
            prefix, uri = m.group(1), m.group(2)
            try:
                ET.register_namespace(prefix, uri)
            except Exception:
                pass

        root = ET.fromstring(xml_str)
    except ET.ParseError:
        # Can't parse — return as-is
        return xml_str

    # __remove__: remove elements
    removes = body_mod.get("__remove__", [])
    if isinstance(removes, str):
        removes = [removes]
    for key in removes:
        mapped_key = field_map.get(key, key)
        # Try dotted path first
        if '.' in mapped_key:
            target = _find_element_by_path(root, mapped_key)
        else:
            target = _find_element_by_name(root, mapped_key)
        if target is not None:
            # Find parent and remove
            for parent in root.iter():
                if target in list(parent):
                    parent.remove(target)
                    break
        else:
            # T24 enquiry fallback: field is a columnName value → clear criteriaValue
            cv_elem, cv_parent = _find_criteria_value_by_column(root, mapped_key)
            if cv_elem is not None:
                cv_elem.text = ''

    # __set_null__: set to empty
    nulls = body_mod.get("__set_null__", [])
    if isinstance(nulls, str):
        nulls = [nulls]
    for key in nulls:
        mapped_key = field_map.get(key, key)
        if '.' in mapped_key:
            target = _find_element_by_path(root, mapped_key)
        else:
            target = _find_element_by_name(root, mapped_key)
        if target is not None:
            target.text = None
            # Remove all children too
            for child in list(target):
                target.remove(child)
        else:
            # T24 enquiry fallback
            cv_elem, _ = _find_criteria_value_by_column(root, mapped_key)
            if cv_elem is not None:
                cv_elem.text = None

    # field=value: set element text
    for k, v in body_mod.items():
        if k.startswith("__"):
            continue
        mapped_key = field_map.get(k, k)
        if '.' in mapped_key:
            target = _find_element_by_path(root, mapped_key)
        else:
            target = _find_element_by_name(root, mapped_key)
        if target is not None:
            target.text = str(v) if v is not None else None
        else:
            # T24 enquiry fallback: set criteriaValue text
            cv_elem, _ = _find_criteria_value_by_column(root, mapped_key)
            if cv_elem is not None:
                cv_elem.text = str(v) if v is not None else None

    # Serialize back to string
    result = ET.tostring(root, encoding='unicode')

    # Restore XML declaration if original had one
    if xml_str.strip().startswith('<?xml'):
        decl_match = re.match(r'(<\?xml[^?]*\?>)', xml_str.strip())
        if decl_match:
            result = decl_match.group(1) + '\n' + result

    return result


def soap_body_to_flat_dict(xml_str: str) -> dict:
    """Convert SOAP XML body to a flat dict suitable as sampler_body.

    Unlike parse_soap_body(), this returns ONLY simple leaf names (no dotted paths)
    to be used as the sampler_body in contracts.
    """
    full = parse_soap_body(xml_str)
    # Filter to only simple (non-dotted) keys
    return {k: v for k, v in full.items() if '.' not in k}


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    sample = '''<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:sup="http://t24.example-bank.com/SuperApp">
   <soapenv:Header/>
   <soapenv:Body>
      <sup:GetRetailAccountsofCustomer>
         <WebRequestCommon>
            <company>VN0010001</company>
            <password>aBC123456@!</password>
            <userName>NGUYETDA</userName>
         </WebRequestCommon>
         <MYAPPLISTRTLACCTType>
            <enquiryInputCollection>
               <columnName>CUSTOMER</columnName>
               <criteriaValue>11811698</criteriaValue>
               <operand>EQ</operand>
            </enquiryInputCollection>
         </MYAPPLISTRTLACCTType>
      </sup:GetRetailAccountsofCustomer>
   </soapenv:Body>
</soapenv:Envelope>'''

    print('=== Parse SOAP Body ===')
    fields = parse_soap_body(sample)
    for k, v in fields.items():
        print(f'  {k}: {v!r}')

    print('\n=== Flat Dict ===')
    flat = soap_body_to_flat_dict(sample)
    for k, v in flat.items():
        print(f'  {k}: {v!r}')

    print('\n=== Apply Modifications ===')
    mod1 = {"__remove__": ["password"], "company": "VN0020002", "criteriaValue": "99999999"}
    modified = apply_soap_body_mod(sample, mod1)
    print(modified[:500])

    print('\n=== Detect SOAP ===')
    print(f'  SOAP sample: {detect_soap_body(sample)}')
    print(f'  JSON sample: {detect_soap_body("{}")}')
    print(f'  Empty: {detect_soap_body("")}')
