import json
import os
import re
import subprocess
import sys
from pathlib import Path

from lib import (GitHub, digest, limits, maxResultBytes, parse_indexers, publicationContext,
                 resolve_tld_version, upsertComment, validateResult)

env = os.environ
mode = sys.argv[1] if len(sys.argv) > 1 else ''
root = Path(__file__).resolve().parents[2]
stateDir = Path(env.get('RUNNER_TEMP') or '/tmp') / 'tld-pr-bot'
resultDir = stateDir / 'result'
dataDir = stateDir / 'data'
configDir = stateDir / 'config'
binaryDir = stateDir / 'bin'
resultDir.mkdir(parents=True, exist_ok=True)
event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())


def output(key, value):
    target = env.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a') as handle:
            handle.write(f'{key}<<TLD_OUTPUT\n{value}\nTLD_OUTPUT\n')


def summary(text):
    print(text)
    target = env.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a') as handle:
            handle.write(f'{text}\n')


def execute(command, args, cwd):
    result = subprocess.run([command, *args], cwd=cwd, capture_output=True, text=True, timeout=25 * 60)
    if result.returncode != 0:
        detail = (result.stderr or '')[-6000:] or str(result.returncode)
        raise RuntimeError(f'{command} failed: {detail}')
    return (result.stdout or '').strip()


repository = str(Path(env.get('INPUT_REPOSITORY_PATH') or env['GITHUB_WORKSPACE']).resolve())
statePath = stateDir / 'state.json'


def readState():
    return json.loads(statePath.read_text())


def saveResult(result):
    (resultDir / 'result.json').write_text(json.dumps(result))


# sourceDigest namespaces the snapshot cache by the action source, so an edit to
# the bot invalidates stale indexes.
def sourceDigest(directory):
    inputs = []
    excluded = {'.git', 'node_modules', 'dist', 'tmp', '.bin', 'data', 'public', 'coverage',
                'playwright-report', 'test-results', '__pycache__'}
    pattern = re.compile(r'\.(py|yml|yaml|json)$')

    def visit(dir_path, prefix=''):
        for entry in sorted(Path(dir_path).iterdir(), key=lambda item: item.name):
            if entry.is_symlink() or entry.name in excluded:
                continue
            name = f'{prefix}{entry.name}'
            if entry.is_dir():
                visit(entry, f'{name}/')
            elif pattern.search(name):
                inputs.append(name)
                inputs.append(digest(entry.read_bytes()))

    visit(directory)
    return digest('\n'.join(inputs))


def initialize():
    for directory in (dataDir, configDir, binaryDir):
        directory.mkdir(parents=True, exist_ok=True)
    tldVersion = resolve_tld_version(env.get('INPUT_TLD_VERSION'), token=env.get('GITHUB_TOKEN'),
                                     api_url=env.get('GITHUB_API_URL'))
    pr = event.get('pull_request')
    if env.get('INPUT_MODE') != 'generate' or not pr or event['repository']['full_name'] != env.get('GITHUB_REPOSITORY'):
        raise RuntimeError('Generation requires generate mode and a pull_request event')
    head = pr['head']['sha']
    targetBase = pr['base']['sha']
    for sha in (targetBase, head):
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise RuntimeError('Invalid revision')
        try:
            execute('git', ['cat-file', '-e', f'{sha}^{{commit}}'], repository)
        except RuntimeError:
            execute('git', ['fetch', '--no-tags', 'origin', sha], repository)
    base = execute('git', ['merge-base', targetBase, head], repository)
    state = {'version': 1, 'repository': env.get('GITHUB_REPOSITORY'), 'prNumber': pr['number'],
             'targetBase': targetBase, 'base': base, 'head': head, 'status': 'error'}
    statePath.write_text(json.dumps(state))
    saveResult(state)  # Remains a valid advisory failure artifact if setup fails.
    enabled = parse_indexers(env.get('INPUT_INDEXERS'))
    source = sourceDigest(root)
    # The `scip-indexersN` suffix versions the cache for indexer install
    # commands/versions; bump it whenever those change.
    tools = f'tld{tldVersion[1:]}-go1.26.2-node24-scip-indexers3'
    source_input = (f"{env.get('GITHUB_REPOSITORY')}|{source}|{tools}|"
                    f"{' '.join(enabled)}|{env.get('INPUT_PREPARE_COMMAND')}")
    namespace = (f"tld-pr-v1-{env.get('RUNNER_OS')}-{env.get('RUNNER_ARCH')}-"
                 f'{digest(source_input)[:32]}')
    scope = f"pr-{state['prNumber']}"
    output('binary-key', f"tld-binary-v2-{env.get('RUNNER_OS')}-{env.get('RUNNER_ARCH')}-{tools}")
    output('tld-version', tldVersion)
    output('indexers', ' '.join(enabled))
    output('binary-dir', str(binaryDir))
    output('data-dir', str(dataDir))
    output('result-dir', str(resultDir))
    output('cache-key', f"{namespace}-{scope}-{base}-{head}-{env.get('GITHUB_RUN_ID')}-{env.get('GITHUB_RUN_ATTEMPT')}")
    output('restore-keys', '\n'.join([f'{namespace}-{scope}-{base}-', f'{namespace}-{scope}-']))
    limits(env)


def generate():
    state = readState()
    if env.get('INPUT_TOOLS_READY') == 'false':
        raise RuntimeError('tld or SCIP installation failed')
    cap = limits(env)
    reportPath = stateDir / 'compare-report.json'
    result = subprocess.run([
        str(binaryDir / 'tld'), 'git', 'compare', repository, state['base'], state['head'],
        '--markdown', '--blast-radius', '0', '--max-nodes', '0', '--max-bytes', '0',
        '--max-elements', str(cap['elements']), '--max-connectors', str(cap['connectors']),
        '--report-json', str(reportPath), '--prepare-command', env.get('INPUT_PREPARE_COMMAND') or '',
        '--data-dir', str(dataDir)],
        cwd=repository, capture_output=True, text=True, timeout=25 * 60,
        env={**env, 'TLD_CONFIG_DIR': str(configDir), 'TLD_UPDATES_AUTO': 'false', 'GOWORK': 'off'})
    if result.stderr:
        print(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f'tld exited {result.returncode}')
    report = json.loads(reportPath.read_text())
    if not isinstance(report, dict):
        raise RuntimeError('Invalid compare report: expected an object')
    # Current tld reports use canonical comparison metadata; older releases
    # emitted the revisions at the top level.
    comparison = report.get('comparison', report)
    if (not isinstance(comparison, dict) or comparison.get('base') != state['base']
            or comparison.get('head') != state['head']):
        raise RuntimeError('Invalid compare report: base/head do not match the requested revisions')
    if (not isinstance(report.get('warnings'), list)
            or any(not isinstance(warning, str) for warning in report['warnings'])):
        raise RuntimeError('Invalid compare report: warnings must be a list of strings')
    if report.get('status') not in ('ready', 'skipped', 'empty'):
        raise RuntimeError('Invalid compare report: unsupported status')
    payload = {'version': state['version'], 'repository': state['repository'], 'prNumber': state['prNumber'],
               'targetBase': state['targetBase'], 'base': state['base'], 'head': state['head'],
               'status': report.get('status'),
               'markdown': result.stdout if report.get('status') == 'ready' else ''}
    if report.get('status') == 'skipped':
        payload['skipReason'] = 'diagram-limit'
    if report['warnings']:
        print('\n'.join(report['warnings']))
        payload['status'] = 'error'
        payload['markdown'] = ''
        payload['reason'] = 'Indexing produced warnings; diagram omitted.'
    if len(json.dumps(payload).encode('utf-8')) > maxResultBytes:
        payload['status'] = 'skipped'
        payload['markdown'] = ''
        payload['reason'] = 'Output exceeds the artifact size budget.'
        payload['skipReason'] = 'artifact-size'
    saveResult(payload)
    output('status', payload['status'])
    output('cache-ready', 'true' if not report['warnings'] else 'false')
    summary(f"tld PR diagram: {payload['status']}. {payload.get('reason') or ''}")


def checkPublish():
    workflow_run = event.get('workflow_run') or {}
    runID = env.get('INPUT_RUN_ID') or workflow_run.get('id')
    # Never accept an arbitrary run selected through artifact contents.
    if str(runID) != str(workflow_run.get('id')):
        raise RuntimeError('run-id does not match the workflow_run event')
    api = GitHub(env.get('GITHUB_TOKEN'), env.get('GITHUB_API_URL'))
    trusted = publicationContext(api, env.get('GITHUB_REPOSITORY'), runID,
                                 env.get('INPUT_SOURCE_WORKFLOW') or 'pr-diagram.yml',
                                 env.get('GITHUB_SERVER_URL'))
    output('publish', 'true' if trusted else 'false')
    if trusted:
        (stateDir / 'publication.json').write_text(json.dumps(trusted))
        output('run-id', trusted['runID'])
        output('pr-number', trusted['prNumber'])
    else:
        summary('tld PR diagram: ignored stale, closed, or unrelated workflow run.')


def publish():
    trusted = json.loads((stateDir / 'publication.json').read_text())
    path = stateDir / 'download' / 'result.json'
    try:
        if path.stat().st_size > maxResultBytes:
            raise RuntimeError('Result artifact is too large')
        result = validateResult(path.read_text(), trusted)
    except Exception as error:
        summary(f'tld PR diagram artifact rejected: {error}')
        result = {**trusted, 'base': trusted['targetBase'], 'status': 'error'}
    api = GitHub(env.get('GITHUB_TOKEN'), env.get('GITHUB_API_URL'))
    updated = upsertComment(api, trusted, result)
    summary(f"tld PR diagram: comment updated ({result.get('status')})." if updated
            else 'tld PR diagram: comment unchanged.')


def main():
    try:
        if mode == 'init':
            initialize()
        elif mode == 'generate':
            generate()
        elif mode == 'publish-check':
            checkPublish()
        elif mode == 'publish':
            publish()
        else:
            raise RuntimeError(f'Unsupported mode: {mode}')
    except Exception as error:
        output('status', 'error')
        output('cache-ready', 'false')
        if mode in ('init', 'generate') and statePath.exists():
            saveResult({**readState(), 'status': 'error',
                        'reason': 'Diagram generation was unsuccessful; see workflow logs.'})
        summary(f'tld PR diagram advisory error: {error}')


if __name__ == '__main__':
    main()
