import hashlib
import json
import re
import urllib.error
import urllib.request

marker = '<!-- tld-pr-diagram:v1 -->'
staleNotice = '> Diagram is stale: the latest update was unsuccessful.'
maxResultBytes = 256 * 1024
maxCommentBytes = 60 * 1024
_sha_pattern = re.compile(r'^[0-9a-f]{40}$')
_repo_pattern = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
_mermaid_pattern = re.compile(r'^```mermaid\r?\nflowchart LR\r?\n[\s\S]*\r?\n```\s*$')
_MAX_SAFE_INTEGER = 9007199254740991

# Indexer families the tld pipeline can invoke, in install order. `typescript`
# covers the web family (TypeScript/JavaScript).
supported_indexers = ['go', 'typescript', 'python', 'dotnet', 'clang', 'java', 'dart', 'php', 'ruby', 'rust']


# parse_indexers resolves the `indexers` input. The default `auto` selects every
# supported indexer; an explicit list selects a subset.
def parse_indexers(value):
    text = (value or '').strip()
    if not text or text.lower() == 'auto':
        return list(supported_indexers)
    selected = []
    for item in re.split(r'[\s,]+', text):
        if not item:
            continue
        if item not in supported_indexers:
            raise ValueError(f"Unsupported indexer: {item}. Supported: {', '.join(supported_indexers)}")
        if item not in selected:
            selected.append(item)
    return selected


def limits(env):
    def positive(name, fallback):
        value = env.get(name)
        if value is None:
            value = str(fallback)
        if not re.fullmatch(r'[1-9][0-9]*', value) or int(value) > _MAX_SAFE_INTEGER:
            raise ValueError(f'{name} must be a positive integer')
        return int(value)

    return {'elements': positive('TLD_PR_MAX_ELEMENTS', 80), 'connectors': positive('TLD_PR_MAX_CONNECTORS', 160)}


def digest(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


# The tld release the bot downloads and indexes with. Callers may pin an exact
# tag, but the default `latest` resolves the newest stable release, so the bot
# needs no version bump when tld ships.
tldRepository = 'Mertcikla/tld'
_tld_tag_pattern = re.compile(r'^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$')


def _default_fetch(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace')


def resolve_tld_version(requested, token=None, repository=None, api_url=None, fetch_impl=None):
    value = (requested or '').strip() or 'latest'
    if value != 'latest':
        if not _tld_tag_pattern.fullmatch(value):
            raise ValueError(f'Invalid tld version: {value}')
        return value
    repository = repository or tldRepository
    if not _repo_pattern.fullmatch(repository):
        raise ValueError('Invalid tld repository')
    api_url = (api_url or 'https://api.github.com').rstrip('/')
    do_fetch = fetch_impl or _default_fetch
    headers = {'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    status, body = do_fetch(f'{api_url}/repos/{repository}/releases/latest', headers, 30)
    if not 200 <= status < 300:
        raise ValueError(f'Failed to resolve latest tld release: HTTP {status}')
    tag = json.loads(body).get('tag_name')
    if not isinstance(tag, str) or not _tld_tag_pattern.fullmatch(tag):
        raise ValueError(f'Invalid tld release tag: {tag}')
    return tag


# The publisher trusts only the identity fields it re-derives from GitHub, so
# validateResult rechecks them against the artifact downloaded from the run.
def validateResult(raw, trusted):
    if len(raw.encode('utf-8')) > maxResultBytes:
        raise ValueError('Result artifact is too large')
    result = json.loads(raw)
    if (result.get('version') != 1 or result.get('repository') != trusted['repository']
            or result.get('prNumber') != trusted['prNumber'] or result.get('head') != trusted['head']
            or result.get('targetBase') != trusted['targetBase']
            or not isinstance(result.get('base'), str) or not _sha_pattern.fullmatch(result['base'])
            or result.get('status') not in ('ready', 'skipped', 'empty', 'error')):
        raise ValueError('Invalid result identity or status')
    if result.get('status') == 'ready':
        markdown = result.get('markdown')
        if (not isinstance(markdown, str) or not _mermaid_pattern.match(markdown)
                or '```' in markdown[10:markdown.rfind('```')]
                or '%%{' in markdown or '<script' in markdown):
            raise ValueError('Invalid Mermaid output')
    return result


def commentBody(result, trusted):
    if result.get('status') != 'ready':
        return None
    body = (f"{marker}\n{runMarker(trusted)}\n{(result.get('markdown') or '').strip()}"
            '\n\n[tld diff diagram bot](https://github.com/Mertcikla/tld-pr-bot)')
    return body if len(body.encode('utf-8')) <= maxCommentBytes else None


def runMarker(trusted):
    return f"<!-- tld-run:{trusted['runID']}:{trusted.get('runAttempt') or 1} -->"


def staleCommentBody(body, trusted):
    # Preserve the published diagram, including comments produced by older bots.
    if not re.search(r'^```mermaid\r?\n[\s\S]*?\r?\n```', body, re.MULTILINE):
        return None
    if re.search(r'<!-- tld-run:[0-9]+:[0-9]+ -->', body):
        body = re.sub(r'<!-- tld-run:[0-9]+:[0-9]+ -->', runMarker(trusted), body, count=1)
    else:
        body = body.replace(marker, f'{marker}\n{runMarker(trusted)}', 1)
    body = body.rstrip()
    if not body.endswith(f'\n\n{staleNotice}'):
        body += f'\n\n{staleNotice}'
    return body if len(body.encode('utf-8')) <= maxCommentBytes else None


def _http(method, url, headers, data, timeout):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace')


class GitHub:
    def __init__(self, token, api_url='https://api.github.com'):
        self.token = token
        self.api_url = (api_url or 'https://api.github.com').rstrip('/')

    def request(self, method, path, body=None):
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
        }
        data = json.dumps(body).encode('utf-8') if body is not None else None
        status, text = _http(method, f'{self.api_url}{path}', headers, data, 30)
        if not 200 <= status < 300:
            raise ValueError(f'GitHub {method} {path}: HTTP {status}')
        return json.loads(text) if text else None

    def pages(self, path):
        items = []
        page = 1
        while True:
            separator = '&' if '?' in path else '?'
            batch = self.request('GET', f'{path}{separator}per_page=100&page={page}')
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


# The PR identity comes from GitHub, never from the downloaded artifact.
def publicationContext(api, repository, runID, sourceWorkflow, serverURL='https://github.com'):
    if not _repo_pattern.fullmatch(repository) or not re.fullmatch(r'[1-9][0-9]*', str(runID)):
        raise ValueError('Invalid workflow identity')
    prefix = f'/repos/{repository}'
    run = api.request('GET', f'{prefix}/actions/runs/{runID}')
    workflow = api.request('GET', f"{prefix}/actions/workflows/{run['workflow_id']}")
    if (run['repository']['full_name'] != repository or run['event'] != 'pull_request'
            or run['status'] != 'completed' or workflow['path'] != f'.github/workflows/{sourceWorkflow}'
            or not _sha_pattern.fullmatch(run['head_sha'])):
        return None
    associated = run.get('pull_requests') or []
    if len(associated) == 0:
        associated = api.pages(f"{prefix}/commits/{run['head_sha']}/pulls")
        associated = [pr for pr in associated
                      if pr['head']['sha'] == run['head_sha'] and pr['head']['ref'] == run['head_branch']
                      and (pr['head'].get('repo') or {}).get('id') == (run.get('head_repository') or {}).get('id')]
    if len(associated) != 1:
        return None
    pr = api.request('GET', f"{prefix}/pulls/{associated[0]['number']}")
    if (pr['state'] != 'open' or pr['base']['repo']['full_name'] != repository
            or pr['head']['sha'] != run['head_sha']
            or (associated[0].get('head') or {}).get('sha') != pr['head']['sha']
            or (associated[0].get('head') or {}).get('repo', {}).get('id') != pr['head']['repo'].get('id')):
        return None
    return {'repository': repository, 'prNumber': pr['number'], 'head': pr['head']['sha'],
            'targetBase': pr['base']['sha'], 'runID': int(runID),
            'runAttempt': run.get('run_attempt') or 1,
            'runURL': f'{serverURL}/{repository}/actions/runs/{runID}'}


def upsertComment(api, trusted, result):
    body = commentBody(result, trusted)
    # Recheck immediately before a mutation; a newer PR update may have arrived.
    prefix = f"/repos/{trusted['repository']}"
    pr = api.request('GET', f"{prefix}/pulls/{trusted['prNumber']}")
    if pr['state'] != 'open' or pr['head']['sha'] != trusted['head'] or pr['base']['sha'] != trusted['targetBase']:
        return False
    comments = api.pages(f"{prefix}/issues/{trusted['prNumber']}/comments")
    existing = None
    for comment in comments:
        user = comment.get('user') or {}
        if (user.get('type') == 'Bot' and user.get('login') == 'github-actions[bot]'
                and (comment.get('body') or '').startswith(marker)):
            existing = comment
            break
    previous = re.search(r'<!-- tld-run:([0-9]+):([0-9]+) -->', existing.get('body') or '') if existing else None
    if previous and (int(previous.group(1)) > trusted['runID']
                     or (int(previous.group(1)) == trusted['runID']
                         and int(previous.group(2)) > (trusted.get('runAttempt') or 1))):
        return False
    if body is None:
        if not existing:
            return False
        body = staleCommentBody(existing.get('body') or '', trusted)
        if body is None:
            return False
    if existing:
        if existing.get('body') != body:
            api.request('PATCH', f"{prefix}/issues/comments/{existing['id']}", {'body': body})
    else:
        api.request('POST', f"{prefix}/issues/{trusted['prNumber']}/comments", {'body': body})
    return True
