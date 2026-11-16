"""Deterministic synthetic corpus; all apparent credentials are invented.

Cases are (case_id, input, canary). No repr of input belongs in a test failure.
The assignment grid crosses every name/style/separator/wrapper, rotating value
classes; password/passphrase additionally cross all multiword value classes.
Provider examples are independent of the implementation's rule data.
"""

import json
from pathlib import Path
import random
import re
import string

SEED = 20261115
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'redact'
STRONG_NAMES = 'password passwd passphrase secret secrets token credential credentials creds signature authorization cookie otp'.split()
NAMES = STRONG_NAMES + ['OPENAI_API_KEY', 'GH_TOKEN', 'ANTHROPIC_AUTH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'DB_PASSWORD', 'client_secret',
    'access_token', 'refreshToken', 'privateKey', 'userPassword', 'TOKEN2', 'API_KEYS', 'GH_PAT', '2FA_TOKEN']
USAGE_NAMES = 'tokens input_tokens output_tokens cache_read_input_tokens cache_creation_input_tokens cached_input_tokens reasoning_output_tokens cacheReadTokens cacheWriteTokens inputTokens outputTokens max_tokens maxOutputTokens token_count token_usage TOKEN_LIMIT token_budget contextWindow utilization'.split()
SEPARATORS = ('=', ' = ', ': ', ':', ':=')


def styles(name):
    parts = re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', '_', name).lower().split('_')
    snake = '_'.join(parts)
    kebab = '-'.join(parts)
    return (snake.upper(), snake, parts[0] + ''.join(p.title() for p in parts[1:]),
            ''.join(p.title() for p in parts), kebab, '.'.join(parts), '--' + kebab,
            'X-' + '-'.join(p.title() for p in parts), 'export ' + snake,
            '$env:' + snake, 'set ' + snake)


def provider_samples():
    rng = random.Random(SEED)
    def body(n, alphabet=string.ascii_letters + string.digits):
        return ''.join(rng.choice(alphabet) for _ in range(n))
    pairs = [
        ('anthropic-admin-api-key', 'sk-ant-admin01-' + body(93) + 'AA'),
        ('anthropic-api-key', 'sk-ant-api03-' + body(93) + 'AA'),
        ('openai-api-key', 'sk-' + body(20) + 'T3BlbkFJ' + body(20)),
        ('github-app-token', 'ghu_' + body(36)),
        ('github-fine-grained-pat', 'github_pat_' + body(82)),
        ('github-oauth', 'gho_' + body(36)), ('github-pat', 'ghp_' + body(36)),
        ('github-refresh-token', 'ghr_' + body(36)), ('gitlab-pat', 'glpat-' + body(20)),
        ('aws-access-token', 'AKIA' + body(16, string.ascii_uppercase + '234567')),
        ('gcp-api-key', 'AIza' + body(35)),
        ('slack-bot-token', 'xoxb-123456789012-234567890123-' + body(24)),
        ('slack-legacy-token', 'xoxs-123-456-789-' + body(24, '0123456789abcdef')),
        ('slack-legacy-workspace-token', 'xoxa-' + body(32)),
        ('slack-user-token', 'xoxp-123456789012-234567890123-345678901234-' + body(32)),
        ('stripe-access-token', 'sk_test_' + body(32)), ('npm-access-token', 'npm_' + body(36)),
        ('pypi-upload-token', 'pypi-AgEIcHlwaS5vcmc' + body(70)),
        ('huggingface-access-token', 'hf_' + body(34, string.ascii_letters)),
        ('jwt', 'eyJ' + body(22) + '.eyJ' + body(22) + '.' + body(32)),
        ('sendgrid-api-token', 'SG.' + body(22) + '.' + body(43)),
        ('twilio-api-key', 'SK' + body(32, '0123456789abcdef')),
        ('telegram-bot-api-token', '123456789:A' + body(34)),
        ('discord-api-token', 'discord=' + body(64, '0123456789abcdef')),
        ('digitalocean-pat', 'dop_v1_' + body(64, '0123456789abcdef')),
        ('linear-api-key', 'lin_api_' + body(40)),
        ('notion-api-token', 'ntn_12345678901' + body(35)),
        ('anthropic-oauth', 'sk-ant-oat01-' + body(64)),
        ('openai-project', 'sk-proj-' + body(64)), ('openai-legacy', 'sk-A9' + body(30)),
        ('slack-prefix', 'xoxr-' + body(40)), ('notion-legacy', 'secret_' + body(40)),
        ('discord-bot', body(24) + '.' + body(6) + '.' + body(32)),
    ]
    return pairs


def must_redact():
    rng = random.Random(SEED)
    case = 0
    # All pairwise intersections of the main structural axes. Value shapes
    # rotate independently; multiword values use quotes or password carriers.
    for name in NAMES:
        for styled in styles(name):
            for sep in SEPARATORS:
                for wrapper in range(17):
                    case += 1
                    canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
                    shape = case % 5
                    value = ('sk-proj-' + canary + 'aB19' * 12 if shape == 0 else
                             canary + ''.join(rng.choice(string.ascii_letters + string.digits) for _ in range(10)) if shape == 1 else
                             'Q%05dZ' % case if shape == 2 else
                             'two words ' + canary if shape == 3 else canary + '-base64_url')
                    if shape == 2:
                        canary = value
                    # Weak names require a secret-shaped leaf; seven chars are
                    # intentionally outside their contract.
                    if name == 'GH_PAT' and shape in (2, 3):
                        value = canary + 'aB9_zX2'
                    if wrapper in (0, 8, 10, 11, 12, 13) and ' ' in value:
                        value = canary + '-multiword-alternate'
                    # Authorization's first word is a scheme, not its secret.
                    if name == 'authorization':
                        value = 'Bearer ' + value
                    clean = styled.removeprefix('--')
                    flag = re.sub(r'^(?:export |set |\$env:)', '', clean)
                    if wrapper == 0:
                        obj = styled + sep + value
                    elif wrapper in (1, 2):
                        quote = '"' if wrapper == 1 else "'"
                        obj = styled + sep + quote + value + quote
                    elif wrapper in (3, 4):
                        obj = json.dumps({styled: value})
                        if wrapper == 4:
                            obj = json.dumps(json.dumps({styled: value}))
                    elif wrapper == 5:
                        obj = styled + ': "' + value + '"'
                    elif wrapper == 6:
                        obj = styled + ': |\n  ' + value + '\n  continued\nnext: ok'
                    elif wrapper == 7:
                        obj = styled + ':\n  ' + value + '\nnext: ok'
                    elif wrapper == 8:
                        obj = 'export ' + styled + '=' + value
                    elif wrapper in (9, 10, 11, 12):
                        delimiter = ('?', '&', '#', ';')[wrapper - 9]
                        value = value.replace(' ', '-')
                        obj = 'https://service.invalid/path' + delimiter + flag + '=' + value + '&ok=1'
                    elif wrapper in (13, 14):
                        obj = '--' + flag + (' ' if wrapper == 13 else '=') + json.dumps(value)
                    elif wrapper == 15:
                        obj = {styled: value, 'ordinary': 12}
                    else:
                        obj = {'nested': [{styled: ('before', value, 17)}]}
                    yield case, obj, canary
    # Dedicated argv containers, short secrets, multiword passwords, base64url.
    for name in NAMES:
        if name != 'GH_PAT':
            case += 1
            canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
            yield case, ['--' + name, canary], canary
    for name in ('password', 'passphrase'):
        for value_kind in range(5):
            case += 1
            canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
            value = ['sk-proj-' + canary + 'aB1_' * 12, canary + 'aB1_' * 3,
                     'Q%05dZ' % case, 'many words ' + canary + ' more', canary + '-_aB19'][value_kind]
            if value_kind == 2:
                canary = value
            yield case, name + '=' + value, canary
    for rid, token in provider_samples():
        # Uniqueness is per case, including fixed-length token alphabets.
        for wrapper in range(5):
            case += 1
            # Change body characters without changing provider structure.
            original = token
            if rid == 'discord-api-token':
                prefix, token_body = original.split('=', 1)
                token = prefix + '=' + ('%02x' % wrapper) + token_body[2:]
                canary = token.split('=', 1)[1]
            elif rid == 'jwt':
                token = original[:-2] + ('%02d' % wrapper)
                canary = token
            elif rid.startswith('anthropic-') and rid != 'anthropic-oauth':
                token = original[:-4] + ('%02d' % wrapper) + 'AA'
                canary = token
            else:
                token = original[:-2] + ('%02d' % wrapper if rid != 'huggingface-access-token' else 'a' + chr(65 + wrapper))
                canary = token
            obj = [token, 'credential received: ' + token + ' done', '"' + token + '"',
                   json.dumps(json.dumps({'message': token})), token + r'\n'][wrapper]
            yield case, obj, canary
    for prefix in ('ghs_', 'ASIA', 'ABIA', 'ACCA', 'A3TX', 'sk-svcacct-', 'sk-admin-', 'rk_live_', 'sk_live_'):
        case += 1
        canary = prefix + ('CNRY%05dQX7LM2PZ9' % case if prefix.startswith(('A',)) else 'CNRY%05dQx7Lm2Pz9Wt4' % case)
        if prefix.startswith('A'):
            canary = prefix + 'QX' + ('%014d' % case)
        elif prefix == 'ghs_':
            canary += 'aB19' * 4
            canary = canary[:4 + 36]
        elif prefix.startswith('sk-'):
            canary += 'aB19' * 12
        yield case, canary, canary
    for name in ('Authorization', 'Proxy-Authorization', 'X-Authorization', 'HTTP_AUTHORIZATION'):
        for scheme in ('Basic', 'Bearer', 'token', 'Custom', 'Digest realm="demo", response=', 'AWS4-HMAC-SHA256 Credential=demo, Signature='):
            for wrapper in range(3):
                case += 1
                canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
                value = scheme + ' ' + canary
                obj = [name + ': ' + value, "curl -H '" + name + ': ' + value + "' https://service.invalid", json.dumps({name: value})][wrapper]
                yield case, obj, canary
    for scheme in ('postgres', 'mysql', 'redis', 'https'):
        for authority in ('user:VALUE@host.invalid', 'user:VALUE@host.invalid:5432/path', 'VALUE@host.invalid/path'):
            case += 1
            canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
            yield case, scheme + '://' + authority.replace('VALUE', canary), canary
    for variant in range(8):
        case += 1
        canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
        label = 'PGP PRIVATE KEY BLOCK' if variant == 2 else 'RSA PRIVATE KEY'
        begin = '-----BEGIN ' + label + '-----\n'
        middle = canary + '\nYWJjZGVmZ2hp\nYWJjZGVmZ2hp\n'
        if variant in (2, 3):
            middle = ('Version: Fake test\n\n' if variant == 2 else 'Proc-Type: 4,ENCRYPTED\n\n') + middle
        obj = begin + middle + ('-----END ' + label + '-----' if variant != 1 else '\n\nremaining private material')
        if variant == 4:
            obj = obj.replace('\n', r'\n')
        elif variant == 5:
            obj = obj.replace('\n', '\r\n')
        elif variant == 6:
            obj = middle + '-----END ' + label + '-----'
        elif variant == 7:
            obj = obj.lower().replace(canary.lower(), canary)
        yield case, obj, canary
    for carrier in ('Cookie: sid=VALUE; csrf=VALUE', 'Set-Cookie: sid=VALUE; Path=/',
                    'curl -u user:VALUE https://service.invalid', 'mysql -u demo -pVALUE',
                    'psql -pVALUE', 'https://service.invalid?sig=VALUE',
                    'https://service.invalid?x=1&amp;code=VALUE'):
        case += 1
        canary = 'CNRY%05dQx7Lm2Pz9Wt4' % case
        yield case, carrier.replace('VALUE', canary), canary
    # Dotted axis is independent of provider regexes. Each shape crosses the
    # full set of text, escape, header, URL, CLI and structured carriers.
    alphabet = string.ascii_letters + string.digits + '_-'
    for shape in range(6):
        for name in ('password', 'token', 'API_KEY', 'key', 'auth', 'GH_PAT'):
            for sample in range(32):
                for carrier in range(29):
                    case += 1
                    body = ''.join(rng.choice(alphabet) for _ in range(96))
                    if shape == 0:
                        value = 's.' + body[:24]
                    elif shape == 1:
                        value = 'hvs.' + body[:91]
                    elif shape == 2:
                        value = 'dp.st.dev.' + body[:40]
                    elif shape == 3:
                        value = ''.join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6)) + '.' + ''.join(rng.choice('0123456789abcdef') for _ in range(16))
                    elif shape == 4:
                        value = 'sk.' + body[:64]
                    else:
                        value = ''.join(rng.choice(string.ascii_letters + string.digits) for _ in range(16)) + '.' + ''.join(rng.choice(string.ascii_letters + string.digits) for _ in range(16))
                    if carrier < 5:
                        obj = name + SEPARATORS[carrier] + value
                    elif carrier < 8:
                        quote = ('"', "'", '`')[carrier - 5]
                        obj = name + '=' + quote + value + quote
                    elif carrier < 12:
                        obj = {name: value}
                        for _ in range(carrier - 7):
                            obj = json.dumps(obj)
                    elif carrier < 14:
                        obj = name + ': ' + ('|\n  ' if carrier == 12 else '\n  ') + value + '\nnext: ok'
                    elif carrier < 16:
                        obj = ('export ' if carrier == 14 else '$env:') + name + '=' + value
                    elif carrier < 20:
                        obj = 'https://service.invalid/path' + ('?', '&', '#', ';')[carrier - 16] + name + '=' + value + '&ok=1'
                    elif carrier < 22:
                        obj = '--' + name + (' ' if carrier == 20 else '=') + value
                    elif carrier == 22:
                        obj = {name: value}
                    elif carrier == 23:
                        obj = {'nested': [{name: ('before', value, 17)}]}
                    elif carrier == 24:
                        obj = ['--' + name, value]
                    elif carrier == 25:
                        obj = 'Authorization: Bearer ' + value
                    elif carrier == 26:
                        obj = 'Cookie: sid=' + value
                    elif carrier == 27:
                        obj = 'https://user:' + value + '@host.invalid/path'
                    else:
                        obj = 'curl -u user:' + value + ' https://host.invalid'
                    yield case, obj, value


def must_keep_usage():
    case = 0
    for name in USAGE_NAMES:
        for styled in styles(name):
            for separator in SEPARATORS:
                for value in ('12000', '12k', '0.0031', 'true'):
                    case += 1
                    yield case, styled + separator + value


def must_keep_streams():
    for line in (FIXTURES / 'streams.jsonl').read_text().splitlines():
        yield line
        yield json.loads(line)


def must_keep_sources():
    case = 0
    for name in ('api_key_source', 'access_token_source', 'credential_source',
                 'password_source', 'secret_source', 'auth_source', 'session_source'):
        for styled in styles(name):
            for value in ('login', 'environment', 'config', 'keychain', 'none'):
                obj = {styled: value}
                for sample in ([styled + sep + value for sep in SEPARATORS] +
                               [json.dumps(obj), json.dumps(json.dumps(obj)),
                                obj, {'nested': [obj]}, ['--' + styled, value]]):
                    case += 1
                    yield case, sample


def must_keep_devlog():
    return (FIXTURES / 'devlog.txt').read_text().splitlines()


def adversarial_families():
    chunks = [('single', 'a'), ('equal', 'a='), ('quoted', '"a":'), ('email', 'a@b.'),
              ('token-empty', 'token='), ('token-value', 'token=x '),
              ('query', '?token=x&'), ('auth-tab', 'Authorization:\tBearer\t'),
              ('begin', '-----BEGIN PRIVATE KEY-----\n'), ('escaped', r'\u0061\n'),
              ('placeholder', '[REDACTED:x] ')]
    prefixes = ['sk-ant-api03-', 'sk-ant-admin01-', 'sk-ant-oat01-', 'sk-proj-', 'sk-svcacct-', 'sk-admin-', 'sk-',
                'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'glpat-', 'AKIA', 'ASIA', 'ABIA', 'ACCA',
                'A3TX', 'AIza', 'xoxa-', 'xoxb-', 'xoxp-', 'xoxo-', 'xoxs-', 'xoxr-', 'sk_live_', 'rk_test_',
                'npm_', 'pypi-', 'hf_', 'eyJ', 'SG.', 'SK', '12345:A', 'discord=', 'dop_v1_', 'lin_api_', 'ntn_', 'secret_']
    chunks.extend(('provider-%d' % i, prefix) for i, prefix in enumerate(prefixes))
    review = [('dash', '-'), ('dot-dash', '.--a'), ('cookie-no-equal', 'Cookie: ' + 'a' * 1024),
              ('reference-call', 'token=a.b('), ('reference-index', 'key=a.b['),
              ('unclosed-template', 'password={{a '), ('placeholder-tail', 'password=xxx '),
              ('quoted-call', '"password":f(1),'), ('unclosed-redacted', '[REDACTED:'),
              ('short-p', ' -px'), ('indented-key', ' ' * 256 + 'key:\n'),
              ('cookie-dense', 'Cookie: a=x; b=y;\n'),
              ('url-no-userinfo', 'http://a:'), ('unclosed-quote', 'token="a\\" '),
              ('email-domain', 'a@' + 'a.' * 256), ('private-label', '-----BEGIN ' + 'A ' * 256),
              ('private-whitespace', '-----BEGIN ' + ' ' * 1024),
              ('angle-no-close', 'token=< '),
              ('query-combined', '?token=x&'), ('orphan-end', '-----END PRIVATE KEY----- ')]
    chunks.extend(review)
    for name, chunk in chunks:
        yield name, chunk
    # All 95 printable ASCII characters, alone and crossed with every review
    # prefix. A mixed block keeps the complete Cartesian product practical.
    for code in range(32, 127):
        char = chr(code)
        yield 'ascii-%d' % code, char
        yield 'ascii-prefixes-%d' % code, ''.join(chunk + char * 128 + '\n' for _, chunk in review)
    for i, (name, chunk) in enumerate(review):
        for other_name, other in review[i + 1:]:
            yield 'mix-' + name + '-' + other_name, chunk + other
    yield 'all-prefixes', ''.join(chunk for _, chunk in review)
    yield 'increasing-indent', ''.join(' ' * i + 'key:\n' for i in range(1, 256))
    yield 'private-long-whitespace', '-----BEGIN ' + ' ' * 1048576
    yield 'cookie-pairs', 'Cookie: ' + 'a=x;' * 262144
    yield 'cookie-long-no-equal', 'Cookie: ' + 'a' * 1048576
    # Force all four decoded views while retaining dense query assignments.
    yield 'query-four-views', r'\\\\\\\\' + '?token=x&' * 128


def adversarial_1mb():
    for name, chunk in adversarial_families():
        yield name, (chunk * (1048576 // len(chunk) + 1))[:1048576]


ADJACENT_FRAGMENTS = ['a@b.invalid','{c@d.invalid','jane@x.invalid','-----BEGIN PRIVATE KEY-----','-----END PRIVATE KEY-----','[REDACTED:x]', '[REDACTED:',']','[','{','}', '-----', ' ', '\\n', '\n', '\"', "'", '=', ':','key=aB3!xyZ', 'key=aB3!xyZ9','token=x','password=xxx ', 'password={{a ', 'token=a.b(', 'key=a.b[', '"password":f(1),','token: string;', 'password="Fake19Secret"','Cookie: a=x;', 'Bearer Fake19MaterialSecret', 'http://user:Fake19@host.invalid', '?token=x&', ' -px', '--- PASS: TestTokenRefresh (0.00s)', 'KeyError:', 'password.py:12:', 'token.interceptor.js:21:16', '${{ secrets.X }}','getpass("Password: ")', 'self.passwd = passwd', 'Idempotency-Key:', 'token_policies=["default"]','self.token','os.environ','dp.st.dev.Fake19CredentialMaterial', 's.Fake19CredentialMaterial', 'sk.Fake19CredentialMaterial', 'abcdef.0123456789abcdef', '%X%', '$X','${X}','{{ x }}','<prompt>','xxx','true','12',';','&','?','@','a@b.c-x', 'git@github.com:org/repo.git', 'sk-proj-'+'Fake9-Ab_'*6, 'ghp_'+'Fake19Ab'*4+'Ab19', r'\u0074oken=Fake19Value', '"token":', 'abc', 'Fake19Secret']
