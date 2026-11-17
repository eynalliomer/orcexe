#!/usr/bin/env python3
"""Run unittest discovery in isolated module subprocesses (Python 3.9+)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / 'tests/.durations.json'


class Discovery(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.modules = set()

    def loadTestsFromModule(self, module, *args, **kwargs):
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        if suite.countTestCases():
            self.modules.add(module.__name__)
        return suite


def worker(module, result_path, top):
    sys.path.insert(0, top)
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    record = dict(module=module, tests=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors),
                  skips=len(result.skipped), duration=time.monotonic() - started,
                  successful=result.wasSuccessful(),
                  tracebacks=[(test.id(), trace) for test, trace in
                              result.failures + result.errors])
    Path(result_path).write_text(json.dumps(record), encoding='utf-8')
    return int(not result.wasSuccessful())


def run_module(module, top, timeout, directory):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='module-', dir=directory) as temporary:
        base = Path(temporary)
        home, tmp = base / 'home', base / 'tmp'
        home.mkdir()
        tmp.mkdir()
        environment = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                           TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp),
                           CODEX_HOME=str(home / '.codex'),
                           XDG_CACHE_HOME=str(home / '.cache'),
                           XDG_CONFIG_HOME=str(home / '.config'),
                           PYTHONDONTWRITEBYTECODE='1')
        result_path = base / 'result.json'
        issue = None
        with (base / 'output.txt').open('w+', encoding='utf-8') as output:
            process = subprocess.Popen(
                [sys.executable, '-B', str(Path(__file__).resolve()),
                 '--worker', module, '--result', str(result_path), '-t', top],
                cwd=top, env=environment, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT,
                **({'start_new_session': True} if os.name != 'nt' else {}))
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                issue = 'timed out after {:.2f}s'.format(timeout)
            finally:
                # Also clean up descendants left by a crashing worker.
                if os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                process.wait()
            output.seek(0)
            captured = output.read()
        try:
            record = json.loads(result_path.read_text(encoding='utf-8'))
            if process.returncode != (0 if record['successful'] else 1):
                issue = issue or 'worker crashed (exit {})'.format(process.returncode)
        except (OSError, ValueError, KeyError):
            record = dict(module=module, tests=0, failures=0, errors=0, skips=0,
                          successful=False, tracebacks=[])
            issue = issue or 'worker crashed (exit {})'.format(process.returncode)
        record.update(duration=time.monotonic() - started, output=captured, issue=issue)
        return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 1)
    parser.add_argument('-s', '--start-directory', default='.')
    parser.add_argument('-p', '--pattern', default='test*.py')
    parser.add_argument('-t', '--top-level-directory', default='.')
    parser.add_argument('--timeout', type=float, default=300,
                        help='per-module seconds, multiplied by ORCEXE_TEST_TIME_SCALE')
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--result', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    top = str(Path(args.top_level_directory).resolve())
    if args.worker:
        return worker(args.worker, args.result, top)
    try:
        scale = float(os.environ.get('ORCEXE_TEST_TIME_SCALE', '1'))
    except ValueError:
        parser.error('ORCEXE_TEST_TIME_SCALE must be finite and > 0')
    if args.jobs <= 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--jobs and --timeout must be > 0 (timeout finite)')
    if not math.isfinite(scale) or scale <= 0:
        parser.error('ORCEXE_TEST_TIME_SCALE must be finite and > 0')
    # Discovery imports tests too: prevent bytecode writes in the parent.
    sys.dont_write_bytecode = True
    started = time.monotonic()
    loader = Discovery()
    loader.discover(args.start_directory, pattern=args.pattern, top_level_dir=top)
    if loader.errors:
        print('\n'.join(loader.errors), file=sys.stderr)
        return 1
    try:
        previous = json.loads(CACHE.read_text(encoding='utf-8'))
        previous = {name: value for name, value in previous.items()
                    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0}
    except (OSError, ValueError, AttributeError):
        previous = {}
    modules = sorted(loader.modules, key=lambda name: (-previous.get(name, 0), name))
    with tempfile.TemporaryDirectory(prefix='.runner-', dir=str(ROOT / 'tests')) as directory:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run_module, name, top, args.timeout * scale, directory)
                       for name in modules]
            records = []
            for name, future in zip(modules, futures):
                try:
                    records.append(future.result())
                except Exception as error:
                    records.append(dict(module=name, tests=0, failures=0, errors=1,
                                        skips=0, duration=0, successful=False,
                                        issue=repr(error), tracebacks=[], output=''))
    records.sort(key=lambda item: item['module'])
    print('Module results (name order):')
    for record in records:
        print('{module}: tests={tests} failures={failures} errors={errors} skips={skips} '
              'duration={duration:.2f}s{issue}'.format(
                  **dict(record, issue='; ' + record['issue'] if record['issue'] else '')))
        if record['output']:
            print(record['output'].rstrip())
        for test, trace in record['tracebacks']:
            # unittest output already contains the traceback; retain structured
            # results above and print it here only if the stream was lost.
            if trace not in record['output']:
                print('{}\n{}'.format(test, trace))
    print('10 slowest modules:')
    for record in sorted(records, key=lambda item: (-item['duration'], item['module']))[:10]:
        print('  {duration:.2f}s {module}'.format(**record))
    failed = sum(not r['successful'] or bool(r['issue']) for r in records)
    print('Ran {} tests in {} modules in {:.2f}s; failures={} errors={} skips={}; '
          'failed modules={}'.format(
              sum(r['tests'] for r in records), len(records), time.monotonic() - started,
              sum(r['failures'] for r in records), sum(r['errors'] for r in records),
              sum(r['skips'] for r in records), failed))
    # Atomic replacement also tolerates two independent developer runs.
    with tempfile.NamedTemporaryFile(mode='w', dir=str(CACHE.parent),
                                     prefix='.durations-', delete=False) as cache:
        json.dump({r['module']: r['duration'] for r in records if not r['issue']}, cache,
                  indent=2, sort_keys=True)
        cache.write('\n')
    os.replace(cache.name, CACHE)
    return int(bool(failed))


if __name__ == '__main__':
    sys.exit(main())
