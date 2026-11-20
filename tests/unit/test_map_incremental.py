"""Incremental, receipt and human-edit contracts, with fake managers only."""

import json
import os
from pathlib import Path
from unittest import mock

from tests.unit.test_mapping import MappingFixture, manager
from tests.unit.test_planning import task as plan_task
from orcexe import current_state, fsio, ledger, map_update, mapping, planning, published, questions


class IncrementalTests(MappingFixture):
    def map(self, value=None, inspect=None, full=False):
        self.final.write_text(json.dumps(manager() if value is None else value))
        calls = []

        def complete(req):
            calls.append(req)
            if inspect:
                inspect(req)
            return self.completed(req)

        with mock.patch.object(mapping.ClaudeCodeAdapter, 'run', side_effect=complete):
            code, out, err = self.invoke(options=['--tool', 'claude-code', '--model', 'fixture-model'] +
                                      (['--full'] if full else []))
        self.assertEqual(code, 0, err)
        return json.loads(out), err, calls

    def commit(self, paths):
        self.git_run(self.main, 'add', '--', *paths)
        self.git_run(self.main, 'commit', '-qm', 'update')
        return self.git_run(self.main, 'rev-parse', 'HEAD').strip()

    def test_incremental_copy_is_exact_changed_head_files_and_brief_is_fenced(self):
        previous, _, _ = self.map()
        (self.main / 'src/app.py').write_text('def run():\n    return 7\n')
        (self.main / 'src/new.py').write_text('print(7)\n')
        head = self.commit(['src/app.py', 'src/new.py'])
        (self.main / 'src/app.py').write_text('DIRTY FILE MUST NOT BE READ')
        (self.main / 'untracked.md').write_text('PRIVATE')

        def inspect(req):
            root = Path(req.workdir)
            files = {p.relative_to(root).as_posix() for p in root.rglob('*')
                     if p.is_file() and '.git' not in p.relative_to(root).parts}
            self.assertEqual(files, {'src/app.py', 'src/new.py'})
            self.assertEqual((root / 'src/app.py').read_text(), 'def run():\n    return 7\n')
            brief = Path(req.brief_path).read_text()
            self.assertIn('incremental map update', brief)
            self.assertIn('previous_map', brief)
            self.assertIn('changed_files', brief)
            self.assertIn(previous['intake_sha'], brief)
            self.assertIn('UNTRUSTED DATA', brief)

        value = manager()
        value['stack'] = []
        result, _, calls = self.map(value, inspect)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['intake_sha'], head)
        self.assertEqual(result['update'], dict(mode='incremental', base=previous['intake_sha'], changed=2))
        self.assertEqual(result['stack'], previous['stack'])

    def test_rename_delete_catalog_and_unresolved_previous_evidence(self):
        original = manager()
        original['conventions'] = [dict(rule='Roadmap rule', evidence=['file:ROADMAP.md'])]
        self.map(original)
        self.git_run(self.main, 'mv', 'src/app.py', 'src/renamed.py')
        self.git_run(self.main, 'rm', 'ROADMAP.md')
        self.git_run(self.main, 'commit', '-qm', 'rename and delete')

        def inspect(req):
            root = Path(req.workdir)
            files = {p.relative_to(root).as_posix() for p in root.rglob('*')
                     if p.is_file() and '.git' not in p.relative_to(root).parts}
            self.assertEqual(files, {'src/renamed.py'})
            brief = Path(req.brief_path).read_text()
            self.assertIn('"path": "ROADMAP.md"}', brief)
            self.assertIn('"status": "R", "path": "src/renamed.py"', brief)
            self.assertNotIn('file:ROADMAP.md', brief.split('"changed_files":', 1)[1])

        result, _, _ = self.map(manager('file:src/renamed.py:1'), inspect)
        self.assertEqual(result['update']['mode'], 'incremental')
        self.assertEqual(result['update']['changed'], 3)
        self.assertEqual(result['conventions'], [])
        self.assertGreater(result['dropped']['count'], 0)

    def test_no_change_keeps_map_bytes_and_never_routes_or_calls_manager(self):
        self.map()
        path = self.main / '.orcexe/project-map.json'
        before = path.read_bytes()
        with mock.patch.object(mapping.route, 'gather_access', side_effect=AssertionError('no probes')):
            result, _, calls = self.map()
        self.assertTrue(result['no_change'])
        self.assertEqual(calls, [])
        self.assertEqual(path.read_bytes(), before)
        # Model-free dirty metadata changes still trigger an update at same HEAD.
        (self.main / 'untracked.md').write_text('dirty')
        result, _, calls = self.map()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['update'], dict(mode='incremental', base=self.head, changed=0))

    def test_full_fallbacks_and_explicit_full_ignores_no_change(self):
        result, _, calls = self.map()
        self.assertEqual(result['update']['mode'], 'full')
        self.assertIsNone(result['update']['base'])
        result, _, calls = self.map(full=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['update']['mode'], 'full')
        path = self.main / '.orcexe/project-map.json'
        path.write_text('{broken')
        result, error, calls = self.map()
        self.assertEqual(result['update']['mode'], 'full')
        self.assertEqual(len(calls), 1)
        self.assertIn('human_edit_kept', error)
        backups = list(path.parent.glob('project-map.human-*.json'))
        self.assertEqual(backups[0].read_text(), '{broken')
        # A valid map from a future/nonancestor commit requires a full rebuild.
        (self.main / 'src/app.py').write_text('new\n')
        ahead = self.commit(['src/app.py'])
        self.map()
        self.git_run(self.main, 'reset', '--hard', self.head)
        result, _, calls = self.map()
        self.assertNotEqual(ahead, result['intake_sha'])
        self.assertEqual(result['update']['mode'], 'full')
        # The 40% bound counts changed paths, including deletions.
        for index in range(12):
            (self.main / ('added{}.txt'.format(index))).write_text('change\n')
        self.commit(['added{}.txt'.format(i) for i in range(12)])
        result, _, calls = self.map()
        self.assertEqual(result['update']['mode'], 'full')
        self.assertEqual(result['update']['changed'], 12)

    def test_human_delta_survives_full_updates_verbatim_and_raises_plan_lane(self):
        value = manager()
        value['conventions'] = [dict(rule='Keep spacing', evidence=['file:src/app.py'])]
        result, _, _ = self.map(value)
        human = dict(path_glob='src/**', reason='Human restriction', evidence=[])
        result['do_not_touch'].append(human)
        result['conventions'][0]['evidence'] = []
        result['conventions'][0]['note'] = 'Keep this field verbatim'
        unknown = dict(question='What about src/app.py?', why_it_matters='Human context', candidate_answers=[])
        result['unknowns'].append(unknown)
        path = self.main / '.orcexe/project-map.json'
        path.write_text(json.dumps(result))
        result, _, _ = self.map(full=True)
        self.assertEqual(result['do_not_touch'], [dict(human, source='human')])
        self.assertEqual(result['unknowns'], [dict(unknown, source='human')])
        self.assertEqual(result['conventions'][0]['note'], 'Keep this field verbatim')
        self.assertEqual(result['conventions'][0]['source'], 'human')
        lane, basis = planning.task_lane(plan_task(files={'allow': ['src/app.py']}), result, self.main)
        self.assertEqual(lane, 'risky')
        self.assertIn('cli:do_not_touch:src/**', basis)
        form = questions.select(result, result['intake_sha'])
        self.assertTrue(any('src/app.py' in q['text'] for q in form['questions']))
        result, _, _ = self.map(full=True)
        self.assertEqual(result['do_not_touch'], [dict(human, source='human')])

    def test_deleted_entries_stay_deleted_and_new_evidence_is_proposed_only(self):
        value = manager()
        claim = dict(path_glob='src/**', reason='Protect source', evidence=['file:src/app.py'])
        value['do_not_touch'] = [claim]
        result, _, _ = self.map(value)
        result['do_not_touch'] = []
        path = self.main / '.orcexe/project-map.json'
        path.write_text(json.dumps(result))
        result, _, _ = self.map(value, full=True)
        self.assertEqual(result['do_not_touch'], [])
        self.assertEqual(result['proposed'], [])
        value['do_not_touch'][0]['evidence'].append('file:package.json')
        result, _, _ = self.map(value, full=True)
        self.assertEqual(result['do_not_touch'], [])
        self.assertEqual(result['proposed'], [dict(section='do_not_touch', status='proposed', entry=value['do_not_touch'][0])])
        self.assertNotEqual(planning.task_lane(plan_task(files={'allow': ['src/app.py']}), result, self.main)[0], 'risky')

    def test_markdown_and_other_text_edits_get_sibling_proposals_even_without_changes(self):
        self.map()
        directory = self.main / '.orcexe'
        for name in ('project-map.md', 'history.md', 'direction.md', 'slop-report.md'):
            (directory / name).write_text('Human content for ' + name)
        result, error, calls = self.map()
        self.assertEqual(calls, [])
        self.assertIn('human_edit_kept', error)
        for name in ('project-map.md', 'history.md', 'direction.md', 'slop-report.md'):
            self.assertEqual((directory / name).read_text(), 'Human content for ' + name)
            proposal = directory / (Path(name).stem + '.proposed.md')
            self.assertTrue(proposal.is_file(), name)
            self.assertNotIn('Human content for ', proposal.read_text())
            self.assertIn(str(directory / name), error)
        receipt = published.read(directory)
        self.assertEqual(receipt['files']['project-map.json']['sha256'], published.digest((directory / 'project-map.json').read_text()))
        self.assertEqual((directory / published.RECEIPT).stat().st_mode & 0o777, 0o600)
        (self.main / 'src/app.py').write_text('def run():\n    return 9\n')
        self.commit(['src/app.py'])
        value = manager()
        value['architecture']['summary'] = 'Updated architecture.'
        self.map(value)
        proposal = directory / 'project-map.proposed.md'
        self.assertIn('Updated architecture.', proposal.read_text())
        self.map()
        self.assertIn('Updated architecture.', proposal.read_text())
        proposal.write_text('Human proposal annotation\n')
        self.map()
        self.assertEqual(proposal.read_text(), 'Human proposal annotation\n')
        self.assertIn('Updated architecture.', (directory / 'project-map.proposed.1.md').read_text())

    def test_current_state_preserves_human_block_and_reads_cli_ledger(self):
        self.map()
        directory = self.main / '.orcexe'
        path = directory / 'current-state.md'
        block = '<!-- orcexe:human -->\r\nMy exact notes.\r\n  ``` untouched ```\r\n<!-- /orcexe:human -->'
        path.write_bytes(published.HUMAN.sub(lambda match: block, path.read_text()).encode('utf-8'))
        plan = dict(plan_id='plan-fixture', head=self.head, status='approved', tasks=[plan_task(), plan_task('T2')])
        fsio.atomic_write_json(directory / 'plan.json', plan)
        log = ledger.Ledger(ledger.default_path(self.main))
        log.append('task.state', 'T1', dict(plan_id=plan['plan_id'], **{'from': 'running', 'to': 'accepted'}, reason='accepted'))
        self.git_run(self.main, 'update-ref', 'refs/orcexe/plans/plan-fixture', self.head)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(current_state.write(self.main), [])
        text = published.text(path)
        self.assertIn(block, text)
        for expected in (self.head[:12], 'full', 'Unknowns: 0', 'plan-fixture / approved',
                         'T1: accepted', 'T2: pending', 'Chain tip: ' + self.head, 'Land: none'):
            self.assertIn(expected, text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_bytes((text + '\nHuman edits outside block\n').encode('utf-8'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            kept = current_state.write(self.main)
        self.assertEqual(kept, [str(directory / 'current-state.previous.md')])
        self.assertNotIn('Human edits outside block', path.read_text())
        self.assertIn('Human edits outside block', published.text(directory / 'current-state.previous.md'))
        self.assertIn(block, published.text(path))
        self.assertFalse((directory / 'current-state.proposed.md').exists())

    def test_docs_parity(self):
        from tests.unit.test_gitutil import ROOT
        for name in ('README.md', 'plugins/orcexe/skills/orcexe/SKILL.md'):
            text = (ROOT / name).read_text()
            for term in ('incremental', '--full', '40%', 'human_edit_kept', 'current-state.md', 'source: human', 'intake_sha',
                         'current-state.previous.md', 'current_state_previous_saved'):
                self.assertIn(term, text, name)

    def test_map_regenerates_summary_with_added_second_block_and_reports_backup(self):
        self.map()
        directory = self.main / '.orcexe'
        path = directory / 'current-state.md'
        block = '<!-- orcexe:human -->\r\nUser-added second block\r\n<!-- /orcexe:human -->'
        path.write_bytes((published.text(path) + '\n' + block).encode())
        _, error, calls = self.map()
        self.assertEqual(calls, [])
        self.assertNotIn('human_edit_kept', error)
        self.assertEqual(len(published.HUMAN.findall(published.text(path))), 2)
        first = path.read_bytes()
        self.map()
        self.assertEqual(path.read_bytes(), first)
        old = published.text(path).replace('Unknowns: 0', 'Unknowns: human edit')
        path.write_bytes(old.encode())
        _, error, _ = self.map()
        self.assertIn('current_state_previous_saved: ' + str(directory / 'current-state.previous.md'), error)
        self.assertNotIn('human_edit_kept', error)
        self.assertEqual(published.text(directory / 'current-state.previous.md'), old)
        self.assertIn('Unknowns: 0', published.text(path))
        self.assertIn(block, published.text(path))
        self.assertEqual(published.text(path).count('# Current state'), 1)
        self.assertFalse((directory / 'current-state.proposed.md').exists())

    def test_delta_all_sections_tracks_text_identity_and_no_change_needs_no_model(self):
        value = manager()
        value['risk_map'] = [dict(path_glob='src/**', **{'class': 'data'}, reason='Old risk', evidence=['file:src/app.py'])]
        value['conventions'] = [dict(rule='Old rule', evidence=['file:src/app.py'])]
        value['unknowns'] = [dict(question='Old question?', why_it_matters='Context', candidate_answers=[])]
        result, _, _ = self.map(value)
        result['risk_map'][0]['reason'] = 'Human risk'
        result['conventions'] = [dict(rule='Human rule', evidence=[])]
        result['unknowns'] = []
        path = self.main / '.orcexe/project-map.json'
        path.write_text(json.dumps(result))
        result, _, calls = self.map()
        self.assertEqual(calls, [])
        human_risk = next(item for item in result['risk_map'] if item['path_glob'] == 'src/**')
        self.assertEqual(human_risk['reason'], 'Human risk')
        self.assertEqual(human_risk['source'], 'human')
        self.assertEqual(result['conventions'], [dict(rule='Human rule', evidence=[], source='human')])
        value['unknowns'][0]['evidence'] = ['file:package.json']
        result, _, _ = self.map(value, full=True)
        self.assertEqual(result['unknowns'], [])
        self.assertTrue(any(p['section'] == 'unknowns' for p in result['proposed']))
        self.assertNotIn('Old rule', [v['rule'] for v in result['conventions']])

    def test_share_bound_and_invalid_shapes_fall_back(self):
        result, _, _ = self.map()
        # Merged entries and CLI-added hotspots can exceed a single response's cap.
        result['conventions'] = [dict(rule='Rule {}'.format(i), evidence=['file:src/app.py']) for i in range(101)]
        path = self.main / '.orcexe/project-map.json'
        path.write_text(json.dumps(result))
        self.assertIsNotNone(map_update.load(path.parent))
        with mock.patch.object(map_update, 'changes', return_value=[{}, {}]):
            self.assertEqual(map_update.select(self.main, result, self.head, ['a'] * 5)[0]['mode'], 'incremental')
        with mock.patch.object(map_update, 'changes', return_value=[{}, {}, {}]):
            self.assertEqual(map_update.select(self.main, result, self.head, ['a'] * 5)[0]['mode'], 'full')
        result['risk_map'] = ['invalid entry']
        path.write_text(json.dumps(result))
        result, _, calls = self.map()
        self.assertEqual(result['update']['mode'], 'full')
        self.assertEqual(len(calls), 1)

    def test_invalid_utf8_map_is_preserved_before_full_rebuild(self):
        self.map()
        path = self.main / '.orcexe/project-map.json'
        path.write_bytes(b'Human invalid JSON: \xff\xfe')
        result, error, calls = self.map()
        self.assertEqual(result['update']['mode'], 'full')
        self.assertEqual(len(calls), 1)
        self.assertIn('human_edit_kept', error)
        self.assertEqual(next(path.parent.glob('project-map.human-*.json')).read_bytes(), b'Human invalid JSON: \xff\xfe')
        path.unlink()
        result, _, calls = self.map()
        self.assertEqual(result['update']['mode'], 'full')
        self.assertEqual(len(calls), 1)
