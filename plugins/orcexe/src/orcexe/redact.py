"""Layered, span-based redaction for display and allow-listed ledger records.

Limits: numeric-only passwords are preserved. Base64-wrapped and line-split
provider keys are not decoded in v1. Heuristics may over-redact ordinary prose;
the realistic developer-log corpus allows at most 2% changed lines.

Known gaps (B10, deferred): five nested JSON escape layers; docker login -p,
sshpass -p, curl -uuser:password; credential-bearing argv pairs (-u, -p) in
objects; os.environ["API_KEY"] assignments; Kubernetes name/value pairs and
{"name":"password","value":...} records; YAML !!str tags; netrc password;
XML password elements; setx; folded Authorization headers; unescaped slash in
URL passwords; ?jwt= and ;jsessionid=; Slack/Discord webhook URLs; pw, passcode,
pswd, connection_string and dsn names; bytes values under secret-named keys.
"""

from array import array
from bisect import bisect_right
from collections import Counter
from functools import lru_cache
import json
import math
import re

from .redact_rules import RULES


STRONG = frozenset('password passwd passphrase secret secrets token credential credentials creds signature authorization authentication cookie otp'.split())
KEY_PREFIXES = frozenset('api secret private access signing client auth master encryption service account license'.split())
TOKENS_PREFIXES = frozenset('access refresh auth id bearer'.split())
WEAK = frozenset('key auth pat pass pwd session sessionid sid pin'.split())
USAGE = frozenset('count counts limit max min usage used budget cost type policy policies length len size id name path file dir url uri endpoint input output cache cached read write total reasoning window prefix helper expires expiry ttl timeout format kind version schema hash fingerprint public source'.split())
COMPOUNDS = dict(apikey=('api', 'key'), accesstoken=('access', 'token'),
                 privatekey=('private', 'key'), secretkey=('secret', 'key'),
                 clientsecret=('client', 'secret'), pgpassword=('pg', 'password'),
                 authtoken=('auth', 'token'), refreshtoken=('refresh', 'token'))
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])')
_PREFIX = re.compile(r'^(?:(?:export|set)\s+|\$env:|\$|-+)', re.I)
_SPLIT = re.compile(r'[^A-Za-z0-9]+')


def _split_components(name):
    name = name.strip()
    name = _PREFIX.sub('', name)
    parts = _SPLIT.split(_CAMEL.sub(' ', name))
    return tuple(piece for part in parts if part for piece in COMPOUNDS.get(part.lower(), (part.lower(),)))


_cached_components = lru_cache(maxsize=4096)(_split_components)


def _components(name):
    return _cached_components(name) if len(name) <= 256 else _split_components(name)


@lru_cache(maxsize=4096)
def classify_name(name: str) -> str:
    """Classify components; only a final usage word overrides a secret.

    Public keys remain metadata. SECRET_ID is explicitly secret-bearing.
    """
    parts = _components(name)
    pairs = list(zip(parts, parts[1:]))
    if parts[-2:] == ('idempotency', 'key') or name.endswith('Error'):
        return 'usage' if parts[-2:] == ('idempotency', 'key') else 'none'
    if 'public' in parts:
        return 'usage'
    if parts[-2:] == ('secret', 'id'):
        return 'strong'
    if parts and parts[-1] in USAGE:
        return 'usage'
    if (STRONG.intersection(parts)
            or any(b in ('key', 'keys') and a in KEY_PREFIXES for a, b in pairs)
            or any(b == 'tokens' and a in TOKENS_PREFIXES for a, b in pairs)):
        return 'strong'
    if 'tokens' in parts:
        return 'usage'
    if WEAK.intersection(parts):
        return 'weak'
    return 'none'


_NUMBER_UNIT = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?(?:\s*(?:k|m|b|usd|eur|ms|s|us|ns|kb|mb|gb|tb|kib|mib|gib|%))?', re.I)
_PLACEHOLDER = re.compile(r'(?:\$\{[^{}]*\}|\$\{\{[^{}]*\}\}|\$[A-Z_][A-Z0-9_]*|\{\{[^{}]*\}\}|%[A-Z_][A-Z0-9_]*%|<(?:prompt|placeholder|redacted)>|[xX*]{3,})')
_REFERENCE = re.compile(r'(?:(?:self|this)\.[A-Za-z_]\w*|os\.environ(?:\[[^\r\n]*\])?|process\.env(?:\.[A-Za-z_]\w*)?|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\([^\r\n]*\)|\[[^\r\n]*\]))')
_TYPE = re.compile(r'(?:str|string|int|number|bool|boolean|float|bytes)(?:;|\s*=.*)?')
_GETPASS = re.compile(r'(?:getpass\.)?getpass\([^\r\n]*\)')


def _entropy(value):
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in Counter(value).values()) if length else 0.0


def _guarded(value):
    value = value.strip()
    return (not value or _PROTECTED.fullmatch(value) is not None
            or value.lower() in ('true', 'false', 'null', 'none', 'token', 'password', 'secret', 'key', 'auth', 'passphrase', 'passwd', 'pwd', 'pass', '|', '>', '|-', '>-', '|+', '>+')
            or _NUMBER_UNIT.fullmatch(value) is not None
            or _PLACEHOLDER.fullmatch(value) is not None)


def _code_reference(value):
    # A fixed window makes the syntax recognizer independent of value length.
    return (len(value) < 20
            and not (any(c.isdigit() for c in value) and any(c.isupper() for c in value))
            and _REFERENCE.fullmatch(value) is not None)


def _secret_value(value, classification='strong', unquoted=False):
    if _guarded(value):
        return False
    if unquoted and classification != 'strong' and _code_reference(value):
        return False
    if classification == 'strong':
        return True
    if classification == 'weak' and _REFERENCE.fullmatch(value) and (len(value) >= 20 or (any(c.isdigit() for c in value) and any(c.isupper() for c in value))):
        return True
    if classification == 'weak' and _SCHEME.match(value):
        return _secret_value(value[_SCHEME.match(value).end():], 'strong')
    if classification != 'weak' or len(value) < 8 or any(c.isspace() for c in value):
        return False
    if value.startswith(('/', './', '~/', '\\')) or re.match(r'^[A-Za-z]:[\\/]', value):
        return False
    classes = sum((any(c.islower() for c in value), any(c.isupper() for c in value),
                   any(c.isdigit() for c in value), any(not c.isalnum() for c in value)))
    return classes >= 3 or _entropy(value) >= 3.0


_ESCAPE = re.compile(r'''\\(?:u[0-9a-fA-F]{4}|["'\\/bfnrt])''')
_UNESCAPE = {"'": "'", '"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}


def _decode_view(text, positions=None):
    """Decode JSON escapes / shell quote escapes, mapping boundaries to raw offsets."""
    chunks = []
    mapping = array('I')
    cursor = 0
    changed = False
    for match in _ESCAPE.finditer(text):
        changed = True
        start, end = match.span()
        chunks.append(text[cursor:start])
        mapping.extend(range(cursor, start) if positions is None else positions[cursor:start])
        code = match.group()[1:]
        chunks.append(chr(int(code[1:], 16)) if code.startswith('u') else _UNESCAPE[code])
        mapping.append(start if positions is None else positions[start])
        cursor = end
    if not changed:
        return None, None
    chunks.append(text[cursor:])
    mapping.extend(range(cursor, len(text) + 1) if positions is None else positions[cursor:])
    return ''.join(chunks), mapping


_COMPILED_RULES = tuple((rule, re.compile(rule['regex'])) for rule in RULES)
_PROTECTED = re.compile(r'\[REDACTED:[^\]\r\n\[]*\]')
_PRIVATE = re.compile(r'(?<!-)-{4,}[ \t]*(BEGIN|END)[ \t]+', re.I)
_PRIVATE_LABEL = re.compile(r'[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?', re.I)
_DASHES = re.compile(r'-{4,}')
_BASE64_LINE = re.compile(r'[A-Za-z0-9+/]{4,}={0,2}')
_EMAIL = re.compile(r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`~@-])[A-Za-z0-9.!#$%&'*+=?^_`~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]+(?![A-Za-z0-9_-]|\.[A-Za-z0-9]|:[~A-Za-z0-9_.-]+/)")
_HEADER = re.compile(r'''(?P<quote>["'`]?)(?<![\w.-])(?P<name>(?:(?:export|set)[ \t]+)?(?:\$env:|\$|--)?[A-Za-z0-9_][A-Za-z0-9_.-]*)(?P=quote)[ \t]*(?P<sep>:=|=>|=(?!=[ \t])|:(?![=:]))[ \t]*''')
_VALUE = re.compile(r'''"(?:\\[^\r\n]|[^"\\\r\n])*"|'(?:\\[^\r\n]|[^'\\\r\n])*'|`(?:\\[^\r\n]|[^`\\\r\n])*`''')
_PLAIN = re.compile(r'''[^\s,;&<>"'`]+''')
_LINE = re.compile(r'[^\r\n]*')
_URL_AUTHORITY = re.compile(r'''\b[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s/?#"'<>]+''')
_URL_FULL = re.compile(r'''\b[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s"'<>]+''')
_QUERY_NAME = re.compile(r'[A-Za-z0-9_.-]+')
_QUERY = re.compile(r'''(?:[?&#;]|&amp;)([A-Za-z0-9_.-]+)=([^\s&#;"'<>]*)''')
_CLI = re.compile(r'(?<![\w.-])--([A-Za-z0-9_.-]+)(?:=|[ \t]+)')
_CURL = re.compile(r'''(?<!\S)(?:-u|--user)(?:[ \t]+|=)["']?[^\s:"']+:''')
_DBCLI = re.compile(r'(?<!\S)-p(?=[^\s])')
_COOKIE = re.compile(r'''(?i)(?<![\w-])(?:set-cookie|cookie)["']?[ \t]*:[ \t]*''')
_COOKIE_PAIR = re.compile(r'''(?<![^;:\s,])([^=:;\s,'"]+)=("(?:\\.|[^"\\])*"|[^;\s,"']+)''')
_AUTH_NAMES = frozenset(('authorization', 'proxy-authorization', 'x-authorization', 'http_authorization', 'authentication', 'x-auth'))
_SCHEME = re.compile(r'(?i)(?:Bearer|Basic|Token|Digest)[ \t]+')
_BEARER = re.compile(r'(?i)(?<![\w.-])Bearer[ \t]+([A-Za-z0-9._~+/-]{16,})')
_COMMAND = re.compile(r'(?<![\w.-])(curl|mysql|psql)(?![\w.-])')
_QUERY_STRONG = frozenset(('sig', 'signature', 'x-amz-signature', 'x-amz-credential', 'code'))


def _private_keys(text):
    # Keep one earliest BEGIN per label. No list(finditer), even for blank lines.
    active = {}
    consumed = 0
    last_marker = -1
    line_start, line_end = 0, -1
    for marker in _PRIVATE.finditer(text):
        if marker.start() < consumed:
            continue
        previous_marker, last_marker = last_marker, marker.start()
        if marker.start() >= line_end:
            line_start = text.rfind('\n', max(0, line_end), marker.start()) + 1
            line_end = text.find('\n', marker.start())
            if line_end < 0:
                line_end = len(text) + 1
        label_end = text.find('-', marker.end())
        if label_end < 0:
            break
        consumed = label_end
        label = text[marker.end():label_end].rstrip(' \t')
        tail = _DASHES.match(text, label_end)
        if not tail or not _PRIVATE_LABEL.fullmatch(label):
            continue
        consumed = tail.end()
        action, label = marker.group(1).upper(), ' '.join(label.upper().split())
        if action == 'BEGIN':
            active.setdefault(label, marker.start())
        elif label in active:
            yield active.pop(label), tail.end(), 'private_key'
        else:
            # A prior marker in this line makes its prefix non-base64. Do not
            # copy/search that growing prefix again for every orphan END.
            if previous_marker >= line_start:
                continue
            cursor = marker.start()
            if cursor and text[cursor - 1] == '\n':
                cursor -= 1
            count = 0
            start = cursor
            while cursor > 0:
                previous = text.rfind('\n', 0, cursor) + 1
                line = text[previous:cursor].rstrip('\r')
                if not _BASE64_LINE.fullmatch(line):
                    break
                start = previous
                count += 1
                cursor = previous - 1
            if count >= 3:
                yield start, tail.end(), 'private_key'
    for start in active.values():
        yield start, len(text), 'private_key'


def _providers(text):
    lower = text.lower()
    for rule, pattern in _COMPILED_RULES:
        if not any(keyword in lower for keyword in rule['keywords']):
            continue
        for match in pattern.finditer(text):
            start, end = match.span('secret') if 'secret' in pattern.groupindex else match.span()
            value = text[start:end]
            if rule['id'] == 'openai-legacy':
                body = value[3:]
                if not (any(c.isupper() for c in body) and any(c.isdigit() for c in body)):
                    continue
            if rule['min_entropy'] is None or _entropy(value) >= rule['min_entropy']:
                yield start, end, rule['kind']


def _value_span(text, start, multiline=False):
    # Template delimiters cannot nest in this recognizer, so failed matches
    # stop at the next opener instead of rescanning an unclosed suffix.
    placeholder = _PLACEHOLDER.match(text, start)
    if placeholder and (placeholder.end() == len(text) or text[placeholder.end()] in ' \t\r\n,;\"\'`'):
        return start, placeholder.end()
    angle = re.compile(r'<[^<>\r\n]*>').match(text, start)
    if angle:
        return start, angle.end()
    reference = _REFERENCE.match(text, start, min(len(text), start + 19))
    if reference and (reference.end() == len(text) or text[reference.end()] in ' \t\r\n,;'):
        return start, reference.end()
    quoted = _VALUE.match(text, start)
    if quoted:
        return quoted.start() + 1, quoted.end() - 1
    # An unclosed quote is consumed through the line, even on a guarded value.
    match = (_LINE if multiline or text[start:start + 1] in ('\"', "'", '`') else _PLAIN).match(text, start)
    return (match.start(), match.end()) if match else (start, start)


def _outer_quote(text, start):
    # A header may be enclosed by curl's -H quote. Search the local prefix only.
    prefix = text[max(0, start - 256):start].rsplit('\n', 1)[-1]
    quoted = None
    escape = False
    for char in prefix:
        if escape:
            escape = False
        elif char == '\\':
            escape = True
        elif quoted == char:
            quoted = None
        elif quoted is None and char in ('"', "'", '`'):
            quoted = char
    return quoted


def _authorization_span(text, start, header):
    quote = text[start] if start < len(text) and text[start] in ('"', "'", '`') else _outer_quote(text, header.start())
    if start < len(text) and text[start] == quote:
        start += 1
    # The scheme is always retained, including unknown schemes.
    scheme = re.match(r'[A-Za-z0-9_-]+[ \t]+', text[start: start + 100])
    if scheme:
        start += scheme.end()
    end = start
    while end < len(text) and text[end] not in '\r\n':
        if text[end] == '\\' and end + 1 < len(text):
            end += 2
        elif quote and text[end] == quote:
            break
        else:
            end += 1
    return start, end


def _yaml_block(text, start, indent):
    marker = re.match(r'(?:[|>][-+]?[ \t]*)?\r?\n', text[start:start + 64])
    if marker is None:
        return None
    line_end = start + marker.end() - 1
    cursor = line_end + 1
    end = cursor
    while cursor < len(text):
        next_end = text.find('\n', cursor)
        if next_end < 0:
            next_end = len(text)
        line = text[cursor:next_end]
        if line.strip() and len(line) - len(line.lstrip(' \t')) <= indent:
            break
        end = next_end
        cursor = next_end + 1
    return (line_end + 1, end) if end > line_end + 1 else None


def _assignments(text):
    consumed = 0
    flat_until = 0
    line_end = -1
    indent = 0
    urls = iter(_URL_FULL.finditer(text))
    url = next(urls, None)
    for header in _HEADER.finditer(text):
        if header.start() < consumed:
            continue
        while url is not None and url.end() <= header.start():
            url = next(urls, None)
        if url is not None and url.start() <= header.start() < url.end():
            continue
        if header.start() >= line_end:
            # Each inter-header gap is traversed once; no backward line search.
            line_start = text.rfind('\n', max(0, line_end), header.start()) + 1
            line_end = text.find('\n', header.start())
            if line_end < 0:
                line_end = len(text) + 1
            indent_end = re.compile(r'[ \t]*').match(text, line_start).end()
            indent = indent_end - line_start
        name = header.group('name')
        # Exact unquoted query assignments are owned by _QUERY. Keeping this
        # path single-owner avoids re-parsing each value in every escape view.
        if (header.start() > 0 and text[header.start() - 1] in '?&#;'
                and not header.group('quote') and header.group('sep') == '='
                and header.start('sep') == header.end('name')
                and header.end() == header.end('sep')
                and text[header.end():header.end() + 1] not in ('"', "'", '`')
                and _QUERY_NAME.fullmatch(name)):
            continue
        classification = classify_name(name)
        if (header.group('sep') == ':' and name.endswith('Error')) or name.lower() == 'idempotency-key':
            continue
        # A file location is diagnostic syntax, even when its path contains a
        # secret word (the header name includes the extension).
        if re.search(r'\.(?:py|js|ts|tsx|jsx|go|rs|c|h)$', name, re.I) and re.match(r'[0-9]+:', text[header.end():header.end() + 32]):
            continue
        cookie_start = header.end()
        if text[cookie_start:cookie_start + 1] in ('\"', "'"):
            cookie_start += 1
        if (name.lower() in ('cookie', 'set-cookie') and header.group('sep') == ':'
                and _COOKIE_PAIR.match(text, cookie_start)):
            continue
        parts = _components(name)
        auth = name.lower() in _AUTH_NAMES or parts[-1:] == ('authorization',)
        if not auth and classification not in ('strong', 'weak'):
            continue
        start = header.end()
        # Escaped opening quotes belong to a decoded view; treating them as
        # unquoted passwords would swallow the rest of the raw JSON line.
        if re.match(r'''\\+["']''', text[start:start + 16]):
            continue
        if (not header.group('quote') and header.start() > 0
                and text[header.start() - 1] in ('\"', "'", '`')
                and text[start:start + 1] == text[header.start() - 1]):
            continue
        if text.startswith(('==', '!='), start):
            continue
        if auth:
            block = _yaml_block(text, start, indent) if header.group('sep') == ':' and start >= flat_until else None
            start, end = block or _authorization_span(text, start, header)
            kind = 'authorization'
        else:
            block = _yaml_block(text, start, indent) if header.group('sep') == ':' and start >= flat_until else None
            multiword = 'password' in parts or 'passphrase' in parts
            start, end = block or _value_span(text, start, multiword)
            kind = 'assignment'
        consumed = max(consumed, end)
        value = text[start:end]
        quoted = text[header.end():header.end() + 1] in ('"', "'", '`')
        # These are explicit source/output syntaxes, not generic references.
        if not auth and not quoted and not block:
            if _TYPE.fullmatch(value) or _GETPASS.fullmatch(value):
                continue
            if classification == 'weak' and name.lower() == 'pass' and value.startswith('Test'):
                continue
            scheme = _SCHEME.match(text, start)
            if scheme:
                start = scheme.end()
                start, end = _value_span(text, start)
                consumed = max(consumed, end)
                value = text[start:end]
                classification = 'strong'
        value_is_secret = _secret_value(value, 'strong' if auth else classification, unquoted=not quoted)
        if not auth and block and classification == 'weak':
            value_is_secret = any(_secret_value(line.strip(), 'weak') for line in value.splitlines())
        if value_is_secret:
            yield start, end, kind
        elif block and classification == 'weak':
            # Inspect inner headers once without rescanning nested YAML blocks.
            flat_until = end
            consumed = start


def _carriers(text):
    for match in _URL_AUTHORITY.finditer(text):
        start = text.find('://', match.start(), match.end()) + 3
        end = text.rfind('@', start, match.end())
        if end < 0:
            continue
        colon = text.find(':', start, end)
        if colon >= 0:
            start = colon + 1
            classification = 'strong'
        else:
            classification = 'weak'
        if _secret_value(text[start:end], classification):
            yield start, end, 'url_userinfo'
    for match in _QUERY.finditer(text):
        name = match.group(1)
        classification = 'strong' if name.lower() in _QUERY_STRONG else classify_name(name)
        if _secret_value(match.group(2), classification):
            yield match.start(2), match.end(2), 'assignment'
    consumed = 0
    for match in _CLI.finditer(text):
        classification = classify_name(match.group(1))
        if match.start() < consumed or classification not in ('strong', 'weak'):
            continue
        start, end = _value_span(text, match.end())
        quoted = text[match.end():match.end() + 1] in ('\"', "'", '`')
        scheme = None if quoted else _SCHEME.match(text, start)
        if scheme:
            start, end = _value_span(text, scheme.end())
            classification = 'strong'
        consumed = end
        if _secret_value(text[start:end], classification, unquoted=not quoted):
            yield start, end, 'assignment'
    for pattern in (_CURL, _DBCLI):
        if ('curl' if pattern is _CURL else '-p') not in text:
            continue
        commands = iter(_COMMAND.finditer(text))
        command = next(commands, None)
        last_command_end = -1
        wanted = ('curl',) if pattern is _CURL else ('mysql', 'psql')
        for match in pattern.finditer(text):
            while command is not None and command.end() <= match.start():
                if command.group(1) in wanted:
                    last_command_end = command.end()
                command = next(commands, None)
            if last_command_end < 0 or '\n' in text[last_command_end:match.start()]:
                last_command_end = -1
                continue
            # Advance even when no value is redacted: no repeated prefix slice.
            last_command_end = match.end()
            start, end = _value_span(text, match.end())
            if _secret_value(text[start:end]):
                yield start, end, 'assignment'
    cookie_end = 0
    for header in _COOKIE.finditer(text):
        if header.start() < cookie_end:
            continue
        start, end = _authorization_span(text, header.end(), header)
        # Cookie has no authorization scheme: start at the header value.
        start = header.end()
        cookie_end = end
        for match in _COOKIE_PAIR.finditer(text, start, end):
            a, b = match.span(2)
            if text[a:a + 1] == '"':
                a, b = a + 1, b - 1
            if not text[a:b].startswith('[REDACTED:'):
                yield a, b, 'assignment'


def _detect(text):
    yield from _private_keys(text)
    yield from _providers(text)
    yield from _assignments(text)
    yield from _carriers(text)
    for match in _BEARER.finditer(text):
        if _guarded(match.group(1)):
            continue
        yield match.start(1), match.end(1), 'authorization'
    authorities = iter(_URL_AUTHORITY.finditer(text))
    authority = next(authorities, None)
    for match in _EMAIL.finditer(text):
        while authority is not None and authority.end() <= match.start():
            authority = next(authorities, None)
        if authority is not None and authority.start() <= match.start() < authority.end():
            continue
        yield match.start(), match.end(), 'email'


_PRIORITY = {'private_key': 6, 'authorization': 4, 'url_userinfo': 3, 'assignment': 2, 'email': 1}


def _merged(spans):
    """Union intersecting extents; detector priority owns the replacement kind."""
    spans.sort(key=lambda span: span[0])
    current = None
    winner = None
    for start, end, kind in spans:
        score = (_PRIORITY.get(kind, 5), end - start)
        if current is not None and start < current[1]:
            current = (current[0], max(end, current[1]), kind if score > winner else current[2])
            winner = max(winner, score)
        else:
            if current is not None:
                yield current
            current, winner = (start, end, kind), score
    if current is not None:
        yield current


def _stable_carrier(text):
    """Validate a complete carrier before omitting the boundary-closing pass."""
    if '@' in text:
        return False
    stripped = text.lstrip('\\ \t\r\n')
    if stripped.startswith(('?', '&', '#', ';')):
        return re.search(r'\s', stripped) is None
    header = _COOKIE.match(text)
    if header is None:
        return False
    cursor = header.end()
    while cursor < len(text):
        pair = _COOKIE_PAIR.match(text, cursor)
        if pair is None:
            return False
        cursor = pair.end()
        while cursor < len(text) and text[cursor] in ' \t':
            cursor += 1
        if cursor == len(text):
            return True
        if text[cursor] != ';':
            return False
        cursor += 1
        while cursor < len(text) and text[cursor] in ' \t':
            cursor += 1
    return True


def _redact_once(text: str) -> tuple:
    """Return rewritten text and whether carrier delimiters prove it stable."""
    if not isinstance(text, str):
        raise TypeError('redact() requires str input')
    spans = []
    seen = None
    view, positions = text, None
    # Two nested JSON strings need up to three escape layers. Bounded decoding
    # avoids an attacker controlling iteration count or the recursion stack.
    for level in range(4):
        protected = iter(_PROTECTED.finditer(view))
        # Detectors are unsorted. Binary-searching protected intervals avoids a
        # quadratic cross product on already redacted large input.
        protected_spans = [(m.start(), m.end()) for m in protected]
        protected_starts = [p[0] for p in protected_spans]
        for start, end, kind in _detect(view):
            if start >= end or view.startswith('[REDACTED:', start):
                continue
            index = bisect_right(protected_starts, start) - 1
            if index >= 0 and protected_spans[index][1] > start:
                continue
            span = (start, end, kind) if positions is None else (positions[start], positions[end], kind)
            if seen is None or span not in seen:
                spans.append(span)
                if seen is not None:
                    seen.add(span)
        if '\\' not in view or level == 3:
            break
        if seen is None:
            seen = set(spans)
        view, positions = _decode_view(view, positions)
        if view is None:
            break
    del seen
    parts = []
    cursor = 0
    replacements = {}
    stable = True
    for start, end, kind in _merged(spans):
        # '=' on the left and a field delimiter/EOF on the right remain fixed.
        # No outside token acquires a new boundary when this field is replaced.
        stable = stable and kind == 'assignment' and start > 0 and text[start - 1] == '=' and (end == len(text) or text[end] in '&;')
        if kind not in replacements:
            replacements[kind] = '[REDACTED:' + kind + ']'
        parts.extend((text[cursor:start], replacements[kind]))
        cursor = end
    parts.append(text[cursor:])
    stable = stable and _stable_carrier(text)
    return ''.join(parts), stable


def redact_text(text: str) -> str:
    """Apply at most two bounded detector passes, closing replacement boundaries."""
    once, stable = _redact_once(text)
    return _redact_once(once)[0] if once != text and not stable else once


def redact(text: str) -> str:
    """Compatibility alias for redact_text."""
    return redact_text(text)


def _json_string(value, transform):
    if value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            # Infer separators outside quoted strings, never from value text.
            separators = [',', ':']
            quoted = escape = False
            for i, char in enumerate(value):
                if escape:
                    escape = False
                elif char == '\\' and quoted:
                    escape = True
                elif char == '"':
                    quoted = not quoted
                elif not quoted and char in ',:':
                    separators[0 if char == ',' else 1] = char + (' ' if value[i + 1:i + 2] == ' ' else '')
            try:
                return json.dumps(transform(parsed), separators=tuple(separators), ensure_ascii=False)
            except (ValueError, RecursionError):
                return '[REDACTED:depth]'
    return redact_text(value)


def _walk(obj, depth=0, force=False):
    if depth > 200:
        return '[REDACTED:depth]'
    if isinstance(obj, str):
        return '[REDACTED:assignment]' if force else _json_string(obj, lambda value: _walk(value, depth + 1))
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            classification = classify_name(key.strip()) if isinstance(key, str) else 'none'
            secret = (classification == 'strong' and (not isinstance(value, str) or _secret_value(value))) or (classification == 'weak' and isinstance(value, str) and _secret_value(value, 'weak'))
            if classification == 'weak' and isinstance(value, (dict, list, tuple)):
                # Decide weak containers from their leaves, then redact the
                # entire subtree when at least one leaf has secret shape.
                secret = _has_secret_leaf(value)
            clean_key = redact_text(key) if isinstance(key, str) else key
            result[clean_key] = _walk(value, depth + 1, force or secret)
        return result
    if isinstance(obj, (list, tuple)):
        result = []
        previous = None
        for value in obj:
            classification = classify_name(previous) if isinstance(previous, str) and previous.startswith('--') else 'none'
            secret = classification in ('strong', 'weak') and isinstance(value, str) and _secret_value(value, classification)
            result.append(_walk(value, depth + 1, force or secret))
            previous = value
        return tuple(result) if isinstance(obj, tuple) else result
    return obj


def _has_secret_leaf(obj):
    pending = [(obj, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 200:
            return True
        if isinstance(value, str) and _secret_value(value, 'weak'):
            return True
        if isinstance(value, (dict, list, tuple)):
            pending.extend((v, depth + 1) for v in (value.values() if isinstance(value, dict) else value))
    return False


def redact_obj(obj):
    """Copy containers, hide secret subtrees and redact textual keys/values."""
    return _walk(obj)


def sanitize_record(obj: dict, schema: dict, text_limit: int = 2000) -> dict:
    """Copy only schema-approved fields; record dropped names, never values.

    ``str`` and ``text`` are both redacted (including embedded JSON); only text
    is length-limited. Each nested record has its own sorted ``_dropped`` list.
    Numeric maps reject booleans and drop nonnumeric entries individually.
    B11: str/text leaves do not classify their schema field name. The caller
    must exclude bare secrets (e.g. {"password": "str"}); only contents and
    embedded JSON are inspected. This is a schema-author responsibility.
    """
    if not isinstance(obj, dict) or not isinstance(schema, dict):
        raise TypeError('sanitize_record() requires dict input and schema')
    if not isinstance(text_limit, int) or isinstance(text_limit, bool) or text_limit < 0:
        raise ValueError('text_limit must be a nonnegative integer')

    def clean(record, shape, depth=0):
        result, dropped = {}, []
        for key, value in record.items():
            spec = shape.get(key)
            valid = False
            output = None
            if key != '_dropped' and depth <= 200:
                if isinstance(spec, dict) and isinstance(value, dict):
                    output, valid = clean(value, spec, depth + 1), True
                elif spec in ('str', 'text') and isinstance(value, str):
                    output = _json_string(value, redact_obj)
                    if spec == 'text' and len(output) > text_limit:
                        output = output[:text_limit] + '…[truncated]'
                    valid = True
                elif spec == 'num' and type(value) in (int, float):
                    output, valid = value, True
                elif spec == 'bool' and type(value) is bool:
                    output, valid = value, True
                elif spec == 'any_num_map' and isinstance(value, dict):
                    output = clean(value, {k: 'num' for k in value}, depth + 1)
                    valid = True
            if valid:
                result[key] = output
            else:
                dropped.append(str(key))
        if dropped:
            result['_dropped'] = sorted(dropped)
        return result

    return clean(obj, schema)
