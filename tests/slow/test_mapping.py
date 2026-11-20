"""Extended end-to-end matrices; enable with --slow or ORCEXE_SLOW_TESTS=1.

Fast examples and pure validators remain in tests.unit.test_mapping.
"""

import os
import unittest
from tests.unit.test_mapping import *

class SlowMappingTests(MappingTests):
    test_double_publication_fault_keeps_readable_old_snapshot = None
    test_publication_is_atomic_and_rolls_back_every_failure_point = None

    test_step_failures_have_distinct_content_free_reasons = None
    test_discovery_computation_failure_is_internal = None
    test_map_publishes_only_planning_valid_risk_globs = None
    test_strict_parser = None
    test_map_cap_diagnostics_name_first_location_without_content = None
    test_empty_evidence_items_are_dropped_by_cli = None
    test_evidence_resolution_and_grammar = None
    test_drop_count_hotspots_commands_and_render_caps = None
    test_manager_budgets_and_failure_reasons = None
    test_large_catalog_is_compact_counted_and_deduplicated = None
    test_output_prose_redaction = None
    test_default_route_prior_and_non_git = None
    test_manager_rejects_command_capable_tools_before_discovery = None
    test_publication_cleans_exact_prefixes_without_following_links = None
    test_discovery_write_errors_are_write_failed = None
    test_subdirectory_and_default_cwd_use_root_for_every_observer = None
    test_instruction_predicate_and_manager_copy_agree_at_every_depth = None
    test_unicode_word_and_byte_caps_count_drops_and_keep_later_claims = None
    test_hotspot_globs_support_segments_braces_and_reject_catchalls = None
    test_invalid_item_paths_are_dropped_and_counted = None
    test_every_prose_field_removes_terminal_controls_and_bidi = None
    test_short_and_common_identities_preserve_ordinary_words = None
    test_identity_matching_case_variants_whole_tokens_and_full_sequences = None
    test_protective_globs_allow_secret_and_excluded_paths_but_not_evidence = None
    test_glob_half_threshold_and_bounded_work = None
    test_bare_urls_are_code_spans = None
    test_head_movement_after_manager_does_not_publish = None
    test_precise_repository_gitlink_and_symlink_diagnostics = None
    test_directory_globs_survive_map_and_cover_descendant_hotspots = None
    test_map_from_linked_worktree_uses_main_top_level_and_state = None

    def test_one_fake_call_per_tool_copy_removed_and_private_outputs(self):
        (self.main / 'src/app.py').write_text('DIRTY_CONTENT')
        (self.main / 'untracked.md').write_text('UNTRACKED_CONTENT')
        for tool in ('claude-code',):
            self.calls.unlink(missing_ok=True)
            code, output, error = self.invoke(tool=tool)
            self.assertEqual((code, error), (0, ''), output)
            result = json.loads(output)
            self.assertEqual(result['intake_sha'], self.head)
            calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
            model_calls = [c for c in calls if c['model_call']]
            self.assertEqual(len(model_calls), 1)
            workdir = Path(model_calls[0]['cwd'])
            self.assertNotEqual(workdir, self.main)
            self.assertFalse(workdir.exists())
            run = self.state / 'runs' / result['manager']['run_id']
            self.assertEqual(json.loads((run / 'result.json').read_text())['profile'], 'manager')
            brief = (run / 'brief-source.md').read_text()
            self.assertNotIn(str(self.main), brief + json.dumps(model_calls))
            for private in ('SECRET_HEAD_CONTENT', 'STATE_CONTENT', 'DIRTY_CONTENT', 'UNTRACKED_CONTENT',
                            'fixture@example.invalid', 'Fixture'):
                self.assertNotIn(private, brief + output)
            self.assertIn('UNTRUSTED DATA', brief)
            self.assertIn('````json', brief)
            for name in ('project-map.json', 'project-map.md', 'history.md', 'direction.md'):
                path = self.main / '.orcexe' / name
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('file:src/app.py:1', (self.main / '.orcexe/history.md').read_text())


    def test_no_write_on_invalid_or_unavailable(self):
        state = self.main / '.orcexe'
        names = ('project-map.json', 'project-map.md', 'history.md', 'direction.md')
        for name in names:
            (state / name).write_text('keep ' + name)
        invalid = ['prose', 'preface ' + json.dumps(manager()),
                   json.dumps(dict(manager(), commands={'test': 'verified'}))]
        for claim in (dict(text='x', evidence=None), dict(text='x', evidence='file:x'),
                      dict(text='x', evidence=[1]), dict(text=1, evidence=[]),
                      dict(text='x'), dict(text='x', evidence=[], extra=True),
                      dict(text='x', evidence=['file:x'] * 101),
                      dict(text='x', evidence=['x' * 1025])):
            invalid.append(json.dumps(dict(manager(), history=[claim])))
        # The adapter's JSON-string redactor normalizes leading JSON whitespace;
        # exercise that raw-input bound directly and use opaque oversize prose
        # for the end-to-end final artifact bound.
        with self.assertRaises(ValueError):
            mapping.parse_manager(' ' * mapping.OUTPUT_CAP + json.dumps(manager()))
        invalid.append('x' * mapping.OUTPUT_CAP + json.dumps(manager()))
        for final in invalid:
            code, output, error = self.invoke(final)
            self.assertEqual(code, 2, repr(final[:120]) + ' bytes=' + str(len(final)))
            self.assertTrue(error.startswith('not_seen: manager_output_invalid\ndetail: '), error)
            self.assertEqual(len(error.splitlines()), 2)
            self.assertEqual(output, '')
            for name in names:
                self.assertEqual((state / name).read_text(), 'keep ' + name)
        with mock.patch.object(mapping, 'manager_run', side_effect=ValueError('manager_unavailable')):
            code, _, error = self.invoke()
        self.assertEqual((code, error.strip()), (2, 'not_seen: manager_unavailable'))
        with mock.patch.object(route, 'gather_access', return_value=[]):
            code, _, error = self.invoke(options=[])
        self.assertEqual((code, error.strip()), (2, 'not_seen: manager_unavailable: no_route'))
        for name in names:
            self.assertEqual((state / name).read_text(), 'keep ' + name)
        requests = []
        def timeout(req):
            requests.append(req)
            return run._minimal_result(req, 'openai', 'timeout', [])
        with mock.patch.object(mapping.ClaudeCodeAdapter, 'run', side_effect=timeout):
            code, _, error = self.invoke()
        self.assertEqual((code, error.strip()), (2, 'not_seen: manager_unavailable: timeout'))
        self.assertFalse(Path(requests[0].workdir).exists())
        for name in names:
            self.assertEqual((state / name).read_text(), 'keep ' + name)
        for name in names:
            (state / name).unlink()
        self.invoke('invalid')
        self.assertTrue(all(not (state / name).exists() for name in names))


    def test_brief_size_on_large_committed_repo(self):
        (self.main / 'src/exports.py').write_text(''.join(
            'def unused_{}():\n    return {}\n\n'.format(n, n) for n in range(2000)))
        for n in range(30):
            path = self.main / 'docs/adr' / ('{:03d}.md'.format(n))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Decision {}. '.format(n) + 'Detailed project direction. ' * 160)
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '-qm', 'large synthetic project')
        self.head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        u1, quality, docs, evidence = self.observations()
        self.assertGreaterEqual(quality['counts']['dead_code'], 2000)
        with mock.patch.dict(os.environ, self.env, clear=True):
            brief = mapping.build_brief(u1, quality, docs['u2'], docs['u3'], evidence, mapping.cleaner(evidence))
        self.assertLess(len(brief.encode('utf-8')), 30 * 1024)
        catalog = json.loads(brief.split('json\n', 1)[1].rsplit('\n', 2)[0])
        self.assertEqual(catalog['slop.dead_code']['count'], quality['counts']['dead_code'])
        self.assertEqual(len(catalog['slop.dead_code']['examples']), 3)






    def test_casefolded_customization_predicates_inventory_and_copy(self):
        table = [
            ('CLAUDE.md', True, False), ('claude.md', True, False),
            ('Claude.md', True, False), ('Agents.MD', True, False),
            ('AGENTſ.MD', True, False), ('CLAUDE.local.md', True, False),
            ('.Claude/settings.json', True, True), ('.CLAUDE/SETTINGS.LOCAL.JSON', True, True),
            ('.CuRſor/MCP.JSON', True, True), ('.CODEX/CONFIG.TOML', True, True),
            ('.Agents/agent.txt', True, False), ('.MCP.JSON', False, True),
            ('.VSCODE/MCP.JSON', False, True),
            ('.GitHub/Copilot-Instructions.MD', True, False),
            ('app.py', False, False), ('claude.md.bak', False, False)]
        instruction_paths, hook_paths = set(), set()
        for index, (name, instruction, security) in enumerate(table):
            for depth in ('', 'nested/', 'deep/e\u0301/'):
                path = '{}case{}/{}'.format(depth, index, name)
                self.assertEqual(intake._instruction(path), instruction, path)
                self.assertEqual(intake._security_config(path), security, path)
                target = self.main / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('[hooks]\nSessionStart = []' if name.endswith('TOML') else
                                  '{"hooks":{"SessionStart":[{}]}}' if security else 'PRIVATE_INSTRUCTION')
                if instruction:
                    instruction_paths.add(path)
                if security:
                    hook_paths.add(path)
        self.assertEqual(intake._name_parts('deep/e\u0301/AGENTſ.MD'), ('deep', 'é', 'agents.md'))
        self.assertTrue(intake._hook_path('nested/.HuSky/commit'))
        self.assertTrue(intake._hook_path('nested/.PRE-COMMIT-CONFIG.YAML'))
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '-qm', 'case variants')
        with mock.patch.dict(os.environ, self.env, clear=True):
            report = intake.scan(self.main)[1]
        # Git on macOS may normalize decomposed path spelling; compare by NFC.
        normalize = lambda values: {mapping.unicodedata.normalize('NFC', v) for v in values}
        self.assertTrue(normalize(instruction_paths) <= normalize(v['path'] for v in report['instructions']))
        self.assertTrue(normalize(hook_paths) <= normalize(v['path'] for v in report['security']['hooks']))

        def inspect(req):
            self.assertEqual(req.profile, 'manager')
            for p in Path(req.workdir).rglob('*'):
                relative = p.relative_to(req.workdir).as_posix()
                self.assertFalse(intake._instruction(relative) or intake._security_config(relative), relative)
            return self.completed(req)
        with mock.patch.object(mapping.ClaudeCodeAdapter, 'run', side_effect=inspect):
            code, _, error = self.invoke()
        self.assertEqual((code, error), (0, ''))


    def test_signals_remove_copy_and_stop_manager_process_group(self):
        if os.name == 'nt':
            self.skipTest('POSIX process groups and signals')
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            with self.subTest(signal=signum):
                marker = self.directory / ('signal-{}.json'.format(signum))
                wrapper = self.directory / 'fake-signal-claude'
                wrapper.write_text(
                    '#!' + sys.executable + '\nimport os,sys,signal,subprocess,time,json,runpy\n'
                    'from pathlib import Path\n'
                    'if "-p" in sys.argv:\n'
                    '    child = subprocess.Popen([sys.executable, "-u", "-c", '
                    '"import time; print(\\\"ready\\\", flush=True); time.sleep(3600)"], stdout=subprocess.PIPE)\n'
                    '    assert child.stdout.readline() == b"ready\\n"\n'
                    '    def reap(sig, frame):\n'
                    '        child.wait(timeout=15)\n'
                    '        sys.exit(0)\n'
                    '    signal.signal(signal.SIGTERM, reap)\n'
                    '    Path(' + repr(str(marker)) + ').write_text(json.dumps(dict('
                    'copy=os.getcwd(), pid=os.getpid(), child=child.pid, group=os.getpgrp())))\n'
                    '    os.kill(os.getppid(), ' + str(int(signum)) + ')\n'
                    '    time.sleep(3600)\n'
                    'sys.path.insert(0, ' + repr(str(FAKES)) + ')\n'
                    'runpy.run_path(' + repr(str(FAKES / 'claude')) + ', run_name="__main__")\n')
                wrapper.chmod(0o700)
                environment = dict(self.env, ORCEXE_BIN_CLAUDE_CODE=str(wrapper))
                source = ('import sys; sys.path.insert(0, ' + repr(str(ROOT / 'plugins/orcexe/src')) + '); '
                          'from orcexe import cli; sys.exit(cli.main(' + repr([
                              'map', '--repo', str(self.main), '--tool', 'claude-code',
                              '--model', 'claude-opus-5-5', '--json']) + '))')
                try:
                    result = subprocess.run([sys.executable, '-B', '-c', source], env=environment,
                                            cwd=str(self.main), capture_output=True, timeout=scaled(30))
                    self.assertEqual(result.returncode, 128 + signum, result.stderr.decode())
                    self.assertEqual(result.stdout, b'')
                    self.assertNotIn(b'Traceback', result.stderr)
                    observed = json.loads(marker.read_text())
                    self.assertFalse(Path(observed['copy']).exists())
                    for key in ('pid', 'child'):
                        with self.assertRaises(ProcessLookupError):
                            os.kill(observed[key], 0)
                finally:
                    if marker.exists():
                        try:
                            os.killpg(json.loads(marker.read_text())['group'], signal.SIGKILL)
                        except ProcessLookupError:
                            pass



def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in (SlowMappingTests,))
