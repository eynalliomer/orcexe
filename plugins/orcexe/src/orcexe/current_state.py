"""Human summary derived exclusively from CLI map, plan, refs and ledger."""

from orcexe import fsio, gitutil, ledger, published


def render(main, project=None, plan=None, log=None):
    from orcexe import mapping, planrun
    directory = main / '.orcexe'
    if project is None:
        project = fsio.read_json(directory / 'project-map.json', default={})
    if plan is None:
        plan = fsio.read_json(directory / 'plan.json', default={})
    head = gitutil.head(main)
    update = project.get('update', {})
    lines = ['# Current state', '', 'HEAD: ' + head[:12],
             'Map update: ' + update.get('mode', 'unknown') + ' / ' + project.get('updated_at', 'unknown'),
             'Unknowns: ' + str(len(project.get('unknowns', [])))]
    if plan:
        log = log if log is not None else ledger.Ledger(ledger.default_path(main))
        state = planrun.status(main, plan, log)
        lines += ['Plan: ' + mapping.escape(state['plan_id']) + ' / ' + mapping.escape(state['plan_status']), '']
        lines.extend('- ' + mapping.escape(item['task_id']) + ': ' + mapping.escape(item['state'])
                     for item in state['tasks'])
        tip = planrun.chain_tip(main, plan)
        lines += ['', 'Chain tip: ' + (tip or 'none'),
                  'Land: ' + (state['land']['command'] if state['land'] else 'none')]
    else:
        lines += ['Plan: none', 'Chain tip: none', 'Land: none']
    lines += ['', '<!-- orcexe:human -->', '<!-- /orcexe:human -->', '']
    return '\n'.join(lines)


def write(main, project=None, plan=None, log=None):
    """Regenerate the summary; publication carries human blocks and backs up edits."""
    return published.write_text(main / '.orcexe', 'current-state.md', render(main, project, plan, log))
