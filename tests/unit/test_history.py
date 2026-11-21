"""Fixed-date offline history fixtures; gh replays measured byte streams."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest import mock, skipUnless

from tests.unit.test_gitutil import GitFixture, ROOT
from tests.unit._timing import scaled
from tests.security import capture_children
from orcexe import cli, history, proc


class HistoryTests(GitFixture):
    def test_identity_whole_tokens_unicode_and_suffixes(self):
        for identity, longer in (('CI', 'decision'), ('dev', 'developer'),
                                 ('root', 'rooted'), ('bot', 'robot'),
                                 ('github-actions', 'github-actionsExtra'),
                                 ('a', 'data'), ('Kılıç', 'Kılıçlar'),
                                 ('İpek Şen', 'İpek Şener')):
            with self.subTest(identity=identity):
                text = longer + ' ' + identity + "'ın " + 'ö' + identity + ' ' + identity + 'ş'
                expected = longer + " [REDACTED:pii]'ın " + 'ö' + identity + ' ' + identity + 'ş'
                self.assertEqual(history._clean(text, [identity]), expected)
                structural = dict(path=identity + '/TODO.md', dir=identity,
                                  single_owner_dirs=[identity])
                self.assertEqual(history._clean(structural, [identity]), structural)
        self.assertEqual(history._clean('İpek-Şen and unknown@example.invalid', ['İpek Şen']),
                         '[REDACTED:pii] and [REDACTED:pii]')

    def test_identity_paths_preserved_and_prose_redacted(self):
        files = {'.github/workflows/ci.yml': 'workflow', 'src/decision.py': 'logic',
                 'CI/TODO.md': 'Add decision logic and CI ci@bot',
                 'ci@bot/TODO.md': 'CI'}
        for day in range(1, 4):
            content = {p: text + ('\n' if day == 2 else '') for p, text in files.items()}
            self.commit('2024-01-0' + str(day), content,
                        subject='Add decision logic and CI', author='CI', email='ci@bot')
        self.git_run(self.main, 'branch', '-M', 'main')
        self.git_run(self.main, 'checkout', '-qb', 'CI/decision')
        self.commit('2024-01-04', author='CI', email='ci@bot')
        self.git_run(self.main, 'checkout', 'main')
        report = self.scan()
        self.assertEqual({h['path'] for h in report['u2']['hotspots']}, set(files))
        dirs = {'.github/workflows', 'src', 'CI', 'ci@bot'}
        self.assertEqual(set(report['u2']['authors']['single_owner_dirs']), dirs)
        self.assertTrue(all(d['dir'] in dirs for p in report['u2']['phases'] for d in p['top_dirs']))
        docs = {d['path']: d['text'] for d in report['u3']['docs']}
        self.assertEqual(docs, {'CI/TODO.md': 'Add decision logic and [REDACTED:pii] [REDACTED:pii]',
                                'ci@bot/TODO.md': '[REDACTED:pii]'})
        self.assertEqual(report['u3']['recent_commits'][0]['subject'],
                         'Add decision logic and [REDACTED:pii]')
        self.assertEqual(report['u3']['unmerged_branches'][0]['name'], '[REDACTED:pii]/decision')

    def commit(self, date, files=None, subject='change', author='Private Person', email='person@example.invalid'):
        for path, content in (files or {}).items():
            target = self.main / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        self.git_run(self.main, 'add', '.')
        values = dict(GIT_AUTHOR_DATE=date + 'T12:00:00+00:00', GIT_COMMITTER_DATE=date + 'T12:00:00+00:00',
                      GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email,
                      GIT_COMMITTER_NAME='Private Committer', GIT_COMMITTER_EMAIL='committer@example.invalid')
        with mock.patch.dict(self.env, values):
            self.git_run(self.main, 'commit', '--allow-empty', '--quiet', '-m', subject)
        return self.git_run(self.main, 'rev-parse', 'HEAD').strip()

    def scan(self, issues=False, repo=None):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return history.scan(repo or self.main, issues)[1]

    def fake_gh(self, case='public', extra=''):
        fixtures = ROOT / 'tests/fixtures/streams/real-gh-2.97.0'
        codes = dict(line.split() for line in (fixtures / 'exit-codes.txt').read_text().splitlines())
        output = (fixtures / (case + '.stdout')).read_bytes()
        error = (fixtures / (case + '.stderr')).read_bytes()
        fake = self.directory / 'fake-gh'
        fake.write_text('#!' + sys.executable + '\nimport os,sys,time\n'
                        'assert sys.argv[1:] == ["issue","list","--state","open","--json","title,labels","--limit","50"]\n'
                        'assert os.getcwd() == ' + repr(str(self.main)) + '\n' + extra + '\n'
                        'os.write(1, ' + repr(output) + ')\nos.write(2, ' + repr(error) + ')\n'
                        'sys.exit(' + codes[case] + ')\n')
        fake.chmod(0o700)
        self.env['ORCEXE_BIN_GH'] = str(fake)
        return fake

    def remote(self, url='git@github.com:org/repo.git'):
        self.git_run(self.main, 'config', 'remote.origin.url', url)

    def test_empty_and_non_git_cli(self):
        report = self.scan()
        self.assertIsNone(report['u2']['head'])
        self.assertEqual(report['u2']['authors'], dict(count=0, single_owner_dirs=[]))
        self.assertFalse(report['u2']['phases'])
        self.assertFalse(report['u3']['docs'])
        for repo, expected in ((self.main, 0), (self.directory, 2)):
            out = io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out):
                code = cli.main(['history', '--repo', str(repo), '--json'])
            self.assertEqual(code, expected)
            json.loads(out.getvalue())

    def test_windows_iso_weeks_owners_and_exclusions(self):
        self.commit('2023-12-31', {'src/a': '1', 'root': '1'})
        self.commit('2024-01-01', {'src/a': '2', 'src/b': '1'})
        self.commit('2024-01-02', {'src/a': '3'})
        self.commit('2024-06-28', {'src/a': '4', 'vendor/a': 'x', '.env': 'x', '.orcexe/state': 'x'})
        u2 = self.scan()['u2']
        # Jan 1 is exactly 179 days before June 28; Dec 31 is 180.
        self.assertEqual([p['week'] for p in u2['phases']], ['2023-W52', '2024-W01', '2024-W26'])
        self.assertEqual(u2['phases'][1]['commits'], 2)
        self.assertEqual(u2['phases'][1]['top_dirs'], [dict(dir='src', changes=3)])
        self.assertEqual(u2['hotspots'], [dict(path='src/a', changes=1, fix_commits=0)])
        self.assertEqual(u2['authors'], dict(count=1, single_owner_dirs=['src']))
        self.commit('2024-06-29')
        self.assertNotIn('2023-W52', [p['week'] for p in self.scan()['u2']['phases']])

    def test_hotspot_order_fix_words_odd_paths_and_reverts(self):
        odd = ['space name', 'dir/é\nfile\tname', 'a' * 40, 'colon/:path', '-dash']
        first = self.commit('2024-01-01', {p: '1' for p in odd}, 'prefix fixes')
        self.commit('2024-01-02', {odd[0]: '2', odd[1]: '2'}, 'HOTFIX crash')
        self.commit('2024-01-03', {odd[0]: '3'}, 'fixture is not fixedly')
        revert = self.commit('2024-03-31', subject='Revert "Private Person change"\n\nThis reverts commit ' + first + '.')
        u2 = self.scan()['u2']
        self.assertEqual(u2['hotspots'][0], dict(path=odd[0], changes=3, fix_commits=2))
        self.assertEqual(u2['hotspots'][1]['changes'], 2)
        self.assertEqual(u2['reverts'][0]['reverted'], first)
        self.assertEqual(u2['reverts'][0]['sha'], revert)
        self.assertNotIn('Private Person', json.dumps(u2))
        self.commit('2024-04-01')  # Jan 1 is now outside 90 days.
        self.assertEqual(self.scan()['u2']['hotspots'][0]['changes'], 2)

    def test_docs_committed_stale_changelog_status_and_caps(self):
        self.commit('2023-01-01', {'ROADMAP.md': 'Private Person person@example.invalid\n' + 'z' * 5000,
                                 'docs/adr/1.md': '# Choice\n## Status\n\nAccepted\n',
                                 'architecture/decisions/1.md': 'Status: Proposed\n',
                                 'CHANGELOG.md': '# Changes\n## [Unreleased]\n### Added\nFuture\n## [1.0]\nOLD RELEASE\n',
                                 'nested/CHANGELOG.txt': '# History\n## 1.0\nNo pending\n',
                                 'vendor/TODO.md': 'excluded', '.envdir/TODO.md': 'secret',
                                 'TODO.md': 'next'})
        self.commit('2024-01-01', {'TODO.md': 'Private Committer other@else.invalid'})
        before = self.scan()
        (self.main / 'TODO.md').write_text('DIRTY')
        self.assertEqual(self.scan()['u3']['docs'], before['u3']['docs'])
        docs = {d['path']: d for d in before['u3']['docs']}
        self.assertEqual(len(docs), 5)
        self.assertEqual(docs['CHANGELOG.md']['text'], '## [Unreleased]\n### Added\nFuture\n')
        self.assertEqual(docs['docs/adr/1.md']['status'], 'Accepted')
        self.assertEqual(docs['architecture/decisions/1.md']['status'], 'Proposed')
        self.assertTrue(docs['ROADMAP.md']['stale'])
        self.assertTrue(docs['ROADMAP.md']['truncated'])
        self.assertEqual(len(docs['ROADMAP.md']['text']), 4000)
        self.assertFalse(docs['TODO.md']['stale'])
        raw = json.dumps(before)
        for value in ('Private Person', 'Private Committer', 'person@example.invalid', 'other@else.invalid', 'OLD RELEASE'):
            self.assertNotIn(value, raw)

    def test_secret_blob_alias_and_symlink_not_fetched(self):
        self.commit('2024-01-01', {'.env': 'DO_NOT_FETCH', 'ROADMAP.md': 'DO_NOT_FETCH'})
        if os.name != 'nt':
            (self.main / 'TODO.md').symlink_to('/never/read')
            self.git_run(self.main, 'add', '.')
            self.commit('2024-01-02')
        with mock.patch.object(history.intake, '_git', wraps=history.intake._git) as git:
            report = self.scan()
        self.assertFalse(report['u3']['docs'])
        self.assertFalse(any(call.args[1:3] == ('cat-file', '--batch') for call in git.call_args_list))
        self.assertIn('secret_blob', [item['reason'] for item in report['u3']['unseen']])

    def test_branches_default_local_remote_and_pii(self):
        self.commit('2024-01-01', {'a': '1'})
        self.git_run(self.main, 'branch', '-M', 'main')
        self.git_run(self.main, 'branch', 'merged')
        self.git_run(self.main, 'checkout', '-qb', 'Private-Person/feature')
        tip = self.commit('2024-02-01', subject='Private Committer update')
        self.git_run(self.main, 'update-ref', 'refs/remotes/origin/topic', tip)
        self.git_run(self.main, 'checkout', 'main')
        branches = self.scan()['u2']['unmerged_branches']
        self.assertEqual([b['name'] for b in branches], ['[REDACTED:pii]/feature', 'origin/topic'])
        self.assertTrue(all(b['ahead'] == 1 for b in branches))
        self.git_run(self.main, 'update-ref', 'refs/remotes/origin/trunk', tip)
        self.git_run(self.main, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        self.assertFalse(self.scan()['u2']['unmerged_branches'])

    def test_default_master_and_current_fallback(self):
        self.commit('2024-01-01')
        self.git_run(self.main, 'branch', '-M', 'master')
        self.git_run(self.main, 'checkout', '-qb', 'topic')
        self.commit('2024-01-02')
        self.assertEqual(self.scan()['u2']['unmerged_branches'][0]['name'], 'topic')
        self.git_run(self.main, 'branch', '-D', 'master')
        self.assertFalse(self.scan()['u2']['unmerged_branches'])

    def test_issue_not_requested_and_no_remote_never_start_gh(self):
        self.fake_gh()
        with mock.patch.object(history.proc, 'run', wraps=proc.run) as run:
            self.assertEqual(self.scan()['u3']['issues']['reason'], 'not_requested')
            self.assertEqual(self.scan(True)['u3']['issues']['reason'], 'no_github_remote')
        self.assertTrue(all(Path(call.args[0][0]).name == 'git' for call in run.call_args_list))
        for url in ('https://example.invalid/github.com/repo', 'git@notgithub.com:org/repo', 'https://github.com.evil/repo'):
            self.remote(url)
            self.assertEqual(self.scan(True)['u3']['issues']['reason'], 'no_github_remote')

    def test_recorded_gh_success_and_error_streams(self):
        self.commit('2024-01-01')
        for url in ('https://github.com/org/repo', 'ssh://git@github.com/org/repo', 'git@github.com:org/repo'):
            self.remote(url)
            self.fake_gh('public')
            result = self.scan(True)['u3']['issues']
            self.assertEqual(result['status'], 'seen', result)
            source = json.loads((ROOT / 'tests/fixtures/streams/real-gh-2.97.0/public.stdout').read_bytes())
            self.assertEqual(result['items'], [dict(title=item['title'], labels=[label['name'] for label in item['labels']]) for item in source])
        for case, reason in (('noauth', 'gh_not_authenticated'), ('nongh', 'gh_error'), ('noremote', 'gh_error')):
            self.fake_gh(case)
            self.assertEqual(self.scan(True)['u3']['issues']['reason'], reason)

    def test_issue_missing_malformed_timeout_and_pii(self):
        self.commit('2024-01-01')
        self.remote()
        self.env['ORCEXE_BIN_GH'] = str(self.directory / 'missing')
        self.assertEqual(self.scan(True)['u3']['issues']['reason'], 'gh_missing')
        for data in ('not json', '{}', '[{"title":"x","labels":["bug"]}]'):
            self.fake_gh(extra='os.write(1, ' + repr(data.encode()) + '); sys.exit(0)')
            self.assertEqual(self.scan(True)['u3']['issues']['reason'], 'gh_error')
        data = json.dumps([dict(title='Private Person Private Committer person@other.invalid ' + 'x' * 300,
                                labels=[dict(name='Private Person', description='DO_NOT_RETURN')])])
        self.fake_gh(extra='os.write(1, ' + repr(data.encode()) + '); sys.exit(0)')
        items = self.scan(True)['u3']['issues']['items']
        self.assertEqual(len(items[0]['title']), 200)
        self.assertNotIn('Private', json.dumps(items))
        self.assertNotIn('DO_NOT_RETURN', json.dumps(items))
        fake = self.fake_gh(extra='time.sleep(60)')
        def shortened_timeout(argv, **kwargs):
            if argv[0] == str(fake):
                self.assertEqual(kwargs['timeout_s'], 20)
                self.assertEqual(kwargs['stall_s'], 20)
                kwargs.update(timeout_s=.2, stall_s=.2)
            return proc_run(argv, **kwargs)
        proc_run = proc.run
        with mock.patch.object(history.proc, 'run', side_effect=shortened_timeout):
            self.assertEqual(self.scan(True)['u3']['issues']['reason'], 'timeout')

    def test_issue_slow_response_within_timeout(self):
        self.commit('2024-01-01')
        self.remote()
        self.fake_gh(extra='time.sleep(3.2)')
        self.assertEqual(self.scan(True)['u3']['issues']['status'], 'seen')

    def test_determinism_private_writes_main_worktree_and_no_models(self):
        self.commit('2024-01-01', {'TODO.md': 'Private Person Private Committer'})
        linked = self.directory / 'linked'
        self.git_run(self.main, 'worktree', 'add', '-qb', 'topic', str(linked))
        self.env.update(ORCEXE_BIN_CODEX='/never/codex', ORCEXE_BIN_CLAUDE_CODE='/never/claude', ORCEXE_BIN_CURSOR='/never/agent')
        with mock.patch.dict(os.environ, self.env, clear=True), capture_children() as children:
            first = history.run_history(linked)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(['history', '--repo', str(linked), '--json']), 0)
            second = json.loads(out.getvalue())
        self.assertTrue(all(Path(child['argv'][0]).name == 'git' for child in children))
        for name in ('u2', 'u3'):
            target = self.main / '.orcexe/intake' / (name + '.json')
            self.assertEqual(json.loads(target.read_text()), second[name])
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            first[name].pop('meta')
            second[name].pop('meta')
        self.assertEqual(first, second)
        self.assertFalse((linked / '.orcexe').exists())
        self.assertNotIn('Private', out.getvalue())

    def test_shallow(self):
        self.commit('2024-01-01', {'a': '1'})
        self.commit('2024-02-01', {'a': '2'})
        clone = self.directory / 'shallow'
        self.git_run(self.directory, 'clone', '--quiet', '--depth=1', self.main.as_uri(), str(clone))
        report = self.scan(repo=clone)
        self.assertTrue(report['u2']['shallow'])
        self.assertTrue(report['u2']['unseen'])
        self.assertEqual(len(report['u3']['recent_commits']), 1)

    def test_merge_exclusion_multiple_owners_recent_limit_and_structural_redaction(self):
        self.commit('2024-01-01', {'src/a': '1'}, author='a')
        self.git_run(self.main, 'branch', '-M', 'main')
        self.git_run(self.main, 'checkout', '-qb', 'side')
        self.commit('2024-01-02', {'src/b': '1'}, author='Other Owner')
        self.git_run(self.main, 'checkout', 'main')
        self.commit('2024-01-03', {'src/a': '2'}, author='a')
        with mock.patch.dict(self.env, dict(GIT_AUTHOR_DATE='2024-01-03T18:00:00+00:00',
                                            GIT_COMMITTER_DATE='2024-01-03T18:00:00+00:00')):
            self.git_run(self.main, 'merge', '--no-ff', '--quiet', 'side', '-m', 'merge')
        # Give HEAD a fixed date after the merge, which itself is excluded.
        for day in range(4, 27):
            self.commit('2024-01-' + str(day), subject='entry ' + str(day), author='a')
        report = self.scan()
        self.assertEqual(report['u2']['authors']['count'], 2)
        self.assertEqual(report['u2']['authors']['single_owner_dirs'], [])
        self.assertEqual(sum(p['commits'] for p in report['u2']['phases']), 26)
        self.assertEqual(len(report['u3']['recent_commits']), 20)
        self.assertEqual(report['u3']['recent_commits'][0]['date'], '2024-01-26')
        self.assertEqual(report['u2']['schema'], 'orcexe.intake.u2.v1')
        self.assertRegex(report['u2']['head'], r'^[0-9a-f]{40}$')
        self.assertEqual(report['u3']['issues']['status'], 'not_seen')
        self.assertFalse(any(c['subject'] == 'merge' for c in report['u3']['recent_commits']))

    def test_stale_boundary_top_dir_and_hotspot_ties(self):
        self.commit('2024-01-01', {'ROADMAP.md': 'plan', 'z/a': '1', 'b/a': '1'})
        self.commit('2024-03-01', {'z/a': '2', 'c/a': '1'}, subject='fixed defect')
        self.commit('2024-03-02', {'b/a': '2', 'd/a': '1'}, subject='feature')
        self.commit('2024-06-29')  # Jan 1 is exactly 180 days ago.
        report = self.scan()
        self.assertFalse(report['u3']['docs'][0]['stale'])
        self.assertEqual(report['u2']['phases'][0]['top_dirs'],
                         [dict(dir='.', changes=1), dict(dir='b', changes=1), dict(dir='z', changes=1)])
        self.commit('2024-06-30')
        self.assertTrue(self.scan()['u3']['docs'][0]['stale'])

    def test_repository_child_cpu_and_python39_import(self):
        if os.name == 'posix':
            import resource
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = subprocess.run([sys.executable, '-c', 'from orcexe.history import scan; from pathlib import Path; import json; print(json.dumps(scan(Path.cwd())[1]))'],
                                cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / 'plugins/orcexe/src')),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertEqual(json.loads(result.stdout)['u2']['schema'], 'orcexe.intake.u2.v1')
        if os.name == 'posix':
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.assertLess(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 10)

    @skipUnless(os.environ.get('ORCEXE_SLOW_TESTS') == '1', 'opt-in wall clock budget')
    def test_wall_budget(self):
        start = time.monotonic()
        history.scan(ROOT)
        self.assertLess(time.monotonic() - start, scaled(10))
