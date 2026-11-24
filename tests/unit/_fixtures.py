"""Private Git templates and binary paths shared by test classes only."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tests.unit._timing import scaled


_GIT = None


def non_repository_root(directory):
    """A gitfile boundary prevents discovering the enclosing source checkout.

    Fixtures can live inside the repository, including worker TMPDIRs, while
    their home/non-repository children still behave as outside worktrees.
    """
    directory = Path(directory)
    (directory / '.git').write_text('gitdir: ' + str(directory / 'not-a-repository') + '\n')


def git_environment(environment, directory):
    """Bypass Apple's xcrun shim, still exercising the installed real Git."""
    global _GIT
    git = shutil.which('git', path=environment['PATH'])
    if not git:
        return None
    # Newer Git (ubuntu-latest) starts detached auto-maintenance after commits; it
    # races fixture copies (vanishing objects/maintenance.lock). Tests never need it.
    if 'GIT_CONFIG_COUNT' not in environment:
        pairs = (('maintenance.auto', 'false'), ('gc.auto', '0'))
        environment['GIT_CONFIG_COUNT'] = str(len(pairs))
        for i, (key, value) in enumerate(pairs):
            environment['GIT_CONFIG_KEY_%d' % i] = key
            environment['GIT_CONFIG_VALUE_%d' % i] = value
    if sys.platform == 'darwin':
        if _GIT is None:
            _GIT = subprocess.run(['xcrun', '--find', 'git'], env=environment,
                                  check=True, capture_output=True, text=True,
                                  timeout=scaled(30)).stdout.strip()
        git = _GIT
        binaries = Path(directory) / 'bin'
        binaries.mkdir(exist_ok=True)
        link = binaries / 'git'
        if not link.exists():
            link.symlink_to(git)
        environment['PATH'] = str(binaries) + os.pathsep + environment['PATH']
    return git


class GitTemplates:
    """Build once per class; copies have independent objects, refs and index."""
    def __init__(self, cleanup):
        self.temporary = tempfile.TemporaryDirectory(prefix='orcexe-git-template-')
        cleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {}

    def copy(self, name, destination, build):
        if name not in self.paths:
            template = self.root / name
            template.mkdir()
            build(template)
            self.paths[name] = template
        # Transient lock files (e.g. a background maintenance run) are not repository state.
        shutil.copytree(self.paths[name], destination, ignore=shutil.ignore_patterns('*.lock'))
