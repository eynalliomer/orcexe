"""Provider token patterns.

Source: https://github.com/gitleaks/gitleaks/blob/5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c/config/gitleaks.toml

MIT License

Copyright (c) 2019 Zachary Rice

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Modified: token spans use lookaround boundaries, ASCII classes and scoped flags;
assignment context and consuming suffixes removed except Discord hex context.
AWS alphabet includes all digits; GitLab PAT accepts longer routable bodies.
Gitleaks entropy / allowlists are intentionally not imported: these distinctive
prefixes are sufficient here. Local extensions have source="orcexe:v1".
"""

RULES = (
    dict(id='anthropic-admin-api-key', kind='anthropic_admin_api_key', keywords=('sk-ant-admin01-',),
         regex='(?<![A-Za-z0-9_-])(?:sk-ant-admin01-[A-Za-z0-9_-]{93}AA)(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:anthropic-admin-api-key@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='anthropic-api-key', kind='anthropic_api_key', keywords=('sk-ant-api03-',),
         regex='(?<![A-Za-z0-9_-])(?:sk-ant-api03-[A-Za-z0-9_-]{93}AA)(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:anthropic-api-key@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='openai-api-key', kind='openai_api_key', keywords=('t3blbkfj',),
         regex='(?<![A-Za-z0-9_-])(?:sk-(?:(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})T3BlbkFJ(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})|[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}))(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:openai-api-key@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='github-app-token', kind='github_app_token', keywords=('ghu_', 'ghs_'),
         regex='(?<![A-Za-z0-9_-])(?:gh[us]_[A-Za-z0-9]{36})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:github-app-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='github-fine-grained-pat', kind='github_fine_grained_pat', keywords=('github_pat_',),
         regex='(?<![A-Za-z0-9_-])(?:github_pat_[A-Za-z0-9_]{82})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:github-fine-grained-pat@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='github-oauth', kind='github_oauth', keywords=('gho_',),
         regex='(?<![A-Za-z0-9_-])(?:gho_[A-Za-z0-9]{36})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:github-oauth@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='github-pat', kind='github_pat', keywords=('ghp_',),
         regex='(?<![A-Za-z0-9_-])(?:ghp_[A-Za-z0-9]{36})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:github-pat@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='github-refresh-token', kind='github_refresh_token', keywords=('ghr_',),
         regex='(?<![A-Za-z0-9_-])(?:ghr_[A-Za-z0-9]{36})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:github-refresh-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='gitlab-pat', kind='gitlab_pat', keywords=('glpat-',),
         regex='(?<![A-Za-z0-9_-])(?:glpat-[A-Za-z0-9_-]{20,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:gitlab-pat@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='aws-access-token', kind='aws_access_token', keywords=('akia', 'asia', 'abia', 'acca', 'a3t'),
         regex='(?<![A-Za-z0-9_-])(?:(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:aws-access-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='gcp-api-key', kind='gcp_api_key', keywords=('aiza',),
         regex='(?<![A-Za-z0-9_-])(?:AIza[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:gcp-api-key@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='slack-bot-token', kind='slack_bot_token', keywords=('xoxb-',),
         regex='(?<![A-Za-z0-9_-])(?:xoxb-[0-9]{10,13}-[0-9]{10,13}[A-Za-z0-9-]*)(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:slack-bot-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='slack-legacy-token', kind='slack_legacy_token', keywords=('xoxo-', 'xoxs-'),
         regex='(?<![A-Za-z0-9_-])(?:xox[os]-[0-9]+-[0-9]+-[0-9]+-[a-fA-F0-9]+)(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:slack-legacy-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='slack-legacy-workspace-token', kind='slack_legacy_workspace_token', keywords=('xoxa-', 'xoxr-'),
         regex='(?<![A-Za-z0-9_-])(?:xox[ar]-(?:[0-9]-)?[A-Za-z0-9]{8,48})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:slack-legacy-workspace-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='slack-user-token', kind='slack_user_token', keywords=('xoxp-', 'xoxe-'),
         regex='(?<![A-Za-z0-9_-])(?:xox[pe](?:-[0-9]{10,13}){3}-[A-Za-z0-9-]{28,34})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:slack-user-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='stripe-access-token', kind='stripe_access_token', keywords=('sk_test_', 'sk_live_', 'rk_test_', 'rk_live_', 'sk_prod_', 'rk_prod_'),
         regex='(?<![A-Za-z0-9_-])(?:(?:sk|rk)_(?:test|live|prod)_[A-Za-z0-9]{10,99})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:stripe-access-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='npm-access-token', kind='npm_access_token', keywords=('npm_',),
         regex='(?<![A-Za-z0-9_-])(?:(?i:npm_[a-z0-9]{36}))(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:npm-access-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='pypi-upload-token', kind='pypi_upload_token', keywords=('pypi-',),
         regex='(?<![A-Za-z0-9_-])(?:pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,1000})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:pypi-upload-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='huggingface-access-token', kind='huggingface_access_token', keywords=('hf_',),
         regex='(?<![A-Za-z0-9_-])(?:hf_(?i:[a-z]{34}))(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:huggingface-access-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='jwt', kind='jwt', keywords=('ey',),
         regex='(?<![A-Za-z0-9_-])(?:ey[A-Za-z0-9]{17,}\\.ey[A-Za-z0-9/_-]{17,}\\.[A-Za-z0-9/_-]{10,}={0,2})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:jwt@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='sendgrid-api-token', kind='sendgrid_api_token', keywords=('sg.',),
         regex='(?<![A-Za-z0-9_-])(?:SG\\.(?i:[a-z0-9=_\\-.]{66}))(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:sendgrid-api-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='twilio-api-key', kind='twilio_api_key', keywords=('sk',),
         regex='(?<![A-Za-z0-9_-])(?:SK[0-9a-fA-F]{32})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:twilio-api-key@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='telegram-bot-api-token', kind='telegram_bot_api_token', keywords=(':a',),
         regex='(?<![A-Za-z0-9_-])(?:[0-9]{5,16}:A[A-Za-z0-9_-]{34})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:telegram-bot-api-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='discord-api-token', kind='discord_api_token', keywords=('discord',),
         regex='(?<![A-Za-z0-9_-])(?:(?i:discord)[ \\t]*[=:][ \\t]*[\\x22\\x27]?(?P<secret>[a-f0-9]{64}))(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:discord-api-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='digitalocean-pat', kind='digitalocean_pat', keywords=('dop_v1_',),
         regex='(?<![A-Za-z0-9_-])(?:dop_v1_[a-f0-9]{64})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:digitalocean-pat@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='linear-api-key', kind='linear_api_key', keywords=('lin_api_',),
         regex='(?<![A-Za-z0-9_-])(?:lin_api_(?i:[a-z0-9]{40}))(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:linear-api-key@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='notion-api-token', kind='notion_api_token', keywords=('ntn_',),
         regex='(?<![A-Za-z0-9_-])(?:ntn_[0-9]{11}[A-Za-z0-9]{35})(?![A-Za-z0-9_-])', min_entropy=None,
         source='gitleaks:notion-api-token@5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c', modified='Python token-span port; see module notes'),
    dict(id='anthropic-oauth', kind='anthropic_oauth', keywords=('sk-ant-oat01-',),
         regex='(?<![A-Za-z0-9_-])(?:sk-ant-oat01-[A-Za-z0-9_-]{40,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='orcexe:v1', modified='Original extension for v1 coverage'),
    dict(id='openai-project', kind='openai_project', keywords=('sk-proj-', 'sk-svcacct-', 'sk-admin-'),
         regex='(?<![A-Za-z0-9_-])(?:sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='orcexe:v1', modified='Original extension for v1 coverage'),
    dict(id='openai-legacy', kind='openai_legacy', keywords=('sk-',),
         regex='(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9]{20,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='orcexe:v1', modified='Original extension for v1 coverage'),
    dict(id='slack-prefix', kind='slack_prefix', keywords=('xoxa-', 'xoxb-', 'xoxp-', 'xoxo-', 'xoxs-', 'xoxr-'),
         regex='(?<![A-Za-z0-9_-])(?:xox[abposr]-[A-Za-z0-9-]{16,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='orcexe:v1', modified='Original extension for v1 coverage'),
    dict(id='notion-legacy', kind='notion_legacy', keywords=('secret_',),
         regex='(?<![A-Za-z0-9_-])(?:secret_[A-Za-z0-9]{32,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='orcexe:v1', modified='Original extension for v1 coverage'),
    dict(id='discord-bot', kind='discord_bot', keywords=('.',),
         regex='(?<![A-Za-z0-9_-])(?:(?:[A-Za-z0-9_-]{24}|[A-Za-z0-9_-]{26})\\.[A-Za-z0-9_-]{6}\\.[A-Za-z0-9_-]{27,})(?![A-Za-z0-9_-])', min_entropy=None,
         source='orcexe:v1', modified='Original extension for v1 coverage'),
)
