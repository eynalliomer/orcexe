"""U5 deterministic selection and CLI persistence contracts, entirely offline."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from unittest import mock

from tests.unit.test_gitutil import GitFixture, ROOT
from orcexe import cli, fsio, questions


def project(head='head'):
    return dict(schema='orcexe.project_map.v1', intake_sha=head, unknowns=[],
                risk_map=[], conventions=[], do_not_touch=[], direction_sentences={'conflicts': []})


def unknown(text, answers=None, **kwargs):
    return dict(question=text, candidate_answers=answers or ['First', 'Second'], **kwargs)


class SelectionTests(GitFixture):
    def test_risk_class_scores(self):
        for risk_class, irreversible in (('auth', 2), ('payment', 3), ('data', 3), ('migration', 3)):
            value = project()
            value['risk_map'] = [dict(path_glob='area/*', **{'class': risk_class})]
            question = questions.select(value, 'head')['questions'][0]
            self.assertEqual(question['score'], dict(impact=2, irreversibility=irreversible, total=4 + irreversible))

    def test_each_kind_scoring_order_and_cap(self):
        value = project()
        value['unknowns'] = [unknown('Question ' + str(i)) for i in range(5)]
        value['risk_map'] = [dict(path_glob='data/*', **{'class': 'data'}),
                             dict(path_glob='auth/*', **{'class': 'auth'}),
                             dict(path_glob='infra/*', **{'class': 'infra'})]
        value['do_not_touch'] = [dict(path_glob='protected/*', **{'class': 'infra'})]
        value['direction_sentences']['conflicts'] = [dict(text='Conflicting direction')]
        result = questions.select(value, 'head')
        self.assertEqual(len(result['questions']), 4)
        scores = [q['score']['total'] for q in result['questions']]
        self.assertEqual(scores, [7, 6, 6, 4])
        all_candidates = result['questions'] + result['assumptions']
        self.assertEqual(len(all_candidates), 9)
        self.assertTrue({'risk_confirm', 'conflict'} <= {q['kind'] for q in result['questions']})
        self.assertEqual([q['id'] for q in result['questions'][1:3]], sorted(q['id'] for q in result['questions'][1:3]))
        scoped = questions.select(value, 'head', 'Improve data auth protected')
        self.assertEqual({q['kind'] for q in scoped['questions']}, {'conflict', 'goal_scope'})
        confirmations = [a for a in scoped['assumptions'] if a['text'].startswith('Confirm ')]
        self.assertEqual([a['reason'] for a in confirmations], ['deferred_to_plan'] * 3)
        only = project()
        only['do_not_touch'] = value['do_not_touch']
        q = questions.select(only, 'head')['questions'][0]
        self.assertEqual(q['kind'], 'do_not_touch_confirm')
        self.assertEqual(q['score'], dict(impact=1, irreversibility=2, total=4))
        only = project()
        only['unknowns'] = [unknown('Unknown')]
        self.assertEqual(questions.select(only, 'head')['questions'][0]['score'], dict(impact=2, irreversibility=0, total=4))

    def test_assumptions_evidence_decided_low_impact_over_limit(self):
        value = project()
        value['unknowns'] = [unknown('From option', ['unsupported', dict(label='Supported', evidence=['file:a'])]),
                             unknown('From convention', ['Use tests']), unknown('From risk', ['Keep data']),
                             unknown('Decided')] + [unknown('Unresolved ' + str(i)) for i in range(5)]
        value['conventions'] = [dict(rule='Use tests', evidence=['file:a'])]
        value['risk_map'] = [dict(path_glob='a', **{'class': 'other'}, reason='Keep data', evidence=['file:a'])]
        value['do_not_touch'] = [dict(path_glob='a')]
        decisions = [dict(schema='orcexe.decision.v1', question_id=questions.question_id('Decided'), answer='User answer')]
        result = questions.select(value, 'head', decisions=decisions, resolve=lambda e: e == 'file:a')
        assumptions = {a['text']: a for a in result['assumptions']}
        self.assertEqual(assumptions['From option']['assumed'], 'Supported')
        self.assertEqual(assumptions['From convention']['reason'], 'evidence')
        self.assertEqual(assumptions['From risk']['reason'], 'evidence')
        self.assertEqual(assumptions['Decided']['assumed'], 'User answer')
        self.assertEqual({a['reason'] for a in result['assumptions']}, {'evidence', 'decided', 'low_impact', 'over_limit'})
        self.assertNotIn(questions.question_id('Decided'), [q['id'] for q in result['questions']])
        # Unresolvable option evidence does not suppress a question or recommend an option.
        invalid = project()
        invalid['unknowns'] = [unknown('Missing evidence', [dict(label='Maybe', evidence=['file:no'])])]
        q = questions.select(invalid, 'head')['questions'][0]
        self.assertFalse(q['options'][0]['recommended'])
        self.assertTrue(q['free_text'])

    def test_ids_determinism_and_goal_heuristic(self):
        self.assertEqual(questions.question_id('  HELLO\nworld '), questions.question_id('hello world'))
        value = project()
        value['unknowns'] = [unknown('Stable')]
        first = questions.select(value, 'head')
        self.assertEqual(first, questions.select(copy.deepcopy(value), 'head'))
        self.assertEqual(first['questions'][0]['id'], questions.select(value, 'newhead')['questions'][0]['id'])
        for goal in ('Do x so that y', 'Until ready', 'When complete', 'must work', 'should work', 'tests pass', '3 items'):
            self.assertEqual(questions.select(project(), 'head', goal)['questions'], [])
        self.assertEqual(questions.select(project(), 'head', 'Improve whenever possible')['questions'][0]['kind'], 'goal_scope')

    def test_goal_relevance_direct_map_paths_conflicts_and_scope(self):
        value = project()
        value['architecture'] = dict(modules=[dict(path='plugins/tool/cli.py', role='Version command'),
                                               dict(path='plugins/tool/auth.py', role='Authentication'),
                                               dict(path='.github/workflows/ci.yml', role='Windows CI')])
        value['unknowns'] = [unknown('Which version JSON format?'), unknown('Should cli.py use a helper?'),
                             unknown('Windows CI?'), unknown('Cursor slug rule?'),
                             unknown('Helper strategy?', why_it_matters='Changes plugins/tool/cli.py')]
        value['direction_sentences']['conflicts'] = [dict(text='Unrelated conflict')]
        value['risk_map'] = [dict(path_glob='plugins/tool/cli.py', **{'class': 'data'}),
                             dict(path_glob='plugins/tool/auth.py', **{'class': 'auth'})]
        form = questions.select(value, 'head', 'Add version JSON in cli.py')
        asked = {q['kind'] for q in form['questions']}
        self.assertEqual(asked, {'conflict', 'goal_scope', 'unknown'})
        assumptions = {a['text']: a['reason'] for a in form['assumptions']}
        for text in ('Windows CI?', 'Cursor slug rule?', 'Which version JSON format?'):
            self.assertEqual(assumptions[text], 'not_relevant')
        for path, risk_class in (('cli', 'data'), ('auth', 'auth')):
            self.assertEqual(assumptions['Confirm this area is high-risk: plugins/tool/' + path + '.py (' + risk_class + ').'],
                             'deferred_to_plan')
        all_reasons = assumptions.copy()
        all_reasons.update({q['text']: 'asked' for q in form['questions']})
        for text in ('Should cli.py use a helper?', 'Helper strategy?'):
            self.assertIn(all_reasons[text], ('asked', 'over_limit'))
        # With an explicit completion criterion there is no scope question.
        direct = questions.select(value, 'head', 'Change plugins/tool/auth.py so that tests pass')
        self.assertFalse(any(q['kind'] == 'risk_confirm' for q in direct['questions']))
        self.assertFalse(any(q['kind'] == 'goal_scope' for q in direct['questions']))

    def test_version_json_goal_does_not_confirm_unrelated_risk_areas(self):
        fixture = ROOT / 'tests/fixtures/planning'
        value = json.loads((fixture / 'T40c-project-map.json').read_text())
        exact_goal = json.loads((fixture / 'T40c-real-plan-final.md').read_text())['tasks'][0]['goal']
        tracked = ['plugins/orcexe/src/orcexe/' + name for name in
                   ('cli.py', 'redact.py', 'worktree.py', 'gitutil.py', 'trust.py', 'envfilter.py', 'adapters/base.py')]
        for goal in (exact_goal, 'Add a --version-json flag to orcexe version …'):
            form = questions.select(value, 'head', goal, tracked_paths=tracked)
            self.assertFalse(any(q['kind'] == 'risk_confirm' for q in form['questions']))
            self.assertEqual([a['reason'] for a in form['assumptions'] if a['text'].startswith('Confirm this area is high-risk:')],
                             ['deferred_to_plan'] * len(value['risk_map']))
        # Basename tokens are counted too, strictly above half; short words
        # cannot connect otherwise unrelated areas through descriptions.
        value = project()
        value['unknowns'] = [unknown('Name.py choice?'), unknown('Go on?')]
        form = questions.select(value, 'head', 'Name go must work',
                                tracked_paths=['a/name.py', 'b/name.py', 'c/other.py'])
        self.assertFalse(form['questions'])
        boundary = questions.select(value, 'head', 'Name must work',
                                    tracked_paths=['a/name.py', 'b/other.py'])
        self.assertEqual([q['text'] for q in boundary['questions']], ['Name.py choice?'])

    def test_unknown_relevance_requires_a_named_path_or_module(self):
        value = project()
        value['architecture'] = dict(modules=[dict(path='src/cli.py', role='JSON version command'),
                                               dict(path='src/ledger.py', role='SHA-256-chained JSON tests')])
        value['unknowns'] = [unknown('Which JSON version format?'), unknown('Add JSON tests?'),
                             unknown('Should ledger.py add JSON tests?'), unknown('Which cli helper?'),
                             unknown('Which src/cli.py helper?')]
        form = questions.select(value, 'head', 'Add JSON to cli so that tests pass',
                                tracked_paths=['src/cli.py', 'src/ledger.py', 'README.md'])
        self.assertEqual({q['text'] for q in form['questions']}, {'Which cli helper?', 'Which src/cli.py helper?'})
        self.assertEqual({a['reason'] for a in form['assumptions']}, {'not_relevant'})
        # An explicit path can identify a future file absent from the map.
        value['unknowns'] = [unknown('How should future/new.py work?')]
        self.assertEqual(len(questions.select(value, 'head', 'Update future/new.py so that tests pass')['questions']), 1)

    def test_question_budget_across_forms_is_scoped_to_goal_hash(self):
        value = project()
        value['unknowns'] = [unknown('Version.py choice ' + str(n)) for n in range(10)]
        goal = 'Version so that tests pass'
        decisions = []
        for n in range(4):
            form = questions.select(value, 'head', goal, decisions)
            self.assertEqual(len(form['questions']), 4 - n)
            question = form['questions'][0]
            decisions.append(dict(schema='orcexe.decision.v1', question_id=question['id'],
                                  answer='Chosen', kind='text', goal_hash=form['goal_hash']))
        # Duplicate answers, approvals and answers for other goals do not spend the budget.
        extras = [dict(decisions[0]), dict(decisions[0], goal_hash='other'),
                  dict(decisions[0], kind='plan_approval', question_id='plan:x')]
        form = questions.select(value, 'newhead', goal, decisions + extras)
        self.assertEqual(form['questions'], [])
        self.assertEqual([a['reason'] for a in form['assumptions']].count('decided'), 4)
        self.assertEqual([a['reason'] for a in form['assumptions']].count('over_limit'), 6)
        self.assertEqual(form['goal_hash'], questions.goal_hash('  VERSION so that tests PASS '))
        self.assertEqual(len(questions.select(value, 'head', 'Version must work', decisions)['questions']), 4)
        # Answers are global by question ID, including legacy unscoped answers.
        self.assertEqual(len(questions.select(value, 'head', goal,
            [dict(d, goal_hash=None) for d in decisions])['questions']), 4)
        self.assertEqual([a['reason'] for a in questions.select(value, 'head', goal,
            [dict(d, goal_hash=None) for d in decisions])['assumptions']].count('decided'), 4)


    def test_decisions_are_global_but_budgets_are_normalized_per_goal(self):
        value = project()
        value['unknowns'] = [unknown('README.md format ' + str(n)) for n in range(12)]
        value['architecture'] = {'modules': [{'path': 'README.md'}, {'path': 'src/other.py'}]}
        first = questions.select(value, 'head', 'README.md must work')
        decisions = [dict(schema='orcexe.decision.v1', question_id=q['id'], answer='Keep',
                          kind='text', goal_hash=first['goal_hash']) for q in first['questions']]
        same = questions.select(value, 'head', '  readme.md MUST work.  ', decisions)
        self.assertEqual(same['goal_hash'], first['goal_hash'])
        self.assertEqual(same['questions'], [])
        self.assertEqual([a['reason'] for a in same['assumptions']].count('decided'), 4)
        other = questions.select(value, 'head', 'README.md should work', decisions)
        self.assertEqual(len(other['questions']), 4)
        self.assertTrue({q['id'] for q in first['questions']}.isdisjoint(q['id'] for q in other['questions']))
        self.assertEqual([a['reason'] for a in other['assumptions']].count('decided'), 4)
        unscoped = questions.select(value, 'head', decisions=decisions)
        self.assertEqual(len(unscoped['questions']), 4)
        self.assertTrue({q['id'] for q in first['questions']}.isdisjoint(q['id'] for q in unscoped['questions']))


class QuestionsCLITests(GitFixture):
    def setUp(self):
        super().setUp()
        (self.main / 'README.md').write_text('Example project\n')
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '-qm', 'seed')
        self.head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.directory_state = self.main / '.orcexe'
        self.value = project(self.head)
        self.value['unknowns'] = [unknown('Which approach?'), unknown('Which scope?')]
        self.write_map()

    def write_map(self):
        fsio.atomic_write_json(self.directory_state / 'project-map.json', self.value)

    def invoke(self, command, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main([command, '--repo', str(self.main), '--json'] + list(args))
        return code, out.getvalue(), err.getvalue()

    def form(self):
        code, out, err = self.invoke('questions')
        self.assertEqual((code, err), (0, ''))
        return json.loads(out)

    def test_step_failures_have_distinct_content_free_reasons(self):
        before = (self.directory_state / 'project-map.json').read_bytes()
        with mock.patch.object(questions, 'select', side_effect=ValueError('PRIVATE_CONTENT')):
            self.assertEqual(self.invoke('questions'),
                             (70, '', 'not_seen: internal_error\ndetail: select_questions ValueError\n'))
        with mock.patch.object(questions.fsio, 'atomic_write_json', side_effect=OSError('PRIVATE_CONTENT')):
            self.assertEqual(self.invoke('questions'), (2, '', 'not_seen: write_failed\n'))
        self.assertEqual((self.directory_state / 'project-map.json').read_bytes(), before)
        self.assertFalse((self.directory_state / 'form.json').exists())
        (self.directory_state / 'project-map.json').write_text('not JSON')
        self.assertEqual(self.invoke('questions'), (2, '', 'not_seen: questions_invalid\n'))

    def test_questions_command_appends_ledger_form_record(self):
        from orcexe import ledger
        form = self.form()
        path = ledger.default_path(self.main)
        records = list(ledger.Ledger(path).records(event='questions.form'))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['task'], 'questions')
        self.assertEqual(records[0]['data']['count'], len(form['questions']))
        self.assertEqual(records[0]['data']['form_sha256'], form['form_id'])
        self.form()
        self.assertEqual(len(list(ledger.Ledger(path).records(event='questions.form'))), 2)

    def test_round_trip_choices_text_and_decided(self):
        form = self.form()
        self.assertEqual((self.directory_state / 'form.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(form, self.form())
        a, b = [q['id'] for q in form['questions']]
        code, out, err = self.invoke('answer', '--form', form['form_id'], '--choice', a + '=1', '--text', b + '=Custom=answer')
        self.assertEqual((code, err), (0, ''))
        records = fsio.read_jsonl(self.directory_state / 'decisions.jsonl')
        self.assertEqual([r['answer'] for r in records], ['Second', 'Custom=answer'])
        self.assertEqual([r['kind'] for r in records], ['choice', 'text'])
        for r in records:
            self.assertEqual(r['head'], self.head)
            self.assertEqual(r['schema'], 'orcexe.decision.v1')
            self.assertIn('at', r)
        next_form = self.form()
        self.assertEqual(next_form['questions'], [])
        self.assertEqual({a['reason'] for a in next_form['assumptions']}, {'decided'})

    def test_choice_accepts_label_and_matches_the_index_record(self):
        form = self.form()
        a, b = [q['id'] for q in form['questions']]
        labels = [o['label'] for o in form['questions'][0]['options']]
        ledger = self.directory_state / 'decisions.jsonl'
        self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--choice', a + '=' + labels[1])[0], 0)
        by_label = fsio.read_jsonl(ledger)[0]
        ledger.unlink()
        self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--choice', a + '=1')[0], 0)
        by_index = fsio.read_jsonl(ledger)[0]
        for record in (by_label, by_index):
            record.pop('at')
        self.assertEqual(by_label, by_index)
        self.assertEqual((by_label['answer'], by_label['kind']), (labels[1], 'choice'))

    def test_numeric_looking_labels_prefer_the_index_when_in_range(self):
        self.value['unknowns'] = [unknown('Which approach?', ['1', '0', '7'])]
        self.write_map()
        form = self.form()
        qid = form['questions'][0]['id']
        labels = [o['label'] for o in form['questions'][0]['options']]
        self.assertEqual(sorted(labels), ['0', '1', '7'])
        ledger = self.directory_state / 'decisions.jsonl'
        # In-range digits are indexes even when they equal another option's label;
        # '7' is out of range as an index, so it resolves as the label.
        for value, expected in (('0', labels[0]), ('1', labels[1]), ('7', '7')):
            if ledger.exists():
                ledger.unlink()
            code, out, err = self.invoke('answer', '--form', form['form_id'], '--choice', qid + '=' + value)
            self.assertEqual((code, err), (0, ''))
            self.assertEqual(fsio.read_jsonl(ledger)[0]['answer'], expected)
        self.assertTrue(any(label != str(i) for i, label in enumerate(labels)))
        ledger.unlink()
        self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--choice', qid + '=3')[0], 2)
        self.assertFalse(ledger.exists())

    def test_invalid_choice_lists_valid_values_after_the_reason(self):
        form = self.form()
        qid = form['questions'][0]['id']
        labels = [o['label'] for o in form['questions'][0]['options']]
        ledger = self.directory_state / 'decisions.jsonl'
        for value in ('Nope', 'first', '9', '-1', ''):
            code, out, err = self.invoke('answer', '--form', form['form_id'], '--choice', qid + '=' + value)
            self.assertEqual((code, out), (2, ''))
            lines = err.splitlines()
            self.assertEqual(lines[0], 'not_seen: answer_invalid')
            self.assertEqual(lines[1:], ['valid choices for {}:'.format(qid)] +
                             ['  {}: {}'.format(i, label) for i, label in enumerate(labels)])
            self.assertFalse(ledger.exists())
        for arguments in (['--form', form['form_id'], '--choice', 'q-bad=0'],
                          ['--form', form['form_id'], '--text', qid + '='],
                          ['--form', form['form_id'], '--choice', qid + '=0', '--text', qid + '=again']):
            self.assertEqual(self.invoke('answer', *arguments), (2, '', 'not_seen: answer_invalid\n'))
        self.assertFalse(ledger.exists())

    def test_invalid_batches_append_nothing(self):
        form = self.form()
        qid = form['questions'][0]['id']
        valid = ['--form', form['form_id'], '--text', qid + '=valid']
        ledger = self.directory_state / 'decisions.jsonl'
        for existing in (False, True):
            if existing:
                self.assertEqual(self.invoke('answer', *valid)[0], 0)
            original = ledger.read_bytes() if ledger.exists() else None
            for arguments in (['--form', 'bad', '--text', qid + '=x'],
                              valid + ['--choice', 'q-bad=0'],
                              ['--form', form['form_id'], '--choice', qid + '=2'],
                              ['--form', form['form_id'], '--choice', qid + '=-1'],
                              ['--form', form['form_id'], '--choice', qid + '=no'],
                              ['--form', form['form_id']],
                              ['--form', form['form_id'], '--text', qid + '='],
                              valid + ['--text', qid + '=duplicate']):
                self.assertEqual(self.invoke('answer', *arguments)[0], 2)
                self.assertEqual(ledger.read_bytes() if ledger.exists() else None, original)
        self.git_run(self.main, 'commit', '--allow-empty', '-qm', 'new head')
        self.assertEqual(self.invoke('answer', *valid)[0], 2)
        self.assertEqual(ledger.read_bytes(), original)

    def test_missing_and_stale_map_and_missing_form(self):
        self.assertEqual(self.invoke('answer', '--form', 'missing', '--text', 'q-no=x')[0], 2)
        self.value['intake_sha'] = 'old'
        self.write_map()
        code, out, err = self.invoke('questions')
        self.assertEqual((code, out, err), (2, '', 'not_seen: map_stale\n'))
        (self.directory_state / 'project-map.json').unlink()
        self.assertEqual(self.invoke('questions'), (2, '', 'not_seen: map_missing\n'))
        self.assertFalse((self.directory_state / 'form.json').exists())

    def test_redaction_caps_and_structured_conflict(self):
        self.value['unknowns'] = [unknown('Send to person@example.com?', ['token=supersecretvalue']), unknown('Long ' + 'x' * 5000)]
        self.value['direction_sentences']['conflicts'] = [dict(text='Conflicting plans', evidence=['file:README.md:1'])]
        self.write_map()
        form = self.form()
        self.assertIn('conflict', [q['kind'] for q in form['questions']])
        self.assertNotIn('person@example.com', json.dumps(form))
        self.assertNotIn('supersecretvalue', json.dumps(form))
        self.assertTrue(all(len(q['text']) <= 4000 for q in form['questions']))

    def test_head_evidence_resolves_and_reorders_options(self):
        self.value['unknowns'] = [unknown('Evidence choice', ['No evidence',
            dict(label='Use README', evidence=['file:README.md:1']),
            dict(label='Missing file', evidence=['file:missing.md:1'])])]
        self.write_map()
        form = self.form()
        self.assertEqual(form['questions'], [])
        self.assertEqual(form['assumptions'][0]['reason'], 'evidence')
        self.assertEqual(form['assumptions'][0]['assumed'], 'Use README')
        self.assertEqual(form['assumptions'][0]['evidence'], ['file:README.md:1'])

    def test_invisible_answers_form_tamper_and_repeat_ids_append_nothing(self):
        form = self.form()
        qid = form['questions'][0]['id']
        ledger = self.directory_state / 'decisions.jsonl'
        for text in ('\u200b', 'ㅤ', '\u2060\u200b'):
            self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--text', qid + '=' + text)[0], 2)
            self.assertFalse(ledger.exists())
        changed = copy.deepcopy(form)
        changed['questions'][0]['text'] = 'Modified question'
        fsio.atomic_write_json(self.directory_state / 'form.json', changed)
        self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--text', qid + '=x'),
                         (2, '', 'not_seen: form_tampered\n'))
        self.assertFalse(ledger.exists())
        fsio.atomic_write_json(self.directory_state / 'form.json', form)
        self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--text', qid + '=Chosen')[0], 0)
        before = ledger.read_bytes()
        self.assertEqual(self.invoke('answer', '--form', form['form_id'], '--text', qid + '=Again')[0], 2)
        self.assertEqual(ledger.read_bytes(), before)
        self.value['unknowns'][0]['question'] = form['questions'][0]['text']
        next_form = self.form()
        self.assertNotIn(qid, [q['id'] for q in next_form['questions']])
