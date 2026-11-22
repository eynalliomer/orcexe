"""U5 deterministic selection and validated answer batches (no manager calls).

Completion heuristic: a whole-word so that/until/when/must/should/tests pass,
 or any digit, counts as an explicit completion criterion. It is deliberately
 small: this is a prompt for scope, not a natural-language intent classifier.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

from orcexe import globs, fsio, gitutil, history, intake, ledger, mapping, slop

IMPACT = dict(conflict=3, goal_scope=3, unknown=2, risk_confirm=2, do_not_touch_confirm=1)
IRREVERSIBILITY = dict(data=3, payment=3, migration=3, auth=2, infra=2, other=1, public_api=1)
COMPLETION = re.compile(r'\b(?:so\s+that|until|when|must|should|tests\s+pass)\b|\d', re.I)
QUESTION_CAP = 4
# Ignore connective words and repository scaffolding, not domain vocabulary.
STOP_WORDS = set(('a an and are as at be by can do does for from how i if in is it of on or '
                  'so that the their this to use we what when which with should must until '
                  'add change fix improve implement make print prints pass tests test flag '
                  'src lib plugins internal py md json jsonl yaml yml').split())


def normalise(text):
    return ' '.join(globs.path_key(intake.prose(' '.join(text.split()))).split())


def question_id(text):
    return 'q-' + hashlib.sha256(normalise(text).encode('utf-8')).hexdigest()[:12]


def goal_hash(goal):
    if goal is None:
        return None
    value = normalise(goal)
    while value and (value[-1].isspace() or unicodedata.category(value[-1]).startswith('P')):
        value = value[:-1]
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def relevance(project, goal, tracked_paths=None):
    """Match unknowns through named paths/modules, never through map prose."""
    def tokens(text):
        return {t for t in re.findall(r'[^\W_]+', text.lower()) if len(t) > 2} - STOP_WORDS

    def strings(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return []

    def paths(item):
        result = [item['path']] if isinstance(item.get('path'), str) else []
        if isinstance(item.get('path_glob'), str):
            try:
                result.extend(globs.expand(item['path_glob']))
            except (ValueError, RecursionError):
                # Legacy unusable risk globs are handled conservatively by planning.
                pass
        for value in strings(item.get('evidence', [])):
            if value.startswith(('file:', 'doc:', 'hotspot:')):
                result.append(re.sub(r':[0-9]+$', '', value.split(':', 1)[1]))
        return result

    rows = [item for value in project.values() if isinstance(value, list)
            for item in value if isinstance(item, dict)]
    rows += project.get('architecture', {}).get('modules', [])
    all_paths = {path for item in rows for path in paths(item)}
    if tracked_paths is not None:
        all_paths.update(tracked_paths)
    # Count each token once per tracked path, including basenames. A project or
    # package name cannot establish relevance merely by occurring everywhere.
    census = [tokens(p) for p in (set(tracked_paths) if tracked_paths is not None else all_paths)]
    counts = Counter(token for path in census for token in path)
    common = {token for token, count in counts.items() if count * 2 > len(census)}
    terms = tokens(goal) - common
    module_terms = tokens(' '.join(all_paths)) - common

    def mentions(text):
        # Explicit paths also cover files not yet present in the map or HEAD.
        found = re.findall(r'[\w.*?{}\[\]!,+-]+(?:/[\w.*?{}\[\]!,+-]+)*', text)
        return {p for value in found for p in [value.rstrip('.,?!')]
                if ('/' in p or '.' in p) and globs.valid(p)}

    goal_paths = mentions(goal)

    def relevant(kind, text, item):
        if kind != 'unknown':
            return True
        text += ' ' + item.get('why_it_matters', '')
        candidate_paths = mentions(text)
        for candidate in candidate_paths:
            for path in goal_paths:
                try:
                    if globs.overlaps(candidate, path):
                        return True
                    if '/' not in candidate and globs.overlaps(candidate, path.rsplit('/', 1)[-1]):
                        return True
                except (ValueError, RecursionError):
                    pass
        candidate = (tokens(' '.join(candidate_paths)) - common) | (tokens(text) & module_terms)
        return bool(candidate & terms)
    return relevant


def select(project, head, goal=None, decisions=(), resolve=lambda evidence: False,
           clean=lambda text: intake.prose(history._clean(text, []))[:mapping.TEXT_CAP],
           tracked_paths=None):
    """Pure selector; evidence resolution and redaction are injectable for proof."""
    candidates = {}
    relevant = relevance(project, goal, tracked_paths) if goal is not None else lambda *args: True

    def evidence(item):
        return [e for e in item.get('evidence', []) if isinstance(e, str) and resolve(e)]

    def add(kind, text, item, options=()):
        identifier = question_id(text)
        text = clean(text)
        prepared = []
        for option in options:
            option = dict(label=option) if isinstance(option, str) else option
            prepared.append(dict(label=clean(option['label']), recommended=False, evidence=evidence(option)))
        # Evidence-backed options lead, preserving source order within each group.
        prepared.sort(key=lambda option: not bool(option['evidence']))
        if prepared and prepared[0]['evidence']:
            prepared[0]['recommended'] = True
        impact = IMPACT[kind]
        irreversible = IRREVERSIBILITY.get(item.get('class', item.get('risk_class')), 0)
        candidates.setdefault(identifier, dict(id=identifier, kind=kind, text=text, options=prepared,
                                               free_text=True, score=dict(impact=impact,
                                               irreversibility=irreversible, total=2 * impact + irreversible)))
        candidates[identifier]['relevant'] = relevant(kind, text, item)

    for item in project.get('unknowns', []):
        add('unknown', item['question'], item, item.get('candidate_answers', []))
    direction = project.get('direction_sentences', {})
    if isinstance(project.get('direction'), dict):
        direction = project['direction']
    for item in direction.get('conflicts', []):
        add('conflict', item['text'], item, item.get('candidate_answers', []))
    for item in project.get('risk_map', []):
        if item.get('class') in ('auth', 'payment', 'data', 'migration'):
            add('risk_confirm', 'Confirm this area is high-risk: ' + item['path_glob'] + ' (' + item['class'] + ').',
                item, ['Confirm high-risk', 'Do not confirm'])
    for item in project.get('do_not_touch', []):
        add('do_not_touch_confirm', 'Confirm do not touch: ' + item['path_glob'] + '.', item,
            ['Confirm do not touch', 'Do not confirm'])
    if goal is not None and not COMPLETION.search(goal):
        add('goal_scope', 'What is the completion criterion for: ' + goal + '?', {})

    digest = goal_hash(goal)
    decided = {d['question_id']: d['answer'] for d in decisions
               if d.get('schema') == 'orcexe.decision.v1' and d.get('kind') != 'plan_approval'}
    spent = {d['question_id'] for d in decisions if d.get('schema') == 'orcexe.decision.v1'
             and d.get('kind') != 'plan_approval' and d.get('goal_hash') == digest}
    remaining = max(0, QUESTION_CAP - len(spent)) if goal is not None else QUESTION_CAP
    facts = [(clean(item.get('rule', item.get('reason', ''))), evidence(item))
             for section in ('conventions', 'risk_map') for item in project.get(section, [])]
    assumptions, pending = [], []
    for candidate in candidates.values():
        is_relevant = candidate.pop('relevant')
        identifier = candidate['id']
        recommended = next((o for o in candidate['options'] if o['recommended']), None)
        backed = next(((o['label'], ids) for o in candidate['options'] for text, ids in facts
                       if candidate['kind'] == 'unknown' and ids and o['label'] == text), None)
        assumed, ids = (candidate['options'][0]['label'] if candidate['options'] else 'Unresolved'), []
        if identifier in decided:
            reason, assumed = 'decided', clean(decided[identifier])
        elif goal is not None and candidate['kind'] in ('risk_confirm', 'do_not_touch_confirm'):
            reason, assumed = 'deferred_to_plan', 'Deferred to plan approval'
        elif not is_relevant:
            reason = 'not_relevant'
        elif recommended or backed:
            reason = 'evidence'
            assumed, ids = (recommended['label'], recommended['evidence']) if recommended else backed
        elif candidate['score']['impact'] <= 1 and candidate['score']['irreversibility'] == 0:
            reason = 'low_impact'
        else:
            pending.append(candidate)
            continue
        assumptions.append(dict(id=identifier, text=candidate['text'], assumed=assumed, reason=reason, evidence=ids))
    pending.sort(key=lambda q: (-q['score']['total'], q['id']))
    for candidate in pending[remaining:]:
        assumptions.append(dict(id=candidate['id'], text=candidate['text'],
                                assumed=candidate['options'][0]['label'] if candidate['options'] else 'Unresolved',
                                reason='over_limit', evidence=[]))
    result = dict(schema='orcexe.form.v1', head=head, goal=clean(goal) if goal is not None else None,
                  goal_hash=digest, questions=pending[:remaining], assumptions=sorted(assumptions, key=lambda a: a['id']))
    result['form_id'] = form_hash(result)
    return result


def form_hash(form):
    content = {k: v for k, v in form.items() if k != 'form_id'}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def context(args):
    return mapping.repository_context(args.repo or Path.cwd())


def read_decisions(directory):
    path = directory / 'decisions.jsonl'
    if path.is_symlink():
        raise ValueError('output_symlink')
    try:
        records = fsio.read_jsonl(path) if path.exists() else []
        if any(not isinstance(d, dict) or d.get('schema') != 'orcexe.decision.v1'
               or not isinstance(d.get('question_id'), str) or not isinstance(d.get('answer'), str)
               for d in records):
            raise ValueError('state_corrupt')
        return records
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('state_corrupt')


def questions_command(args):
    failure, step = 'internal_error', 'context'
    try:
        repo, directory, head = context(args)
        failure, step = 'questions_invalid', 'read_map'
        try:
            project = fsio.read_json(directory / 'project-map.json')
        except FileNotFoundError:
            raise ValueError('map_missing')
        if project.get('intake_sha') != head:
            raise ValueError('map_stale')
        if project.get('schema') != 'orcexe.project_map.v1':
            raise ValueError('map_invalid')
        failure, step = 'internal_error', 'observe'
        # Reuse HEAD-only evidence rules without refreshing or writing U1-U4.
        u1 = intake.scan(repo)[1]
        quality = slop.scan(repo)[1]
        docs = history.scan(repo)[1]
        resolver = mapping.Evidence(repo, head, u1, quality, docs['u2'], docs['u3'])
        step = 'select_questions'
        result = select(project, head, args.goal, read_decisions(directory), resolver.resolve, mapping.cleaner(resolver),
                        tracked_paths=resolver.entries)
        if gitutil.head(repo) != head:
            raise ValueError('map_stale')
        failure, step = 'write_failed', 'publish_questions'
        fsio.atomic_write_json(directory / 'form.json', result, mode=0o600)
        try:
            ledger.Ledger(ledger.default_path(repo)).append(
                'questions.form', 'questions',
                dict(count=len(result['questions']), form_sha256=result['form_id']))
        except (OSError, ValueError, ledger.LedgerError):
            pass
    except Exception as error:
        return mapping.report_failure(error, failure, step,
                                      ('map_missing', 'map_stale', 'output_symlink', 'orcexe_invalid', 'not_a_repository',
                                       'state_corrupt'))
    print(json.dumps(result, ensure_ascii=False) if args.json else render(result))
    return 0


def render(form):
    lines = ['Form ' + form['form_id']]
    for question in form['questions']:
        lines.append(question['id'] + ': ' + question['text'])
        lines.extend('  {}: {}{}'.format(i, option['label'], ' (recommended)' if option['recommended'] else '')
                     for i, option in enumerate(question['options']))
    lines.extend('Assumed: ' + a['text'] + ' = ' + a['assumed'] + ' (' + a['reason'] + ')'
                 for a in form['assumptions'])
    return '\n'.join(lines)


def answer_command(args):
    hint = None
    try:
        repo, directory, head = context(args)
        form = fsio.read_json(directory / 'form.json')
        if isinstance(form, dict) and form_hash(form) != form.get('form_id'):
            raise ValueError('form_tampered')
        if form['schema'] != 'orcexe.form.v1' or form['form_id'] != args.form or form['head'] != head:
            raise ValueError('form_invalid')
        questions = {q['id']: q for q in form['questions']}
        decided = {d['question_id'] for d in read_decisions(directory) if d.get('kind') != 'plan_approval'}
        records, seen = [], set()
        for kind, values in (('choice', args.choice), ('text', args.text)):
            for value in values:
                qid, separator, answer = value.partition('=')
                if not separator or qid not in questions or qid in seen or qid in decided:
                    raise ValueError('answer_invalid')
                seen.add(qid)
                question = questions[qid]
                if kind == 'choice':
                    options = question['options']
                    if re.fullmatch(r'[0-9]+', answer) and int(answer) < len(options):
                        answer = options[int(answer)]['label']
                    elif answer not in [option['label'] for option in options]:
                        hint = ['valid choices for {}:'.format(qid)]
                        hint.extend('  {}: {}'.format(i, intake.prose(history._clean(option['label'], []))[:mapping.TEXT_CAP])
                                    for i, option in enumerate(options))
                        raise ValueError('answer_invalid')
                answer = intake.prose(history._clean(answer, []))[:mapping.TEXT_CAP]
                if not answer.strip():
                    raise ValueError('answer_invalid')
                records.append(dict(schema='orcexe.decision.v1', question_id=qid,
                                    question=intake.prose(history._clean(question['text'], []))[:mapping.TEXT_CAP],
                                    answer=intake.prose(history._clean(answer, []))[:mapping.TEXT_CAP], kind=kind,
                                    goal_hash=form.get('goal_hash'),
                                    form_id=args.form, head=head, at=datetime.now(timezone.utc).isoformat()))
        if not records:
            raise ValueError('answer_invalid')
        read_decisions(directory)
        if gitutil.head(repo) != head:
            raise ValueError('form_invalid')
        # All validation and serialization finish before the first append.
        for record in records:
            fsio.append_jsonl(directory / 'decisions.jsonl', record)
    except (OSError, ValueError, gitutil.GitError, KeyError, TypeError, AttributeError, RecursionError) as error:
        reason = str(error) if type(error) is ValueError and str(error) in ('form_tampered', 'state_corrupt') else 'answer_invalid'
        print('not_seen: ' + reason, file=sys.stderr)
        if hint and reason == 'answer_invalid':
            print('\n'.join(hint), file=sys.stderr)
        return 2
    print(json.dumps(records, ensure_ascii=False) if args.json else 'Recorded {} answer(s).'.format(len(records)))
    return 0
