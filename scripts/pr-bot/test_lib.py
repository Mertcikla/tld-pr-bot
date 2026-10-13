import json
import unittest
from types import SimpleNamespace

from lib import (commentBody, limits, marker, parse_indexers, publicationContext, resolve_tld_version, staleNotice,
                 supported_indexers, upsertComment, validateResult)

head = 'a' * 40
base = 'b' * 40
trusted = {'repository': 'owner/repo', 'prNumber': 7, 'head': head, 'targetBase': base, 'runID': 12,
           'runAttempt': 1, 'runURL': 'https://github.com/owner/repo/actions/runs/12'}
ready = {'version': 1, **trusted, 'base': base, 'status': 'ready',
         'markdown': '```mermaid\nflowchart LR\n  a["A"] --> b["B"]\n```\n'}


def pr():
    return {'number': 7, 'state': 'open', 'base': {'sha': base, 'repo': {'full_name': 'owner/repo'}},
            'head': {'sha': head, 'ref': 'feature', 'repo': {'id': 9}}}


def association():
    return {'number': 7, 'head': {'sha': head, 'repo': {'id': 9}}}


def botComment(body):
    return {'id': 8, 'user': {'type': 'Bot', 'login': 'github-actions[bot]'}, 'body': body}


def api_fixture(run_changes=None, pr_changes=None, comments=None, associated=None):
    calls = []
    run = {'repository': {'full_name': 'owner/repo'}, 'workflow_id': 3, 'event': 'pull_request',
           'status': 'completed', 'head_sha': head, 'head_branch': 'feature',
           'head_repository': {'id': 9},
           'pull_requests': [association()] if associated is None else associated}
    run.update(run_changes or {})

    def request(method, path, body=None):
        calls.append({'method': method, 'path': path, 'body': body})
        if '/actions/runs/' in path:
            return run
        if '/actions/workflows/' in path:
            return {'path': '.github/workflows/pr-diagram.yml'}
        if path.endswith('/pulls/7'):
            return {**pr(), **(pr_changes or {})}
        if method in ('POST', 'PATCH'):
            return {'id': 10}
        raise AssertionError(f'Unexpected request: {method} {path}')

    def pages(path):
        if path.endswith('/comments'):
            return list(comments or [])
        if path.endswith('/pulls'):
            return [pr()]
        raise AssertionError(f'Unexpected pages: {path}')

    return SimpleNamespace(calls=calls, request=request, pages=pages)


class LibTest(unittest.TestCase):
    def test_limits_have_documented_defaults_and_reject_invalid_settings(self):
        self.assertEqual(limits({}), {'elements': 80, 'connectors': 160})
        self.assertEqual(limits({'TLD_PR_MAX_ELEMENTS': '1', 'TLD_PR_MAX_CONNECTORS': '20'}),
                         {'elements': 1, 'connectors': 20})
        for value in ['0', '-1', '', '1.5', 'word', '9007199254740992']:
            with self.assertRaises(ValueError):
                limits({'TLD_PR_MAX_ELEMENTS': value})
            with self.assertRaises(ValueError):
                limits({'TLD_PR_MAX_CONNECTORS': value})

    def test_indexers_default_to_auto_and_reject_unknowns(self):
        self.assertEqual(parse_indexers(None), supported_indexers)
        self.assertEqual(parse_indexers(''), supported_indexers)
        self.assertEqual(parse_indexers('auto'), supported_indexers)
        self.assertEqual(parse_indexers('go, python  ruby'), ['go', 'python', 'ruby'])
        self.assertEqual(parse_indexers('go go python'), ['go', 'python'])
        with self.assertRaises(ValueError):
            parse_indexers('go cobol')

    def test_tld_version_accepts_pinned_tags_and_resolves_latest_from_api(self):
        self.assertEqual(resolve_tld_version('v2.4.2'), 'v2.4.2')
        self.assertEqual(resolve_tld_version('2.4.2'), '2.4.2')
        self.assertEqual(resolve_tld_version('v2.4.0-beta.4'), 'v2.4.0-beta.4')
        for value in ['main', 'v2', 'bad tag']:
            with self.assertRaises(ValueError):
                resolve_tld_version(value)
        with self.assertRaises(ValueError):
            resolve_tld_version('latest', repository='bad repo')

        calls = []

        def fetch_impl(url, headers, timeout):
            calls.append({'url': url, 'headers': headers})
            return 200, json.dumps({'tag_name': 'v9.9.9'})

        self.assertEqual(resolve_tld_version('latest', token='secret', fetch_impl=fetch_impl,
                                             api_url='https://api.example.test/'), 'v9.9.9')
        self.assertEqual(calls[0]['url'], 'https://api.example.test/repos/Mertcikla/tld/releases/latest')
        self.assertEqual(calls[0]['headers']['Authorization'], 'Bearer secret')
        self.assertEqual(resolve_tld_version('', fetch_impl=fetch_impl), 'v9.9.9')
        with self.assertRaises(ValueError):
            resolve_tld_version('latest', fetch_impl=lambda url, headers, timeout: (404, ''))
        with self.assertRaises(ValueError):
            resolve_tld_version('latest',
                                fetch_impl=lambda url, headers, timeout: (200, json.dumps({'tag_name': 'nonsense'})))

    def test_valid_results_and_all_advisory_statuses_are_accepted(self):
        self.assertEqual(validateResult(json.dumps(ready), trusted)['status'], 'ready')
        for status in ['skipped', 'empty', 'error']:
            self.assertEqual(validateResult(json.dumps({**ready, 'status': status, 'markdown': ''}),
                                            trusted)['status'], status)

    def test_artifact_identity_status_mermaid_and_size_are_checked(self):
        changes = [
            {'prNumber': 8}, {'repository': 'attacker/repo'}, {'head': base}, {'targetBase': head},
            {'base': 'bad'}, {'status': 'bad'}, {'markdown': 'hello'},
            {'markdown': '```mermaid\nflowchart LR\n```\n@everyone\n```\n```'},
            {'markdown': '```mermaid\nflowchart LR\n%%{init: {}}%%\n```'},
        ]
        for change in changes:
            with self.assertRaises(ValueError):
                validateResult(json.dumps({**ready, **change}), trusted)
        with self.assertRaises(ValueError):
            validateResult('x' * 300_000, trusted)
        with self.assertRaises(ValueError):
            validateResult('not-json', trusted)

    def test_successful_comments_display_the_mermaid_diagram_and_signature(self):
        body = commentBody(ready, trusted)
        self.assertEqual(body, f"{marker}\n<!-- tld-run:12:1 -->\n{ready['markdown'].strip()}"
                         '\n\n[tld diff diagram bot](https://github.com/Mertcikla/tld-pr-bot)')

    def test_unsuccessful_results_and_oversized_diagrams_create_no_comment(self):
        oversized = {**ready, 'markdown': 'x' * 90_000}
        results = [oversized] + [{**ready, 'status': status, 'reason': '@everyone'}
                                 for status in ['empty', 'error', 'skipped']]
        for result in results:
            with self.subTest(status=result['status']):
                self.assertIsNone(commentBody(result, trusted))
                empty = api_fixture(comments=[])
                self.assertFalse(upsertComment(empty, trusted, result))
                self.assertTrue(all(call['method'] == 'GET' for call in empty.calls))

    def test_unsuccessful_updates_preserve_the_diagram_and_mark_it_stale(self):
        # A legacy comment retains its diagram and surrounding content on failure.
        published = (f"{marker}\n<!-- tld-run:11:1 -->\n### tld PR diagram\n\n"
                     f"`{base[:12]}` → `{head[:12]}`.\n\n{ready['markdown']}\n[Workflow run](https://example.com)")
        results = [{**ready, 'status': status, 'reason': '@everyone'}
                   for status in ['empty', 'error', 'skipped']]
        results.append({**ready, 'markdown': 'x' * 90_000})
        for result in results:
            with self.subTest(status=result['status']):
                api = api_fixture(comments=[botComment(published)])
                self.assertTrue(upsertComment(api, trusted, result))
                call = api.calls[-1]
                self.assertEqual(call['method'], 'PATCH')
                self.assertEqual(call['path'], '/repos/owner/repo/issues/comments/8')
                self.assertEqual(call['body']['body'],
                                 published.replace('tld-run:11:1', 'tld-run:12:1') + f'\n\n{staleNotice}')
                self.assertNotIn('@everyone', call['body']['body'])

    def test_stale_notice_is_idempotent_and_success_clears_it(self):
        failed = {**ready, 'status': 'error'}
        api = api_fixture(comments=[botComment(commentBody(ready, trusted))])
        self.assertTrue(upsertComment(api, trusted, failed))
        stale_body = api.calls[-1]['body']['body']
        same = api_fixture(comments=[botComment(stale_body)])
        self.assertTrue(upsertComment(same, trusted, failed))
        self.assertTrue(all(call['method'] == 'GET' for call in same.calls))

        later = {**trusted, 'runID': 13}
        repeated = api_fixture(comments=[botComment(stale_body)])
        self.assertTrue(upsertComment(repeated, later, failed))
        repeated_body = repeated.calls[-1]['body']['body']
        self.assertEqual(repeated_body.count(staleNotice), 1)
        self.assertIn('<!-- tld-run:13:1 -->', repeated_body)

        recovered = api_fixture(comments=[botComment(repeated_body)])
        new_diagram = {**ready, 'markdown': '```mermaid\nflowchart LR\n  c["New"]\n```'}
        self.assertTrue(upsertComment(recovered, later, new_diagram))
        self.assertEqual(recovered.calls[-1]['body']['body'], commentBody(new_diagram, later))

    def test_failures_do_not_add_notices_to_old_comments_without_a_diagram(self):
        api = api_fixture(comments=[botComment(f'{marker}\nDiagram generation was unsuccessful.')])
        self.assertFalse(upsertComment(api, trusted, {**ready, 'status': 'error'}))
        self.assertTrue(all(call['method'] == 'GET' for call in api.calls))

    def test_publisher_accepts_associated_same_repo_and_fork_prs(self):
        self.assertEqual(publicationContext(api_fixture(), 'owner/repo', 12, 'pr-diagram.yml'), trusted)
        api = api_fixture(run_changes={'head_repository': {'id': 123}})
        self.assertEqual(publicationContext(api, 'owner/repo', 12, 'pr-diagram.yml')['prNumber'], 7)

    def test_fork_runs_missing_pr_associations_use_verified_commit_associations(self):
        self.assertEqual(publicationContext(api_fixture(associated=[]), 'owner/repo', 12,
                                            'pr-diagram.yml')['prNumber'], 7)
        self.assertIsNone(publicationContext(api_fixture(associated=[], run_changes={'head_repository': {'id': 123}}),
                                             'owner/repo', 12, 'pr-diagram.yml'))

    def test_publisher_ignores_stale_closed_unrelated_and_ambiguous_runs(self):
        options = [
            {'run_changes': {'event': 'push'}},
            {'run_changes': {'status': 'in_progress'}},
            {'run_changes': {'repository': {'full_name': 'other/repo'}}},
            {'run_changes': {'head_sha': base}},
            {'pr_changes': {'state': 'closed'}},
            {'associated': [association(), association()]},
        ]
        for option in options:
            self.assertIsNone(publicationContext(api_fixture(**option), 'owner/repo', 12, 'pr-diagram.yml'))
        self.assertIsNone(publicationContext(api_fixture(), 'owner/repo', 12, 'other.yml'))

    def test_create_and_update_exactly_the_bot_owned_marked_comment(self):
        spoof = {'id': 5, 'user': {'type': 'User', 'login': 'someone'}, 'body': marker}
        create = api_fixture(comments=[spoof])
        self.assertTrue(upsertComment(create, trusted, ready))
        self.assertEqual(create.calls[-1]['method'], 'POST')
        update = api_fixture(comments=[spoof, {'id': 8, 'user': {'type': 'Bot', 'login': 'github-actions[bot]'},
                                               'body': marker}])
        self.assertTrue(upsertComment(update, trusted, ready))
        self.assertEqual(update.calls[-1]['path'], '/repos/owner/repo/issues/comments/8')
        self.assertEqual(update.calls[-1]['method'], 'PATCH')

    def test_publication_rechecks_revisions_and_does_not_regress_newer_comments(self):
        stale = api_fixture(pr_changes={'base': {'sha': head}})
        self.assertFalse(upsertComment(stale, trusted, ready))
        self.assertTrue(all(call['method'] == 'GET' for call in stale.calls))
        newer = api_fixture(comments=[{'user': {'type': 'Bot', 'login': 'github-actions[bot]'},
                                       'body': f'{marker}\n<!-- tld-run:13:1 -->'}])
        self.assertFalse(upsertComment(newer, trusted, ready))
        same = api_fixture(comments=[{'user': {'type': 'Bot', 'login': 'github-actions[bot]'},
                                      'body': commentBody(ready, trusted)}])
        self.assertTrue(upsertComment(same, trusted, ready))
        self.assertTrue(all(call['method'] == 'GET' for call in same.calls))

    def test_failed_updates_cannot_mark_newer_or_outdated_pr_diagrams_stale(self):
        failed = {**ready, 'status': 'error'}
        cases = [
            {'comments': [botComment(commentBody(ready, {**trusted, 'runID': 13}))]},
            {'comments': [botComment(commentBody(ready, {**trusted, 'runAttempt': 2}))]},
            {'comments': [botComment(commentBody(ready, trusted))], 'pr_changes': {'state': 'closed'}},
            {'comments': [botComment(commentBody(ready, trusted))], 'pr_changes': {'base': {'sha': head}}},
            {'comments': [botComment(commentBody(ready, trusted))], 'pr_changes': {'head': {'sha': base}}},
            {'comments': [{'id': 8, 'user': {'type': 'User', 'login': 'someone'},
                           'body': commentBody(ready, trusted)}]},
        ]
        for case in cases:
            with self.subTest(case=case):
                api = api_fixture(**case)
                self.assertFalse(upsertComment(api, trusted, failed))
                self.assertTrue(all(call['method'] == 'GET' for call in api.calls))


if __name__ == '__main__':
    unittest.main()
