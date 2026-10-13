import contextlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lib import commentBody, staleNotice

script = Path(__file__).resolve().parent / 'main.py'

tld_source = '''import json
import os
import sys

args = sys.argv[1:]
warnings = ['indexer failed'] if os.environ.get('FAKE_WARNINGS') else []
if os.environ.get('FAKE_ERROR'):
    sys.exit(1)
with open(args[args.index('--report-json') + 1], 'w') as handle:
    comparison = {'base': args[3], 'head': args[4]}
    report = {'status': os.environ.get('FAKE_STATUS') or 'ready',
              'comparison': comparison, 'warnings': warnings}
    if os.environ.get('FAKE_LEGACY_REPORT'):
        report.pop('comparison')
        report.update(comparison)
    if os.environ.get('FAKE_REPORT_JSON'):
        report = json.loads(os.environ['FAKE_REPORT_JSON'])
    json.dump(report, handle)
sys.stdout.write('```mermaid\\nflowchart LR\\n  a["A"] --> b["B"]\\n```\\n')
'''


class MainTest(unittest.TestCase):
    def fixture(self):
        directory = Path(tempfile.mkdtemp(prefix='tld-action-test-'))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        repo = directory / 'repo'
        repo.mkdir()

        def git(*args):
            result = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        git('init', '-b', 'main')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.com')
        (repo / 'file.txt').write_text('seed')
        git('add', '.')
        git('commit', '-m', 'seed')
        (repo / 'file.txt').write_text('base')
        git('add', '.')
        git('commit', '-m', 'base')
        base = git('rev-parse', 'HEAD')
        (repo / 'file.txt').write_text('head')
        git('add', '.')
        git('commit', '-m', 'head')
        head = git('rev-parse', 'HEAD')
        event_path = directory / 'event.json'
        event_path.write_text(json.dumps({'repository': {'full_name': 'owner/repo'},
                                          'pull_request': {'number': 7, 'base': {'sha': base},
                                                           'head': {'sha': head}}}))
        env = {**os.environ, 'RUNNER_TEMP': str(directory), 'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64',
               'GITHUB_EVENT_PATH': str(event_path), 'GITHUB_WORKSPACE': str(repo),
               'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '12', 'GITHUB_RUN_ATTEMPT': '1',
               'GITHUB_OUTPUT': str(directory / 'output'), 'GITHUB_STEP_SUMMARY': str(directory / 'summary'),
               'INPUT_MODE': 'generate', 'INPUT_TLD_VERSION': 'v2.4.2', 'INPUT_REPOSITORY_PATH': str(repo),
               'INPUT_INDEXERS': 'go', 'INPUT_TOOLS_READY': 'true'}

        def run(mode, override=None):
            result = subprocess.run([sys.executable, str(script), mode], env={**env, **(override or {})},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        run('init')
        bin_dir = directory / 'tld-pr-bot' / 'bin'
        bin_dir.mkdir(parents=True, exist_ok=True)

        def stub(name, source):
            path = bin_dir / name
            path.write_text(f'#!{sys.executable}\n{source}\n')
            path.chmod(0o700)

        stub('scip-go', 'print("scip-go 0.2.7")')
        stub('tld', tld_source)
        env['PATH'] = f'{bin_dir}:{env["PATH"]}'

        def result():
            return json.loads((directory / 'tld-pr-bot' / 'result' / 'result.json').read_text())

        return run, result, env, directory

    def test_producer_initializes_identities_and_creates_a_valid_diagram_artifact(self):
        run, result, env, _ = self.fixture()
        run('generate')
        self.assertEqual(result()['status'], 'ready')
        self.assertEqual(result()['prNumber'], 7)
        self.assertIn('```mermaid', result()['markdown'])
        self.assertIn('cache-ready<<TLD_OUTPUT\ntrue', Path(env['GITHUB_OUTPUT']).read_text())

    def test_producer_accepts_legacy_reports_with_top_level_revisions(self):
        run, result, _, _ = self.fixture()
        run('generate', {'FAKE_LEGACY_REPORT': '1'})
        self.assertEqual(result()['status'], 'ready')
        self.assertIn('```mermaid', result()['markdown'])

    def test_producer_rejects_invalid_reports_without_saving_the_cache(self):
        run, result, env, directory = self.fixture()
        state = json.loads((directory / 'tld-pr-bot' / 'state.json').read_text())
        comparison = {'base': state['base'], 'head': state['head']}
        valid = {'status': 'ready', 'comparison': comparison, 'warnings': []}
        reports = [
            [],
            {**valid, 'comparison': None},
            {**valid, 'comparison': {**comparison, 'base': '0' * 40}},
            {**valid, 'comparison': {**comparison, 'head': '0' * 40}},
            {**valid, 'comparison': {}, **comparison},
            {**valid, 'warnings': None},
            {**valid, 'warnings': [None]},
            {**valid, 'status': 'unknown'},
        ]
        for report in reports:
            with self.subTest(report=report):
                Path(env['GITHUB_OUTPUT']).write_text('')
                logs = run('generate', {'FAKE_REPORT_JSON': json.dumps(report)})
                self.assertIn('Invalid compare report:', logs)
                self.assertEqual(result()['status'], 'error')
                outputs = Path(env['GITHUB_OUTPUT']).read_text()
                self.assertIn('cache-ready<<TLD_OUTPUT\nfalse', outputs)
                self.assertNotIn('cache-ready<<TLD_OUTPUT\ntrue', outputs)

    def test_empty_canonical_reports_are_accepted_and_save_healthy_indexes(self):
        run, result, env, _ = self.fixture()
        logs = run('generate', {'FAKE_STATUS': 'empty'})
        self.assertNotIn('Invalid compare report', logs)
        self.assertEqual(result()['status'], 'empty')
        self.assertEqual(result()['markdown'], '')
        self.assertIn('cache-ready<<TLD_OUTPUT\ntrue', Path(env['GITHUB_OUTPUT']).read_text())

    def test_partial_indexes_omit_diagrams_and_do_not_save_the_cache(self):
        run, result, env, _ = self.fixture()
        run('generate', {'FAKE_WARNINGS': '1'})
        self.assertEqual(result()['status'], 'error')
        self.assertEqual(result()['markdown'], '')
        self.assertIn('cache-ready<<TLD_OUTPUT\nfalse', Path(env['GITHUB_OUTPUT']).read_text())

    def test_install_and_indexing_failures_preserve_an_advisory_failure_artifact(self):
        run, result, _, _ = self.fixture()
        run('generate', {'INPUT_TOOLS_READY': 'false'})
        self.assertEqual(result()['status'], 'error')
        run('generate', {'FAKE_ERROR': '1'})
        self.assertEqual(result()['status'], 'error')

    def test_invalid_limits_are_advisory_and_skipped_renders_save_healthy_indexes(self):
        run, result, env, _ = self.fixture()
        run('init', {'TLD_PR_MAX_ELEMENTS': '0'})
        self.assertEqual(result()['status'], 'error')
        run('generate', {'FAKE_STATUS': 'skipped'})
        self.assertEqual(result()['status'], 'skipped')
        self.assertEqual(result()['markdown'], '')
        self.assertIn('cache-ready<<TLD_OUTPUT\ntrue', Path(env['GITHUB_OUTPUT']).read_text())

    def test_init_resolves_auto_to_every_supported_indexer(self):
        run, _, env, _ = self.fixture()
        run('init', {'INPUT_INDEXERS': 'auto'})
        resolved = re.findall(r'indexers<<TLD_OUTPUT\n([^\n]+)', Path(env['GITHUB_OUTPUT']).read_text())
        self.assertEqual(resolved[-1].split(), [
            'go', 'typescript', 'python', 'dotnet', 'clang', 'java', 'dart', 'php', 'ruby', 'rust'])

    def test_pr_caches_preserve_the_comparison_and_config_changes_namespace(self):
        run, result, env, _ = self.fixture()
        self.assertNotEqual(result()['base'], result()['head'])
        self.assertEqual(result()['prNumber'], 7)
        output_path = Path(env['GITHUB_OUTPUT'])
        first = output_path.read_text()
        release = re.search(r'tld-version<<TLD_OUTPUT\n(v[^\n]+)', first).group(1)
        self.assertIn(f'tld-binary-v2-Linux-X64-tld{release[1:]}-', first)
        run('init', {'INPUT_PREPARE_COMMAND': 'echo setup'})
        keys = re.findall(r'cache-key<<TLD_OUTPUT\n([^\n]+)', output_path.read_text())
        self.assertIn('-pr-7-', first)
        self.assertNotEqual(keys[-1], keys[-2])

    def test_publisher_preserves_diagrams_for_failed_or_unavailable_results(self):
        _, _, env, directory = self.fixture()
        state_dir = directory / 'tld-pr-bot'
        state = json.loads((state_dir / 'state.json').read_text())
        trusted = {**state, 'runID': 12, 'runAttempt': 1}
        (state_dir / 'publication.json').write_text(json.dumps(trusted))
        download = state_dir / 'download'
        download.mkdir()
        artifact = download / 'result.json'
        ready = {**state, 'status': 'ready',
                 'markdown': '```mermaid\nflowchart LR\n  a["A"] --> b["B"]\n```'}
        published = commentBody(ready, {**trusted, 'runID': 11})
        cases = {
            'ready': json.dumps(ready),
            'error': json.dumps({**state, 'status': 'error'}),
            'empty': json.dumps({**state, 'status': 'empty'}),
            'skipped': json.dumps({**state, 'status': 'skipped'}),
            'invalid': 'not-json',
            'wrong-identity': json.dumps({**ready, 'prNumber': 8}),
            'oversized': 'x' * (256 * 1024 + 1),
            'missing': None,
        }
        for name, raw in cases.items():
            with self.subTest(result=name):
                if raw is None:
                    artifact.unlink(missing_ok=True)
                else:
                    artifact.write_text(raw)
                api = Mock()
                api.request.return_value = {'state': 'open', 'head': {'sha': state['head']},
                                            'base': {'sha': state['targetBase']}}
                api.pages.return_value = [
                    {'id': 8, 'user': {'type': 'Bot', 'login': 'github-actions[bot]'}, 'body': published}]
                with (patch.dict(os.environ, env), patch('lib.GitHub', return_value=api),
                      contextlib.redirect_stdout(io.StringIO())):
                    runpy.run_path(str(script))['publish']()
                expected = commentBody(ready, trusted)
                if name != 'ready':
                    expected += f'\n\n{staleNotice}'
                self.assertEqual(api.request.call_args.args,
                                 ('PATCH', '/repos/owner/repo/issues/comments/8', {'body': expected}))


if __name__ == '__main__':
    unittest.main()
