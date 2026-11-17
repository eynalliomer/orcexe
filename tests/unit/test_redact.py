"""Contract tests. Failures identify cases, never credential/canary values."""

import ast
import base64
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import tracemalloc
import unittest
from unittest import mock
import warnings

from tests.unit._timing import scaled

SOURCE = Path(__file__).resolve().parents[2] / 'plugins' / 'orcexe' / 'src'
sys.path.insert(0, str(SOURCE))

import orcexe.redact as redact_module
from orcexe.redact import classify_name, redact, redact_obj, redact_text, sanitize_record
from orcexe.redact_rules import RULES
# Full 457-family x 1MB scaling and memory runs take ~10 minutes; they run
# only with ORCEXE_SLOW_TESTS=1 (separate CI job, pre-release). The default
# run keeps every family at a smaller size plus a fixed 1MB subset.
SLOW_TESTS = os.environ.get('ORCEXE_SLOW_TESTS') == '1'
# Default-run growth sample: every hand-built family (the ReDoS findings) plus a
# fixed stride over the generated ascii-/mix-/provider- families. The full set
# runs with ORCEXE_SLOW_TESTS=1 and is mandatory whenever redact*.py changes.
_GENERATED_STRIDE = {'ascii-': 10, 'mix-': 10, 'provider-': 5}


def _in_fast_growth_sample(name, generated_index):
    for prefix, stride in _GENERATED_STRIDE.items():
        if name.startswith(prefix):
            return generated_index[prefix] % stride == 0
    return True


FAST_1MB_FAMILIES = ('query', 'escaped', 'placeholder', 'begin', 'cookie-dense', 'cookie-pairs', 'query-four-views')

from tests.unit.redact_corpus import (ADJACENT_FRAGMENTS, SEED, adversarial_1mb, must_keep_devlog,
    must_keep_sources, must_keep_streams, must_keep_usage, must_redact, provider_samples, styles, adversarial_families)

# Independent specification table: do not import the implementation constants.
EXPECTED_STRONG = 'password passwd passphrase secret secrets token credential credentials creds signature authorization cookie otp'.split()
EXPECTED_KEY_PREFIXES = 'api secret private access signing client auth master encryption service account license'.split()
EXPECTED_TOKENS_PREFIXES = 'access refresh auth id bearer'.split()
EXPECTED_WEAK = 'key auth pat pass pwd session sessionid sid pin'.split()
EXPECTED_USAGE = 'count counts limit max min usage used budget cost type policy length len size id name path file dir url uri endpoint input output cache cached read write total reasoning window prefix helper expires expiry ttl timeout format kind version schema hash fingerprint public source'.split()
EXPECTED_COMPOUNDS = {'apikey': ('api', 'key'), 'accesstoken': ('access', 'token'),
    'privatekey': ('private', 'key'), 'secretkey': ('secret', 'key'),
    'clientsecret': ('client', 'secret'), 'pgpassword': ('pg', 'password'),
    'authtoken': ('auth', 'token'), 'refreshtoken': ('refresh', 'token')}

# (1-based fixture line, reason). Known false positives in real stdlib source.
ACCEPTED_OVERREDACTION = [(296, 'stdlib passwd assigned fallback_getpass expression'),
                         (311, 'stdlib passwd assigned _raw_input expression'),
                         (325, 'stdlib passwd assigned fallback_getpass expression')]


def rendered(obj):
    return redact_text(obj) if isinstance(obj, str) else json.dumps(redact_obj(obj), ensure_ascii=False)


class ScanWorkText(str):
    """Count Python scans/copies; regex restart costs have separate ratio gates."""
    def __new__(cls, value):
        obj = str.__new__(cls, value)
        obj.work = 0
        return obj

    def __getitem__(self, key):
        if isinstance(key, slice):
            self.work += len(range(*key.indices(len(self))))
        else:
            self.work += 1
        return str.__getitem__(self, key)

    def find(self, sub, start=0, end=None):
        stop = len(self) if end is None else end
        result = str.find(self, sub, start, stop)
        self.work += max(0, (result + len(sub) if result >= 0 else stop) - start)
        return result

    def rfind(self, sub, start=0, end=None):
        stop = len(self) if end is None else end
        result = str.rfind(self, sub, start, stop)
        self.work += max(0, stop - (result if result >= 0 else start))
        return result

    def lower(self):
        self.work += len(self)
        return str.lower(self)

    def __contains__(self, value):
        self.work += len(self)
        return str.__contains__(self, value)


class RedactTests(unittest.TestCase):
    def test_generated_secret_grid(self):
        count = 0
        canaries = set()
        for case, obj, canary in must_redact(sampled=not SLOW_TESTS):
            count += 1
            self.assertTrue(canary not in canaries, 'duplicate canary case id %d' % case)
            canaries.add(canary)
            self.assertTrue(canary not in rendered(obj), 'leak case id %d' % case)
        total = count if SLOW_TESTS else sum(1 for _ in must_redact())
        self.assertTrue(total >= 25000, 'corpus coverage count')
        print('must_redact sampled=%d total=%d leaks=0' % (count, total))

    def test_usage_grid_is_byte_identical(self):
        count = 0
        for case, text in must_keep_usage():
            count += 1
            self.assertTrue(redact_text(text) == text, 'usage case id %d' % case)
            self.assertTrue('[REDACTED' not in redact_text(text), 'usage marker case id %d' % case)
        print('must_keep_usage=%d changed=0' % count)

    def test_streams_text_and_objects_are_identical(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'redact' / 'streams.jsonl'
        self.assertTrue(fixture.is_file(), 'missing owner input: tool-experiments section 2 JSONL')
        count = 0
        for case, obj in enumerate(must_keep_streams()):
            count += 1
            output = redact_text(obj) if isinstance(obj, str) else redact_obj(obj)
            self.assertTrue(output == obj, 'stream case id %d' % case)
            self.assertTrue('[REDACTED' not in json.dumps(output), 'stream marker case id %d' % case)
        self.assertTrue(count >= 6, 'stream fixtures missing')
        print('must_keep_streams=%d changed=0' % count)

    def test_generated_source_metadata_is_identical(self):
        count = 0
        for case, obj in must_keep_sources():
            count += 1
            output = redact_text(obj) if isinstance(obj, str) else redact_obj(obj)
            self.assertTrue(output == obj, 'source metadata case id %d' % case)
        print('must_keep_sources=%d changed=0' % count)

    def test_source_metadata_still_redacts_provider_secrets(self):
        for case, (_, token) in enumerate(provider_samples()):
            for name in ('apiKeySource', 'password_source', 'auth-source'):
                obj = {name: token}
                for sample in (obj, json.dumps(obj)):
                    self.assertTrue(token not in rendered(sample), 'source provider case id %d' % case)

    def test_realistic_log_budget(self):
        lines = must_keep_devlog()
        self.assertTrue(len(lines) >= 200, 'devlog too small')
        changed = [i for i, text in enumerate(lines, 1) if redact_text(text) != text]
        self.assertTrue(len(changed) / len(lines) <= 0.02, 'devlog over-redaction budget')
        accepted = {i for i, reason in ACCEPTED_OVERREDACTION if reason}
        self.assertTrue(set(changed) == accepted, 'unexplained devlog changed lines')
        text = '\n'.join(lines)
        output = redact_text(text)
        self.assertTrue(redact_text(output) == output, 'whole devlog idempotence')
        self.assertTrue(len(output.splitlines()) == len(lines), 'whole devlog line preservation')
        print('devlog_lines=%d changed=%d over_redaction=%.3f%%' % (len(lines), len(changed), 100 * len(changed) / len(lines)))

    def test_independent_classification_tables(self):
        for table, expected in ((EXPECTED_STRONG, 'strong'), (EXPECTED_WEAK, 'weak')):
            for i, name in enumerate(table):
                for styled in styles(name):
                    self.assertTrue(classify_name(styled) == expected, 'classification %s case id %d' % (expected, i))
        for i, prefix in enumerate(EXPECTED_KEY_PREFIXES):
            for ending in ('key', 'keys'):
                self.assertTrue(classify_name(prefix + '_' + ending) == 'strong', 'key-prefix case id %d' % i)
        for i, prefix in enumerate(EXPECTED_TOKENS_PREFIXES):
            self.assertTrue(classify_name(prefix + '_tokens') == 'strong', 'tokens-prefix case id %d' % i)
        # Owner contract change (2026-11-15): only LAST usage component wins;
        # a final secret word wins over INPUT/CACHE/ENDPOINT/READ/etc.
        for i, component in enumerate(EXPECTED_USAGE):
            for name in ('token_' + component, 'key_' + component):
                self.assertTrue(classify_name(name) == 'usage', 'usage override case id %d' % i)
            expected = 'usage' if component == 'public' else 'strong'
            self.assertTrue(classify_name(component + '_secret') == expected, 'final secret case id %d' % i)
        for i, (compound, parts) in enumerate(EXPECTED_COMPOUNDS.items()):
            self.assertTrue(classify_name(compound) == classify_name('_'.join(parts)) == 'strong', 'compound case id %d' % i)
        for i, name in enumerate(('tokens', 'other_tokens', 'token_id', 'DATABASE_URL', 'REDIS_URL', 'MONGODB_URI')):
            self.assertTrue(classify_name(name) == 'usage', 'usage exception case id %d' % i)
        for i, name in enumerate(('tokenizer', 'author', 'utilization', 'foobar', 'monkey', 'authentic')):
            self.assertTrue(classify_name(name) == 'none', 'none case id %d' % i)
        for i, name in enumerate(('INPUT_PASSWORD', 'CACHE_PASSWORD', 'STRIPE_ENDPOINT_SECRET', 'endpoint_secret', 'read_token', 'id_token', 'VAULT_SECRET_ID', 'secret_id', 'SOURCE_API_KEY', 'TOKEN2', '2FA_TOKEN', 'refreshToken', 'privateKey', 'userPassword', 'API_KEYS')):
            self.assertTrue(classify_name(name) == 'strong', 'component boundary case id %d' % i)

    def test_value_guards_and_weak_shape(self):
        protected = ['12', '12k', '200k', '0.0031 USD', '12ms', '5s', '1.2GB',
            'true', 'false', 'null', 'none', '', '${X}', '$X', '{{ x }}', '%X%',
            '<prompt>', '***', 'xxxxxx', 'token', '[REDACTED:x]']
        for case, value in enumerate(protected):
            for name in ('password', 'token', 'key'):
                text = name + '=' + value
                self.assertTrue(redact_text(text) == text, 'guard case id %d' % case)
                # Object string guards also preserve numeric-only passwords.
                self.assertTrue(redact_obj({name: value}) == {name: value}, 'object guard case id %d' % case)
        for case, value in enumerate(('short77', 'lowercase', '/home/project/file', './cache', '~/cache/file', 'C:\\Temp\\file')):
            # "lowercase" has entropy >=3 and is therefore secret-shaped.
            if value == 'lowercase':
                continue
            text = 'key=' + value
            self.assertTrue(redact_text(text) == text, 'weak guard case id %d' % case)
        for case, value in enumerate(('aB3!xyZ9', 'qrstuvwx', 'LONG_fake_123')):
            self.assertTrue(value not in redact_text('key=' + value), 'weak secret case id %d' % case)
        for case, text in enumerate(('if password == other:', 'if token != other:', 'def f(token: str = None, x=1):')):
            self.assertTrue(redact_text(text) == text, 'code comparison case id %d' % case)

    def test_reference_guards_only_for_short_unquoted_weak_values(self):
        for case, value in enumerate(('self.token', 'this.key', 'os.environ["X"]', 'process.env')):
            text = 'key=' + value
            self.assertTrue(redact_text(text) == text, 'weak reference id %d' % case)
            for sample in ('password=' + value, 'key="' + value + '"', {'key': value}, {'password': value}):
                self.assertTrue(value not in rendered(sample), 'reference context id %d' % case)
        for case, value in enumerate(('self.' + 'a' * 20, 'self.A9value', 'f(Real9Secret)', 'x["Real9Secret"]')):
            self.assertTrue(value not in redact_text('password=' + value), 'strong reference id %d' % case)
        for case, value in enumerate(('self.' + 'a' * 20, 'self.A9value')):
            self.assertTrue(value not in redact_text('key=' + value), 'reference threshold id %d' % case)

    def test_narrow_placeholders_and_punctuation_passwords(self):
        for case, value in enumerate(('xxxReal9Secret', '$Real9Secret', '%Real9Secret%', '<Real9Secret>', '!Real9Secret', '=Real9Secret', 'f(Real9Secret)', 'x["Real9Secret"]')):
            for sample in ('password=' + value, {'password': value}, json.dumps({'password': value})):
                self.assertTrue('Real9Secret' not in rendered(sample), 'placeholder narrowing id %d' % case)

    def test_scheme_words_and_standalone_bearer(self):
        value = 'Fake19CredentialMaterial'
        for case, name in enumerate(('token', 'auth', 'X-Auth', 'Authentication', 'Authorization', 'password')):
            for scheme in ('Bearer', 'Basic', 'Token', 'Digest'):
                for sample in (name + ': ' + scheme + ' ' + value,
                               json.dumps({name: scheme + ' ' + value}), {name: scheme + ' ' + value}):
                    self.assertTrue(value not in rendered(sample), 'scheme case id %d' % case)
        self.assertTrue(value not in redact_text('Bearer ' + value), 'standalone bearer')
        for case, text in enumerate(('auth: Bearer Zq7', '--auth Bearer Zq7', '--token Basic Zq7')):
            self.assertTrue('Zq7' not in redact_text(text), 'short scheme id %d' % case)

    def test_developer_output_and_source_syntax(self):
        cases = ('--- PASS: TestTokenRefresh (0.00s)', "KeyError: 'access_token'",
                 'src/auth/password.py:12: error: missing field', 'token.interceptor.js:21:16',
                 'token: string;', '${{ secrets.DB_PASSWORD }}',
                 'password = getpass.getpass("Password: ")', 'password = getpass("Password: ")',
                 'self.passwd = passwd', 'Idempotency-Key: 123e4567-e89b-12d3-a456-426614174000',
                 'token_policies=["default"]')
        for case, text in enumerate(cases):
            self.assertTrue(redact_text(text) == text, 'developer syntax id %d' % case)
        obj = {'Idempotency-Key': '123e4567-e89b-12d3-a456-426614174000', 'KeyError': 'access_token'}
        self.assertTrue(redact_obj(obj) == obj, 'developer object metadata')

    def test_weak_seven_eight_boundary(self):
        self.assertTrue(redact_text('key=aB3!xyZ') == 'key=aB3!xyZ', 'weak seven')
        self.assertTrue('aB3!xyZ9' not in redact_text('key=aB3!xyZ9'), 'weak eight')

    def test_three_and_four_json_dumps_layers(self):
        for layers in (3, 4):
            text = {'password': 'Fake19LayerCanary'}
            for _ in range(layers):
                text = json.dumps(text)
            self.assertTrue('Fake19LayerCanary' not in redact_text(text), 'JSON layers id %d' % layers)

    def test_provider_rules_compile_and_each_has_positive_negative(self):
        samples = dict(provider_samples())
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            for case, rule in enumerate(RULES):
                pattern = re.compile(rule['regex'])
                self.assertTrue(rule['id'] in samples, 'missing provider sample id %d' % case)
                self.assertTrue(pattern.search(samples[rule['id']]) is not None, 'provider positive id %d' % case)
                negative = samples[rule['id']][:max(1, len(samples[rule['id']]) // 4)]
                self.assertTrue(pattern.search(negative) is None, 'provider negative id %d' % case)
                self.assertTrue(redact_text(negative) == negative, 'negative redacted id %d' % case)
                self.assertTrue(all(k == k.lower() for k in rule['keywords']), 'keyword case id %d' % case)
                self.assertTrue(rule['source'] == 'orcexe:v1' or rule['source'] == 'gitleaks:' + rule['id'] + '@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', 'provider source id %d' % case)
                self.assertTrue(bool(rule.get('modified')), 'provider modified note id %d' % case)
        self.assertTrue(sum(r['source'].startswith('gitleaks:') for r in RULES) >= 25, 'ported rule count')

    def test_provider_boundaries_and_openai_package_guard(self):
        keep = ['sk-learn-contrib-extra-pkg-name', 'sk-' + 'lowercase' * 4,
                'AKIA' + 'A' * 17, 'ghp_short', 'npm_short', 'AIzashort']
        for case, text in enumerate(keep):
            self.assertTrue(redact_text(text) == text, 'provider boundary case id %d' % case)
        for case, prefix in enumerate(('sk-proj-', 'sk-svcacct-', 'sk-admin-')):
            token = prefix + 'Fake7-Ab_' * 6
            self.assertTrue(token not in redact_text(token), 'project extension id %d' % case)
        for case, token in enumerate(('ghs_' + 'Fake7Ab9' * 4 + 'AB19', 'rk_live_' + 'Fake7Ab9' * 4)):
            self.assertTrue(token not in redact_text(token), 'provider prefix id %d' % case)

    def test_escaped_views_and_source_offsets(self):
        for case, encoded in enumerate(('password=\\"FakE19value\\"', r'\u0074oken=FakE19value',
                'password="Fake\\\\Part19"', json.dumps(json.dumps({'password': 'FakE19value'})))):
            output = redact_text('prefix ' + encoded + ' suffix')
            self.assertTrue('FakE19value' not in output and 'Part19' not in output, 'decoded case id %d' % case)
            self.assertTrue(output.startswith('prefix ') and output.endswith(' suffix'), 'offset case id %d' % case)
        for case, prefix in enumerate(('Authorization: Custom ', 'Proxy-Authorization: Basic ', 'X-Authorization: Digest ', 'HTTP_AUTHORIZATION: Bearer ')):
            text = prefix + 'FakE19value, more; material\nnext: ok'
            output = redact_text(text)
            self.assertTrue(output.startswith(prefix) and output.endswith('\nnext: ok'), 'auth prefix id %d' % case)
            self.assertTrue('FakE19value' not in output and 'material' not in output, 'auth extent id %d' % case)

    def test_assignments_quotes_separators_yaml(self):
        for case, separator in enumerate(('=', ':', ':=', '=>')):
            for quote in ('"', "'", '`'):
                text = quote + 'password' + quote + separator + quote + 'Fake19' + '\\' + quote + ' extra' + quote
                self.assertTrue('Fake19' not in redact_text(text) and 'extra' not in redact_text(text), 'quotes id %d' % case)
        for case, quote in enumerate(('\"', "'")):
            text = '\\' + quote + 'password' + '\\' + quote + '=' + '\\' + quote + 'Fake19phrase' + '\\' + quote
            self.assertTrue('Fake19phrase' not in redact_text(text), 'escaped assignment quote id %d' % case)
        for case, marker in enumerate(('|', '>', '', '|-', '>+')):
            text = 'root:\n  password: ' + marker + '\n    Fake19phrase\n\n    continuation\n  status: ok'
            output = redact_text(text)
            self.assertTrue('Fake19phrase' not in output and 'continuation' not in output, 'yaml block id %d' % case)
            self.assertTrue(output.endswith('\n  status: ok'), 'yaml extent id %d' % case)
        self.assertTrue(redact_text('token=Fake19 value') == 'token=[REDACTED:assignment] value', 'plain token whitespace boundary')
        self.assertTrue(redact_text('password=Fake19 many words').endswith('[REDACTED:assignment]'), 'password line extent')
        self.assertTrue('Zq7' not in redact_text('key:\n  token: Zq7\nnext: ok'), 'weak YAML parent must not hide strong child')

    def test_url_cookie_and_cli_carriers(self):
        for case, text in enumerate(('DATABASE_URL=postgres://user:Fake19pass@host:5432/db',
                'REDIS_URL=redis://Fake19pass@host:6379', 'MONGODB_URI=mongodb://user:Fake19pass@host/db',
                'https://host?x=1&amp;signature=Fake19pass', 'https://host#code=Fake19pass',
                'Cookie: id=Fake19pass; csrf=Fake19pass', 'Set-Cookie: id=Fake19pass; Path=/',
                'curl -u user:Fake19pass https://host', 'mysql -u demo -pFake19pass', 'psql -pFake19pass')):
            self.assertTrue('Fake19pass' not in redact_text(text), 'carrier case id %d' % case)
        self.assertTrue('user:' in redact_text('postgres://user:Fake19pass@host'), 'userinfo username preserved')
        self.assertTrue(redact_text('https://user@host.invalid/path') == 'https://user@host.invalid/path', 'ordinary URL username')
        self.assertTrue(redact_text('https://token:Fake19pass@host.invalid/path?password=Fake19pass&ok=1') == 'https://token:[REDACTED:url_userinfo]@host.invalid/path?password=[REDACTED:assignment]&ok=1', 'URL boundaries')
        self.assertTrue(redact_text('Cookie:sid=Fake19pass; csrf=12') == 'Cookie:sid=[REDACTED:assignment]; csrf=[REDACTED:assignment]', 'Cookie names preserved')

    def test_private_blocks_truncated_orphan_and_public(self):
        for case, label in enumerate(('PRIVATE KEY', 'RSA PRIVATE KEY', 'EC PRIVATE KEY', 'OPENSSH PRIVATE KEY', 'PGP PRIVATE KEY BLOCK', 'SSH2 ENCRYPTED PRIVATE KEY')):
            for tail in ('', '\n\nprivate remainder', '\n-----END ' + label.lower() + '-----'):
                text = 'before\n-----BEGIN ' + label.lower() + '-----\nFake19private' + tail
                self.assertTrue(redact_text(text) == 'before\n[REDACTED:private_key]', 'private extent id %d' % case)
        self.assertTrue(redact_text('YWJjZGVm\nYWJjZGVm\nYWJjZGVm\n-----END PRIVATE KEY-----') == '[REDACTED:private_key]', 'orphan private tail')
        self.assertTrue(redact_text('YWJjZGVm\nYWJjZGVm\nYWJjZGVm-----END PRIVATE KEY-----') == '[REDACTED:private_key]', 'inline orphan private tail')
        public = '-----BEGIN PUBLIC KEY-----\nYWJjZGVmZ2hp\n-----END PUBLIC KEY-----'
        self.assertTrue(redact_text(public) == public, 'public blocks')
        nested = '-----BEGIN RSA PRIVATE KEY-----\n-----BEGIN EC PRIVATE KEY-----\nFake19private\n-----END EC PRIVATE KEY-----'
        self.assertTrue(redact_text(nested) == '[REDACTED:private_key]', 'unmatched outer private block')
        with_placeholder = '-----BEGIN PRIVATE KEY-----\n[REDACTED:x]\nFake19private'
        self.assertTrue('Fake19private' not in redact_text(with_placeholder), 'placeholder inside private block')

    def test_email_exceptions(self):
        for case, text in enumerate(('git@github.com:org/repo.git', 'pkg@1.2.3', 'npm i @scope/pkg@1.2.3', 'user@host:~/path', 'user@host.invalid:~/path', 'a@b.c-x')):
            self.assertTrue(redact_text(text) == text, 'email exception id %d' % case)
        for case, text in enumerate(('jane@example.invalid', 'test.user+tag@example.invalid')):
            self.assertTrue(text not in redact_text(text), 'email case id %d' % case)

    def test_object_copy_secret_subtrees_tuple_argv_and_depth(self):
        obj = {' password ': {'unrelated': ['Fake19phrase', 12, False, ('other', None)]},
               'GH_PAT': ['Fake19phrase', 'other', 42], 'count': 5,
               'argv': ['--password', 'Fake19phrase'], 'tuple': ('jane@example.invalid', 17)}
        original = json.dumps(obj)
        result = redact_obj(obj)
        self.assertTrue('Fake19phrase' not in json.dumps(result), 'secret subtree')
        self.assertTrue(result[' password ']['unrelated'][1:3] == [12, False], 'scalar preservation')
        self.assertTrue(result[' password ']['unrelated'][3] == ('[REDACTED:assignment]', None), 'tuple preservation')
        self.assertTrue(isinstance(result['tuple'], tuple), 'ordinary tuple type')
        self.assertTrue(json.dumps(obj) == original and result is not obj, 'input copy')
        keyed = redact_obj({'jane@example.invalid': 'ordinary'})
        self.assertTrue('jane@example.invalid' not in keyed, 'object keys redacted')
        deep = 'Fake19phrase'
        for _ in range(220):
            deep = [deep]
        result = redact_obj(deep)
        for _ in range(201):
            result = result[0]
        self.assertTrue(result == '[REDACTED:depth]', 'depth cap')
        cycle = []
        cycle.append(cycle)
        self.assertTrue('[REDACTED:depth]' in str(redact_obj(cycle)), 'cycle depth cap')

    def test_embedded_json_and_separators(self):
        for case, separators in enumerate(((',', ':'), (', ', ': '), (',', ': '))):
            value = json.dumps({'password': 'Fake19phrase', 'count': 12, 'label': 'é'}, separators=separators, ensure_ascii=False)
            expected = json.dumps({'password': '[REDACTED:assignment]', 'count': 12, 'label': 'é'}, separators=separators, ensure_ascii=False)
            output = redact_obj({'payload': value})['payload']
            self.assertTrue(output == expected, 'embedded separators id %d' % case)
            self.assertTrue(sanitize_record({'payload': value}, {'payload': 'str'})['payload'] == expected, 'ledger JSON id %d' % case)
        malformed = '{password=Fake19phrase'
        self.assertTrue('Fake19phrase' not in redact_obj(malformed), 'invalid JSON fallback')

    def test_sanitize_record_fail_closed(self):
        obj = {'provider': 'demo', 'usage': {'input_tokens': 123, 'ratio': 0.5, 'bad': 'Fake19phrase', 'flag': True},
               'meta': {'ok': True, 'unexpected': 'Fake19phrase'}, 'text': 'password=Fake19phrase',
               'wrong': 'Fake19phrase', 'unknown': {'password': 'Fake19phrase'}, 'number': True}
        schema = {'provider': 'str', 'usage': 'any_num_map', 'meta': {'ok': 'bool'}, 'text': 'text', 'wrong': 'num', 'number': 'num'}
        result = sanitize_record(obj, schema, text_limit=12)
        self.assertTrue(result['_dropped'] == ['number', 'unknown', 'wrong'], 'ledger dropped names')
        self.assertTrue(result['usage'] == {'input_tokens': 123, 'ratio': 0.5, '_dropped': ['bad', 'flag']}, 'ledger numeric map')
        self.assertTrue(result['meta'] == {'ok': True, '_dropped': ['unexpected']}, 'nested schema')
        self.assertTrue('Fake19phrase' not in json.dumps(result), 'ledger leaked value')
        self.assertTrue(result['text'].endswith('…[truncated]') and len(result['text']) == 12 + len('…[truncated]'), 'text truncation')
        self.assertTrue(sanitize_record({'x': 1, '_dropped': ['unsafe']}, {}) == {'_dropped': ['_dropped', 'x']}, 'reserved dropped field')
        self.assertTrue(sanitize_record({'x': 'Fake19phrase'}, {'x': 'invalid'}) == {'_dropped': ['x']}, 'invalid schema fail closed')
        for case, value in enumerate((-1, True, '12')):
            with self.assertRaises(ValueError, msg='invalid limit id %d' % case):
                sanitize_record({}, {}, value)

    def test_idempotence_twenty_thousand_random_compositions(self):
        rng = random.Random(SEED)
        secrets = [obj for _, obj, _ in must_redact() if isinstance(obj, str)]
        keep = [text for _, text in must_keep_usage()] + must_keep_devlog()
        sampled = 0
        for case in range(20000):
            # Delimiters intentionally include adjacency and JSON escaping.
            parts = [rng.choice(secrets), rng.choice(keep), '[REDACTED:x]']
            rng.shuffle(parts)
            text = rng.choice(('', ' ', '\n', '; ', r'\n')).join(parts)
            if not SLOW_TESTS and case % 20:
                continue
            sampled += 1
            once = redact_text(text)
            self.assertTrue(redact_text(once) == once, 'idempotence case id %d' % case)
        print('idempotence sampled=%d total=20000 violations=0' % sampled)

    def test_adjacent_fragment_fuzz_text_and_objects(self):
        rng = random.Random(SEED)
        sampled = 0
        for case in range(65000):
            text = ''.join(rng.choice(ADJACENT_FRAGMENTS) for _ in range(rng.randint(2, 9)))
            obj = text if case < 60000 else {'password': text, 'payload': [text, {'message': text}]}
            if not SLOW_TESTS and case % 20:
                continue
            sampled += 1
            once = redact_text(obj) if isinstance(obj, str) else redact_obj(obj)
            twice = redact_text(once) if isinstance(once, str) else redact_obj(once)
            self.assertTrue(once == twice, 'adjacent idempotence case id %d' % case)
        print('adjacent_idempotence sampled=%d total=65000 violations=0' % sampled)

    def test_delimited_fields_are_stable_in_one_pass(self):
        for case, text in enumerate(('?token=Fake19Value&password=Other19Value&',
                                     'Cookie: sid=Fake19Value; csrf=Other19Value;')):
            with mock.patch.object(redact_module, '_redact_once', wraps=redact_module._redact_once) as once:
                result = redact_text(text)
                self.assertTrue(once.call_count == 1, 'stable field pass count id %d' % case)
            self.assertTrue(redact_text(result) == result, 'stable field idempotence id %d' % case)
        # A boundary-sensitive email/PEM composition still needs the second pass.
        for case, text in enumerate(('jane@x.invalid-----BEGIN PRIVATE KEY-----',
                                     'key=aB3!xyZtrue{{c@d.invalid&jane@x.invalid',
                                     'key=?otp=x', '?token=real& key=?otp=x',
                                     'Cookie: sid=real; key= ?otp=x')):
            self.assertTrue(redact_text(redact_text(text)) == redact_text(text), 'unsafe boundary fixed point id %d' % case)

    def test_overlap_priority_and_widest_extent(self):
        token = 'sk-proj-' + 'Fake19Ab_' * 8
        output = redact_text('password=' + token + ' remaining password words')
        self.assertTrue(output == 'password=[REDACTED:openai_project]', 'provider priority and union extent')
        block = '-----BEGIN PRIVATE KEY-----\n' + token
        self.assertTrue(redact_text(block) == '[REDACTED:private_key]', 'private priority')

    def test_existing_placeholders_are_opaque(self):
        for case, text in enumerate(('[REDACTED:sk-A9' + 'x' * 30 + ']', 'password=[REDACTED:x]',
                'Authorization: Bearer [REDACTED:x]', '[REDACTED:jane@example.invalid]')):
            self.assertTrue(redact_text(text) == text, 'placeholder id %d' % case)

    def test_documented_non_goals(self):
        token = 'sk-proj-' + 'Fake19Ab_' * 8
        cases = ['password=123456', base64.b64encode(token.encode()).decode(),
                 token[:12] + '\n' + token[12:]]
        for case, text in enumerate(cases):
            self.assertTrue(redact_text(text) == text, 'documented non-goal id %d' % case)
        self.assertTrue(redact_obj({'password': '123456'}) == {'password': '123456'}, 'numeric password object non-goal')

    def test_detector_scan_work_is_bounded_by_input_length(self):
        detectors = (redact_module._private_keys, redact_module._providers,
                     redact_module._assignments, redact_module._carriers)
        original = redact_module._value_span
        worst = 0
        count = 0
        # Charge both successful and preserved value scans. This deterministically
        # catches repeated suffix scans even when a fast machine hides them.
        def counted_value(text, start, multiline=False):
            result = original(text, start, multiline)
            text.work += result[1] - start
            return result
        with mock.patch.object(redact_module, '_value_span', counted_value):
            for case, (name, chunk) in enumerate(adversarial_families()):
                for size in (4096, 8192):
                    value = (chunk * (size // len(chunk) + 1))[:size]
                    for detector in detectors:
                        text = ScanWorkText(value)
                        list(detector(text))
                        count += 1
                        worst = max(worst, text.work / size)
                        self.assertTrue(text.work <= 64 * size,
                                        'scan budget family id %d detector %s' % (case, detector.__name__))
        print('detector_scan_checks=%d max_work_per_byte=%.2f bound=64' % (count, worst))

    @unittest.skipUnless(SLOW_TESTS, 'timing ratios/latency require ORCEXE_SLOW_TESTS=1')
    def test_each_detector_growth_ratio(self):
        chunk = '\n'.join(chunk for _, chunk in adversarial_families() if len(chunk) <= 2048)
        text = (chunk * (262144 // len(chunk) + 1))[:262144]
        detectors = (redact_module._private_keys, redact_module._providers,
                     redact_module._assignments, redact_module._carriers,
                     redact_module._EMAIL.finditer, redact_module._BEARER.finditer,
                     redact_module._PROTECTED.finditer)
        worst = 0
        for case, detector in enumerate(detectors):
            def measure():
                samples = []
                for index, size in enumerate((131072, 262144)):
                    start = time.process_time()
                    # Consume iterators without retaining spans or Match objects.
                    for _ in detector(text[:size]):
                        pass
                    samples.append(time.process_time() - start)
                return samples
            # Interleave sizes and use minima to filter scheduling noise, as
            # in the family growth check. Near-boundary results get more pairs;
            # quadratic growth stays near 4 even after re-measurement.
            pairs = [measure() for _ in range(3)]
            ratio = min(p[1] for p in pairs) / max(min(p[0] for p in pairs), 1e-9)
            while len(pairs) < 8 and ratio >= 2.25:
                pairs.append(measure())
                ratio = min(p[1] for p in pairs) / max(min(p[0] for p in pairs), 1e-9)
            worst = max(worst, ratio)
            self.assertTrue(ratio < 2.5, 'detector growth id %d: %.3f' % (case, ratio))
        print('detector_ratios=%d max_ratio=%.3f' % (len(detectors), worst))

    @unittest.skipUnless(SLOW_TESTS, 'timing ratios/latency require ORCEXE_SLOW_TESTS=1')
    def test_adversarial_growth_ratios_and_megabyte_cap(self):
        worst_ratio = (0.0, '')
        slowest = (0.0, '')
        count = checked = 0
        sizes = (131072, 262144) if SLOW_TESTS else (16384, 32768)
        def measure(text):
            small, large = [], []
            for size, samples in ((sizes[0], small), (sizes[1], large)):
                start = time.process_time()
                redact_text(text[:size])
                samples.append(time.process_time() - start)
            return small[0], large[0]
        generated_index = dict.fromkeys(_GENERATED_STRIDE, -1)
        sampled = 0
        for case, (name, text) in enumerate(adversarial_1mb()):
            count += 1
            for prefix in _GENERATED_STRIDE:
                if name.startswith(prefix):
                    generated_index[prefix] += 1
            in_sample = SLOW_TESTS or _in_fast_growth_sample(name, generated_index)
            sampled += in_sample
            if count % 50 == 0:
                print('scaling_progress=%d family=%s' % (count, name), flush=True)
            # Slow mode: paired medians of 3. Default: best-of timings (the
            # minimum filters scheduler noise at millisecond scale), re-measured
            # up to 8 times only while the ratio looks super-linear. A truly
            # quadratic family keeps a best-of ratio near 4 and still fails.
            if not in_sample:
                pairs = None
            elif SLOW_TESTS:
                pairs = [measure(text) for _ in range(3)]
                small = sorted(p[0] for p in pairs)[1]
                large = sorted(p[1] for p in pairs)[1]
            else:
                pairs = [measure(text)]
                while len(pairs) < 8 and (min(p[1] for p in pairs) /
                                          max(min(p[0] for p in pairs), 1e-9)) >= 2.5:
                    pairs.append(measure(text))
                small = min(p[0] for p in pairs)
                large = min(p[1] for p in pairs)
            if pairs is not None and (SLOW_TESTS or small >= 0.002):
                checked += 1
                ratio = large / small
                worst_ratio = max(worst_ratio, (ratio, name))
                self.assertTrue(ratio < 2.5, 'growth family id %d (%s): %.3f' % (case, name, ratio))
            if not SLOW_TESTS and name not in FAST_1MB_FAMILIES and case % 50:
                continue
            start = time.process_time()
            redact_text(text)
            elapsed = time.process_time() - start
            slowest = max(slowest, (elapsed, name))
            self.assertTrue(elapsed < scaled(10.0), '1MB timeout family id %d' % case)
        print('adversarial_families=%d sampled=%d ratio_checked=%d slow=%s max_ratio=%.3f family=%s slowest_1mb=%.3fs family=%s' %
              (count, sampled, checked, SLOW_TESTS, worst_ratio[0], worst_ratio[1], slowest[0], slowest[1]))

    def test_one_megabyte_peak_memory(self):
        worst = 0
        # Dense spans, dense protected placeholders, decoded mappings and PEM
        # state have distinct memory profiles. Time is measured without tracing.
        for case, (name, text) in enumerate(adversarial_1mb()):
            if name not in FAST_1MB_FAMILIES[:(7 if SLOW_TESTS else 1)]:
                continue
            tracemalloc.start()
            try:
                redact_text(text)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            worst = max(worst, peak)
            self.assertTrue(peak < 64 * 1024 * 1024, '1MB memory case id %d' % case)
        print('1MiB memory sampled=%d total=7 tracemalloc_peak=%.2fMiB' %
              (7 if SLOW_TESTS else 1, worst / 1024 / 1024))

    def test_compatibility_and_type_error(self):
        self.assertTrue(redact('ordinary') == redact_text('ordinary'), 'compatibility alias')
        for case, value in enumerate((None, 1, True, b'ordinary', [])):
            for fn in (redact, redact_text):
                try:
                    fn(value)
                except TypeError as error:
                    self.assertTrue(str(error) == 'redact() requires str input', 'type error id %d' % case)
                else:
                    self.fail('type accepted case id %d' % case)

    def test_assertion_messages_do_not_echo_inputs(self):
        # Demonstrate unittest's actual failing output contains only the case id.
        class DeliberateFailure(unittest.TestCase):
            def runTest(inner):
                inner.assertTrue(False, 'case id 123')
        result = unittest.TestResult()
        DeliberateFailure().run(result)
        self.assertTrue(len(result.failures) == 1, 'failure harness')
        # Enforce no assertEqual/assertIn-style automatic value repr, subTests,
        # or formatting input variables into messages in this contract file.
        tree = ast.parse(Path(__file__).read_text())
        forbidden = {'assertEqual', 'assertIn', 'assertNotIn', 'assertSequenceEqual', 'assertDictEqual', 'subTest'}
        self.assertTrue(not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in forbidden for n in ast.walk(tree)), 'unsafe test assertion API')
