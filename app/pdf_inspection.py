"""Bounded static PDF inspection in a separate worker; no rendering or scripts.

pdf-parser.py supplies the object tokenizer. This adapter reads dictionary
values, including compressed object streams, and reports evidence, not verdicts.
Only the follow-up controller should launch this worker on a verified copy.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import zlib

MAX_FILE = 512 * 1024 * 1024
MAX_OBJECTS = 50000
MAX_DICTIONARY = 2 * 1024 * 1024
MAX_DECODED = 16 * 1024 * 1024
MAX_DECODED_TOTAL = 64 * 1024 * 1024
MAX_FINDINGS = 2000
VENDOR_SHA256 = 'a9fa2faa25e88ab0293543d14aa168ff21fc9fd25a0a8ad657b0dad3171214ac'
MAX_TEXT = 65536
PDF_SPACE = '\x00\t\n\f\r '
NAME_TOKEN = re.compile(r'/[^\x00\t\n\f\r ()<>\[\]{}/%]*')
REFERENCE_TOKEN = re.compile(r'(\d+)[\x00\t\n\f\r ]+(\d+)[\x00\t\n\f\r ]+R(?=$|[\x00\t\n\f\r ()<>\[\]/%])')
SCALAR_TOKEN = re.compile(r'(true|false|null|[+-]?(?:\d+\.?\d*|\.\d+))(?=$|[\x00\t\n\f\r ()<>\[\]/%])')


class PDFString:
    """Bounded decoded string; never evaluated or exported as script source."""
    def __init__(self, data, truncated=False):
        self.data, self.truncated = data[:MAX_TEXT], truncated or len(data) > MAX_TEXT

    def text(self):
        if self.data.startswith((b'\xfe\xff', b'\xff\xfe')):
            return self.data.decode('utf-16', errors='replace')
        try:
            return self.data.decode('utf-8')
        except UnicodeDecodeError:
            return self.data.decode('latin1')


class InspectionError(ValueError):
    pass


class Reference(tuple):
    def __new__(cls, number, generation):
        return tuple.__new__(cls, (number, generation))


class Values:
    """Bounded value reader; string bytes are data and are never executed."""
    def __init__(self, text):
        if len(text) > MAX_DICTIONARY:
            raise InspectionError('An object dictionary/value exceeds the inspection limit')
        self.text, self.at, self.count = text, 0, 0

    def space(self):
        while self.at < len(self.text):
            if self.text[self.at] in PDF_SPACE:
                self.at += 1
            elif self.text[self.at] == '%':
                end = re.search(r'[\r\n]', self.text[self.at:])
                self.at = self.at + end.start() if end else len(self.text)
            else:
                break

    def value(self, depth=0):
        self.count += 1
        if depth > 40 or self.count > 100000:
            raise InspectionError('Nested PDF values exceed the inspection limit')
        self.space()
        if self.text.startswith('<<', self.at):
            self.at += 2
            result = {}
            while True:
                self.space()
                if self.text.startswith('>>', self.at):
                    self.at += 2
                    return result
                key = self.value(depth + 1)
                if not isinstance(key, str) or not key.startswith('/') or key in result:
                    raise InspectionError('A PDF dictionary has an invalid or repeated key')
                result[key] = self.value(depth + 1)
        if self.text.startswith('[', self.at):
            self.at += 1
            result = []
            while True:
                self.space()
                if self.text.startswith(']', self.at):
                    self.at += 1
                    return result
                result.append(self.value(depth + 1))
        if self.text.startswith('(', self.at):
            self.at += 1
            balance = 1
            decoded = bytearray()
            truncated = False
            while self.at < len(self.text) and balance:
                char = self.text[self.at]
                self.at += 1
                if char == '\\':
                    if self.at >= len(self.text):
                        break
                    char = self.text[self.at]
                    self.at += 1
                    if char in '\r\n':
                        if char == '\r' and self.text.startswith('\n', self.at):
                            self.at += 1
                        continue
                    if char in '01234567':
                        octal = char
                        while len(octal) < 3 and self.at < len(self.text) and self.text[self.at] in '01234567':
                            octal += self.text[self.at]
                            self.at += 1
                        char = chr(int(octal, 8) & 255)
                    else:
                        char = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f'}.get(char, char)
                elif char == '(':
                    balance += 1
                elif char == ')':
                    balance -= 1
                    if not balance:
                        break
                if len(decoded) < MAX_TEXT:
                    decoded.append(ord(char) & 255)
                else:
                    truncated = True
            if balance:
                raise InspectionError('Unterminated PDF string')
            return PDFString(bytes(decoded), truncated)
        if self.text.startswith('<', self.at):
            end = self.text.find('>', self.at + 1)
            if end < 0 or re.search(r'[^\da-fA-F\x00\t\n\f\r ]', self.text[self.at + 1:end]):
                raise InspectionError('Invalid PDF hexadecimal string')
            digits = re.sub(r'[\x00\t\n\f\r ]', '', self.text[self.at + 1:end])
            truncated = len(digits) > MAX_TEXT * 2
            digits = digits[:MAX_TEXT * 2]
            if len(digits) % 2:
                digits += '0'
            self.at = end + 1
            return PDFString(bytes.fromhex(digits), truncated)
        if self.text.startswith('/', self.at):
            match = NAME_TOKEN.match(self.text, self.at)
            self.at += len(match[0])
            return re.sub(r'#([0-9a-fA-F]{2})', lambda m: chr(int(m[1], 16)), match[0])
        reference = REFERENCE_TOKEN.match(self.text, self.at)
        if reference:
            self.at += len(reference[0])
            return Reference(int(reference[1]), int(reference[2]))
        scalar = SCALAR_TOKEN.match(self.text, self.at)
        if scalar:
            self.at += len(scalar[0])
            value = scalar[0]
            if value in ('true', 'false', 'null'):
                return {'true': True, 'false': False, 'null': None}[value]
            return float(value) if '.' in value else int(value)
        raise InspectionError('Unrecognized or truncated PDF value')

    def read(self):
        result = self.value()
        self.space()
        if self.at != len(self.text):
            raise InspectionError('Unexpected data after a PDF value')
        return result


def dictionaries(value, depth=0):
    if depth > 40:
        raise InspectionError('Dictionary nesting limit reached')
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child, depth + 1)


def load_parser():
    path = Path(__file__).with_name('vendor') / 'pdf_parser.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != VENDOR_SHA256:
        raise InspectionError('Bundled PDF parser checksum differs; reinstall the reviewed tool')
    spec = importlib.util.spec_from_file_location('pinakeys_bundled_pdf_parser', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parser_for(module, data):
    parser = module.cPDFParser(io.BytesIO(data))
    ignore_space = parser.oPDFTokenizer.TokenIgnoreWhiteSpace
    # pdf-parser deliberately drops trailer whitespace. Retain the original
    # tokens instead: concatenating /Size, 123 and /Root, 1, 0, R corrupts them.
    def next_token():
        if parser.context != module.CONTEXT_TRAILER:
            return ignore_space()
        token = parser.oPDFTokenizer.Token()
        if token and token[1].startswith('%'):
            return (module.CHAR_WHITESPACE, '\n')
        return token
    parser.oPDFTokenizer.TokenIgnoreWhiteSpace = next_token
    return parser


def decode_stream(raw, dictionary, limit):
    filters = dictionary.get('/Filter')
    if dictionary.get('/DecodeParms') is not None:
        raise InspectionError('Stream decoding parameters are not supported by this check')
    if filters is None:
        if len(raw) > limit:
            raise InspectionError('Decoded stream exceeds the inspection limit')
        return raw
    if filters in ('/FlateDecode', '/Fl') or filters in (['/FlateDecode'], ['/Fl']):
        inflater = zlib.decompressobj()
        decoded = inflater.decompress(raw, limit + 1)
        if len(decoded) > limit or inflater.unconsumed_tail or not inflater.eof:
            raise InspectionError('Stream decompression exceeded its limit or was incomplete')
        return decoded
    raise InspectionError('A stream uses an unsupported compression filter')


def describe_script(value, identity, issue):
    if not isinstance(value, PDFString):
        issue('A JavaScript value could not be inspected as a bounded string or supported stream')
        return {'object': identity, 'complete': False, 'observations': ['Script text unavailable; behaviour remains unresolved.']}
    text = value.text()
    patterns = [
        (r'\bthis\s*\.\s*(?:pageNum|zoomType|zoom)\s*=|\bthis\s*\.\s*scroll\s*\(', 'Page navigation or view setting'),
        (r'\bhostContainer\s*\.\s*postMessage\s*\(', 'Message to the hosting application'),
        (r'\b(?:this\s*\.\s*)?print\s*\(', 'Print command'),
        (r'\b(?:addMenuItem|addSubMenu|addToolButton|execMenuItem)\s*\(', 'Viewer menu or toolbar command'),
        (r'\b(?:getField|setAction|calculateNow|resetForm)\s*\(', 'Form-field operation'),
        (r'\b(?:launchURL|submitForm|getURL|XMLHttpRequest|fetch|Net\s*\.)\b', 'URL, submission or network-related text'),
        (r'\b(?:launchFile|exportDataObject|importDataObject|saveAs)\s*\(', 'File, attachment or save operation'),
        (r'\b(?:eval|Function|unescape|fromCharCode|atob)\s*\(', 'Dynamic evaluation or encoded-text helper'),
    ]
    observations = [label for pattern, label in patterns if re.search(pattern, text)]
    if value.truncated:
        issue('A JavaScript string exceeds the bounded text-inspection limit')
    return {'object': identity, 'complete': not value.truncated, 'bytes_inspected': len(value.data),
            'sha256_of_inspected_text': hashlib.sha256(value.data).hexdigest(),
            'observations': observations or ['No selected behaviour pattern recognised; manual analysis remains necessary.'],
            'notice': 'Text patterns only; comments and strings can match. Not execution, full code analysis or a malware verdict.'}


def inspect(data):
    if len(data) > MAX_FILE or not data[:1024].lstrip().startswith(b'%PDF-'):
        raise InspectionError('The verified copy is too large or has no PDF header')
    parser_module = load_parser()
    parser = parser_for(parser_module, data)
    objects, origins, issues = {}, {}, []
    previous = []
    decoded_total = 0
    encrypted = False
    revision_hint = False

    def issue(message):
        if message not in issues and len(issues) < 40:
            issues.append(message)

    def add_object(identity, value, origin=None):
        if len(objects) >= MAX_OBJECTS:
            raise InspectionError('PDF object count exceeds the inspection limit')
        if identity in objects:
            if len(previous) >= MAX_OBJECTS:
                raise InspectionError('Repeated-object count exceeds the inspection limit')
            previous.append((identity, objects[identity]))
            issue('Repeated object definitions or incremental updates need additional review')
        objects[identity] = value
        origins[identity] = origin

    while True:
        item = parser.GetObject()
        if item is None:
            if parser.context != parser_module.CONTEXT_NONE:
                issue('The parser reached the end inside an unfinished PDF element')
            break
        if item.type == parser_module.PDF_ELEMENT_TRAILER:
            try:
                trailer = Values(''.join(t[1] for t in item.content)[len('trailer'):]).read()
                encrypted |= isinstance(trailer, dict) and '/Encrypt' in trailer
                if isinstance(trailer, dict):
                    revision_hint |= any(type(trailer.get(key)) is int and trailer[key] > 0 for key in ('/Prev', '/XRefStm'))
            except InspectionError as exc:
                issue('A trailer dictionary could not be fully inspected: ' + str(exc))
        if item.type != parser_module.PDF_ELEMENT_INDIRECT_OBJECT:
            continue
        identity = (item.id, item.version)
        prefix = item.ContainsStream()
        try:
            value = Values(''.join(t[1] for t in (prefix if prefix is not False else item.content))).read()
            add_object(identity, value)
            if isinstance(value, dict) and '/Encrypt' in value:
                encrypted = True
            if isinstance(value, dict) and value.get('/Type') == '/XRef':
                revision_hint |= type(value.get('/Prev')) is int and value['/Prev'] > 0
            if not isinstance(value, dict) or value.get('/Type') != '/ObjStm':
                continue
            raw = item.Stream(filter=False)
            if not isinstance(raw, str):
                raise InspectionError('A compressed object stream could not be read')
            raw = raw.encode('latin1')
            decoded = decode_stream(raw, value, MAX_DECODED)
            decoded_total += len(decoded)
            if len(decoded) > MAX_DECODED or decoded_total > MAX_DECODED_TOTAL:
                raise InspectionError('Decoded object streams exceed the inspection limit')
            first, number = value.get('/First'), value.get('/N')
            if type(first) is not int or type(number) is not int or not 0 <= first <= len(decoded) or not 0 <= number <= MAX_OBJECTS:
                raise InspectionError('Invalid compressed-object index')
            index_tokens = re.findall(rb'[^\x00\t\n\f\r ]+', decoded[:first])
            if any(not re.fullmatch(rb'\d+', n) for n in index_tokens):
                raise InspectionError('Invalid compressed-object index token')
            indexes = [int(n) for n in index_tokens]
            if len(indexes) != number * 2:
                raise InspectionError('Invalid compressed-object index length')
            offsets = indexes[1::2]
            if offsets != sorted(set(offsets)) or any(n < 0 or first + n > len(decoded) for n in offsets):
                raise InspectionError('Invalid compressed-object offsets')
            for at in range(number):
                end = first + offsets[at + 1] if at + 1 < number else len(decoded)
                fragment = decoded[first + offsets[at]:end].decode('latin1')
                add_object((indexes[at * 2], 0), Values(fragment).read(), f'{item.id} {item.version}')
        except (ValueError, TypeError, IndexError, zlib.error) as exc:
            issue(f'Object {item.id} {item.version}: ' + str(exc)[:180])

    if not objects:
        issue('No PDF indirect objects could be inspected')
    if b'%%EOF' not in data[-4096:]:
        issue('No final PDF end marker was found near the end of the file')
    if not any(isinstance(v, dict) and v.get('/Type') == '/Catalog' for v in objects.values()):
        issue('A document catalog could not be identified')
    if encrypted:
        issue('The PDF is encrypted; encrypted strings or streams were not decrypted')

    result = analyze_objects(data, parser_module, objects, origins, list(issues), decoded_total)
    result['revision_resolution'] = {'status': 'not_needed', 'repeated_definitions': len(previous), 'revision_pointer_found': revision_hint}
    if not previous and not revision_hint:
        return result
    physical = list(result['findings'])
    for identity, value in previous:
        physical.extend(physical_markers(identity, value))
    result['physical_findings'] = deduplicate_findings(physical)
    if len(physical) > MAX_FINDINGS:
        result['limitations'].append('Physical-history detail reached its bounded limit; older features may not all be listed')
    return resolve_revision(data, parser_module, objects, origins, result, decoded_total)


def analyze_objects(data, parser_module, objects, origins, issues, decoded_total=0, script_reader=None):
    findings = []
    encrypted = any('encrypted' in message.lower() for message in issues)

    def issue(message):
        if message not in issues and len(issues) < 40:
            issues.append(message)

    def resolve(value):
        seen = set()
        while isinstance(value, Reference):
            if value in seen or len(seen) > 40:
                issue('A relevant PDF reference is cyclic or too deeply nested')
                return None
            seen.add(value)
            if value not in objects:
                issue(f'A relevant PDF reference could not be resolved: {value[0]} {value[1]}')
                return None
            value = objects[value]
        return value

    def action_types(value, depth=0):
        if depth > 20:
            issue('An action chain exceeded the inspection depth limit')
            return set()
        value = resolve(value)
        if isinstance(value, list):
            return set().union(*(action_types(v, depth + 1) for v in value)) if value else set()
        if not isinstance(value, dict):
            return set()
        result = {value['/S']} if isinstance(value.get('/S'), str) else set()
        if '/JS' in value:
            result.add('/JavaScript')
        if '/Next' in value:
            result |= action_types(value['/Next'], depth + 1)
        return result

    def finding(feature, identity, detail):
        if len(findings) >= MAX_FINDINGS:
            issue('The findings limit was reached; additional features may be present')
            return
        row = {'feature': feature, 'object': f'{identity[0]} {identity[1]}', 'detail': detail}
        if origins.get(identity):
            row['inside_object_stream'] = origins[identity]
        if row not in findings:
            findings.append(row)

    script_values, launch_reviews = [], []

    def launch_target(dictionary):
        target = dictionary.get('/F')
        windows = resolve(dictionary.get('/Win'))
        if isinstance(windows, dict):
            target = windows.get('/F', target)
        target = resolve(target)
        if isinstance(target, dict):
            target = resolve(target.get('/UF', target.get('/F')))
        name = target.text() if isinstance(target, PDFString) else ''
        name = ''.join(c if c.isprintable() else '?' for c in name)[:240]
        return {'target': name or 'Target not resolved',
                'parameters_present': isinstance(windows, dict) and '/P' in windows,
                'notice': 'Target text only. No file, program or address was opened.'}

    for identity, value in objects.items():
        for dictionary in dictionaries(value):
            subtype = dictionary.get('/S')
            if '/JS' in dictionary or subtype == '/JavaScript' or '/JavaScript' in dictionary:
                finding('javascript', identity, 'JavaScript-related dictionary structure is present. Its code was not executed or judged malicious.')
                if '/JS' in dictionary:
                    if len(script_values) < MAX_FINDINGS:
                        script_values.append((identity, dictionary['/JS']))
                    else:
                        issue('The script-detail limit was reached')
            if subtype == '/Launch' or '/Launch' in dictionary:
                finding('launch', identity, 'A launch action is present; its target was not opened.')
                if len(launch_reviews) < MAX_FINDINGS:
                    launch_reviews.append(dict(object=f'{identity[0]} {identity[1]}', **launch_target(dictionary)))
            if dictionary.get('/Type') == '/EmbeddedFile' or '/EmbeddedFiles' in dictionary or '/EF' in dictionary:
                finding('embedded', identity, 'Embedded-file structure is present. No attachment was extracted or opened.')
            if any(k in dictionary for k in ('/RichMedia', '/RichMediaContent')) or dictionary.get('/Subtype') == '/RichMedia':
                finding('rich_media', identity, 'Rich-media content is present; it was not rendered.')
            if subtype in ('/URI', '/GoToR', '/SubmitForm', '/ImportData'):
                finding('external_action', identity, 'An external-link or data action is present; no address was followed.')
            if '/OpenAction' in dictionary:
                opening = resolve(dictionary['/OpenAction'])
                if isinstance(opening, dict) and opening.get('/S') == '/GoTo' and '/Next' not in opening:
                    opening = resolve(opening.get('/D'))
                if isinstance(opening, list) and len(opening) > 1 and opening[1] in ('/XYZ', '/Fit', '/FitH', '/FitV', '/FitR', '/FitB', '/FitBH', '/FitBV'):
                    page = resolve(opening[0])
                    if not isinstance(page, dict) or page.get('/Type') != '/Page':
                        issue('The opening destination could not be resolved to a page object')
                    finding('opening_view', identity, 'OpenAction specifies an opening page/view destination, rather than a script action.')
                else:
                    types = action_types(opening)
                    finding('automatic_action', identity, 'Document-open action types: ' + (', '.join(sorted(types)) or 'not determined; needs review'))
                    if '/JavaScript' in types:
                        finding('automatic_javascript', identity, 'A document-open action is linked to JavaScript.')
            if '/AA' in dictionary:
                extra = resolve(dictionary['/AA'])
                types = set()
                if isinstance(extra, dict):
                    for action in extra.values():
                        types |= action_types(action)
                finding('automatic_action', identity, 'Additional event actions are present: ' + (', '.join(sorted(types)) or 'types not determined'))
                if '/JavaScript' in types:
                    finding('automatic_javascript', identity, 'An additional event action is linked to JavaScript.')

    # Decode only streams explicitly referenced as /JS, within the same worker.
    # No page content, images or attached files are extracted or rendered.
    wanted = {value for _, value in script_values if isinstance(value, Reference)
              and isinstance(resolve(value), dict)}
    script_streams = {}
    if wanted and script_reader is not None:
        for identity in wanted:
            try:
                script_streams[identity] = PDFString(script_reader(identity, objects[identity]))
            except (ValueError, TypeError, RuntimeError, zlib.error) as exc:
                issue(f'JavaScript stream {identity[0]} {identity[1]}: ' + str(exc)[:180])
    elif wanted:
        second = parser_for(parser_module, data)
        while True:
            item = second.GetObject()
            if item is None:
                break
            identity = (getattr(item, 'id', -1), getattr(item, 'version', -1))
            if identity not in wanted or item.type != parser_module.PDF_ELEMENT_INDIRECT_OBJECT:
                continue
            try:
                raw = item.Stream(filter=False)
                if not isinstance(raw, str):
                    raise InspectionError('A JavaScript stream could not be read')
                # Bound both raw and decoded script data. Unsupported formats
                # stay incomplete; do not fall back to executing an interpreter.
                if len(raw) > MAX_DICTIONARY:
                    raise InspectionError('A JavaScript stream exceeds the raw-data inspection limit')
                decoded = decode_stream(raw.encode('latin1'), objects[identity], MAX_TEXT)
                decoded_total += len(decoded)
                if decoded_total > MAX_DECODED_TOTAL:
                    raise InspectionError('Decoded streams exceed the total inspection limit')
                script_streams[identity] = PDFString(decoded)
            except (ValueError, TypeError, zlib.error) as exc:
                issue(f'JavaScript stream {identity[0]} {identity[1]}: ' + str(exc)[:180])

    script_reviews = []
    for identity, source in script_values:
        value = script_streams.get(source) if isinstance(source, Reference) and source in wanted else resolve(source)
        script_reviews.append(describe_script(value, f'{identity[0]} {identity[1]}', issue))
        if len(script_reviews) >= MAX_FINDINGS:
            issue('The script-detail limit was reached')
            break

    features = {f['feature'] for f in findings}
    priority = 'closer review' if features & {'javascript', 'automatic_javascript', 'launch', 'rich_media', 'automatic_action'} else 'context review'
    if issues:
        category = 'incomplete'
    elif features & {'javascript', 'automatic_javascript', 'launch', 'rich_media', 'automatic_action'}:
        category = 'active_features'
    elif 'embedded' in features:
        category = 'embedded_content'
    elif 'opening_view' in features:
        category = 'opening_view'
    else:
        category = 'keyword_unconfirmed'
    return {'category': category, 'priority': priority, 'objects_inspected': len(objects),
            'compressed_objects_inspected': sum(v is not None for v in origins.values()),
            'findings': findings, 'limitations': issues, 'encrypted': encrypted,
            'inspection_status': 'incomplete' if issues else 'complete_within_limits',
            'script_reviews': script_reviews, 'launch_reviews': launch_reviews[:MAX_FINDINGS],
            'parser': 'Didier Stevens pdf-parser.py 0.7.14; Pinakeys adapter 3',
            'revision_notice': 'Physical object definitions inspected; current revision resolution is recorded separately.'}


def deduplicate_findings(rows):
    result, seen = [], set()
    for row in rows:
        key = (row['feature'], row['object'])
        if key not in seen:
            seen.add(key)
            result.append(row)
        if len(result) >= MAX_FINDINGS:
            break
    return result


def physical_markers(identity, value):
    """Retain evidence in overwritten definitions without claiming it is live."""
    result = []
    for dictionary in dictionaries(value):
        features = []
        if '/JS' in dictionary or dictionary.get('/S') == '/JavaScript' or '/JavaScript' in dictionary:
            features.append('javascript')
        if dictionary.get('/S') == '/Launch' or '/Launch' in dictionary:
            features.append('launch')
        if dictionary.get('/Type') == '/EmbeddedFile' or '/EmbeddedFiles' in dictionary or '/EF' in dictionary:
            features.append('embedded')
        if any(k in dictionary for k in ('/RichMedia', '/RichMediaContent')) or dictionary.get('/Subtype') == '/RichMedia':
            features.append('rich_media')
        if '/AA' in dictionary:
            features.append('automatic_action')
        if '/OpenAction' in dictionary:
            opening = dictionary['/OpenAction']
            if isinstance(opening, dict) and opening.get('/S') == '/GoTo' and '/Next' not in opening:
                opening = opening.get('/D')
            features.append('opening_view' if isinstance(opening, list) and len(opening) > 1 and opening[1] in
                            ('/XYZ', '/Fit', '/FitH', '/FitV', '/FitR', '/FitB', '/FitBH', '/FitBV') else 'automatic_action')
        for feature in features:
            result.append({'feature': feature, 'object': f'{identity[0]} {identity[1]}',
                           'detail': 'Feature structure in an earlier physical definition. Current selection is checked separately; historical script bodies were not analysed.'})
    return result


def load_revision_engine():
    try:
        return importlib.import_module('pymupdf')
    except ImportError:
        return None


def has_nonzero_reference(value):
    if isinstance(value, Reference):
        return value[1] != 0
    if isinstance(value, dict):
        return any(has_nonzero_reference(v) for v in value.values())
    if isinstance(value, list):
        return any(has_nonzero_reference(v) for v in value)
    return False


def reference_numbers(value):
    if isinstance(value, Reference):
        yield value[0]
    elif isinstance(value, dict):
        for child in value.values():
            yield from reference_numbers(child)
    elif isinstance(value, list):
        for child in value:
            yield from reference_numbers(child)


def resolve_revision(data, parser_module, physical_objects, origins, physical_result, decoded_total):
    """Use the installed reader's current xref, never file order, for revisions.

    This path reads object dictionaries and selected script streams only. It
    never renders, saves, authenticates, follows links or invokes document code.
    The optional native reader is subject to the existing worker restrictions.
    Nonzero generations are deliberately unsupported: the public xref_object
    API addresses numbers only, so silently collapsing generations is unsafe.
    """
    resolution = {'status': 'unresolved',
                  'repeated_definitions': physical_result['revision_resolution']['repeated_definitions'],
                  'engine': 'not available'}
    physical_result['revision_resolution'] = resolution

    def unresolved(message):
        if message not in physical_result['limitations']:
            physical_result['limitations'].append(message)
        physical_result['inspection_status'] = 'incomplete'
        physical_result['category'] = 'incomplete'
        physical_result['revision_notice'] = 'Current revision remains unresolved. Physical findings are retained; do not treat their order as proof of current selection.'
        resolution['reason'] = message
        return physical_result

    if physical_result['encrypted']:
        return unresolved('Revision resolution was not attempted on encrypted content')
    if any(generation != 0 for _, generation in physical_objects) or any(has_nonzero_reference(v) for v in physical_objects.values()):
        return unresolved('Nonzero object generations need a generation-aware specialist check; number-only revision resolution was not used')
    engine = load_revision_engine()
    if engine is None:
        return unresolved('Current PDF revision resolution needs the optional PyMuPDF installation on this computer')
    resolution['engine'] = 'PyMuPDF ' + str(engine.VersionBind)
    doc = None
    stage = 'opening the revision reader'
    try:
        engine.TOOLS.mupdf_warnings(reset=True)
        doc = engine.open(stream=data, filetype='pdf')
        if doc.needs_pass or doc.is_encrypted:
            return unresolved('The revision reader reports encrypted content; no password or decryption was attempted')
        if doc.is_repaired:
            return unresolved('The revision reader needed in-memory structural repair; current revision selection remains ambiguous')
        length = doc.xref_length()
        if not 0 < length <= MAX_OBJECTS:
            return unresolved('Current cross-reference table exceeds the bounded object limit')
        objects, absent = {}, []
        for number in range(1, length):
            stage = f'reading current object {number}'
            try:
                source = doc.xref_object(number, compressed=False, ascii=True)
            except RuntimeError as exc:
                # Native xref lookup distinguishes an absent number from an
                # unreadable object. Do not revive such holes from old bytes.
                # A reference to an absent number is checked below.
                if 'cannot find object in xref' not in str(exc):
                    raise
                absent.append(number)
                continue
            if not source:
                continue
            # A returned null is a PDF value (also used for free xref slots),
            # not a failed lookup. Preserve it so valid references to null do
            # not become false dangling-reference warnings. Actual absent
            # entries take the explicit native-error path above.
            value = Values(source).read()
            if has_nonzero_reference(value):
                return unresolved('A current cross-reference object uses nonzero generations; number-only revision resolution was not used')
            objects[(number, 0)] = value
        catalog = doc.pdf_catalog()
        if not isinstance(objects.get((catalog, 0)), dict) or objects[(catalog, 0)].get('/Type') != '/Catalog':
            return unresolved('The current cross-reference catalog could not be verified')
        stage = 'reading the current trailer'
        trailer = Values(doc.pdf_trailer()).read()
        if not isinstance(trailer, dict) or '/Encrypt' in trailer or has_nonzero_reference(trailer):
            return unresolved('The current trailer is encrypted or generation-sensitive; selection remains unresolved')
        current_issues = [s for s in physical_result['limitations']
                          if s != 'Repeated object definitions or incremental updates need additional review']
        references = {number for value in [*objects.values(), trailer] for number in reference_numbers(value)}
        missing = sorted(number for number in references if number != 0 and (number, 0) not in objects)
        if missing:
            current_issues.append('Current definitions reference missing object numbers: ' + ', '.join(map(str, missing[:20])))
        decoded = decoded_total

        def read_script(identity, dictionary):
            nonlocal decoded
            if not doc.xref_is_stream(identity[0]):
                raise InspectionError('The current JavaScript reference is not a stream')
            raw = doc.xref_stream_raw(identity[0])
            if not isinstance(raw, bytes) or len(raw) > MAX_DICTIONARY:
                raise InspectionError('The current JavaScript stream exceeds its raw-data limit')
            text = decode_stream(raw, dictionary, MAX_TEXT)
            decoded += len(text)
            if decoded > MAX_DECODED_TOTAL:
                raise InspectionError('Decoded streams exceed the total inspection limit')
            return text

        stage = 'inspecting current feature definitions'
        result = analyze_objects(data, parser_module, objects, {identity: None for identity in objects},
                                 current_issues, decoded_total, read_script)
        if doc.is_repaired or engine.TOOLS.mupdf_warnings(reset=True):
            return unresolved('The revision reader reported structural warnings; current selection was not accepted as resolved')
        stage = 'reading the revision count'
        resolution.update(status='resolved_current_xref', versions=getattr(doc, 'version_count', None),
                          objects_selected=len(objects), absent_object_numbers=len(absent))
        result['revision_resolution'] = resolution
        result['physical_findings'] = physical_result['physical_findings']
        selected = {(f['feature'], f['object']) for f in result['findings']}
        result['physical_only_findings'] = [f for f in result['physical_findings'] if (f['feature'], f['object']) not in selected]
        result['physical_script_reviews'] = physical_result.get('script_reviews', [])
        result['physical_scan_limitations'] = physical_result['limitations']
        result['compressed_objects_inspected'] = None
        result['parser'] += '; ' + resolution['engine'] + ' current xref'
        result['revision_notice'] = 'Current definitions were selected using the reader’s cross-reference table. Physical-only findings are retained separately and are not evidence of current execution. No older script bodies were analysed.'
        if result['physical_only_findings'] and result['category'] == 'keyword_unconfirmed':
            result['category'] = 'historical_features'
        if any(f['feature'] in {'javascript', 'launch', 'rich_media', 'automatic_action'} for f in result['physical_only_findings']):
            result['priority'] = 'closer review of historical features'
        return result
    except (ValueError, RuntimeError, TypeError, AttributeError) as exc:
        return unresolved('Current revision resolution could not complete while ' + stage + ': ' + type(exc).__name__)
    finally:
        if doc is not None:
            doc.close()


def main():
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        except (ImportError, OSError, ValueError):
            pass
        path = Path(sys.argv[1])
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            raise InspectionError('The worker input is missing, symbolic or over the size limit')
        with contextlib.redirect_stdout(io.StringIO()):
            result = inspect(path.read_bytes())
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__ + ': ' + str(exc)[:240]}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
