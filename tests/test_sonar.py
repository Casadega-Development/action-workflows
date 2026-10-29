"""Exercise the actual reusable-workflow scripts against offline API and hostile data fixtures."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/sonar-scan.yml'
SOURCE = WORKFLOW.read_text()


def shell(name):
    """Extract a literal named step, preserving its real shell/Python rather than copying logic."""
    section = SOURCE.split('      - name: ' + name + '\n', 1)[1]
    body = section.split('        run: |\n', 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith('          '):
            break
        lines.append(line[10:] if line else '')
    return '\n'.join(lines)


class WorkflowScripts(unittest.TestCase):
    """Provide isolated workspaces and synthetic credentials; no test contacts a live service."""

    def setUp(self):
        """Allocate a clean source and runner-temp tree for each independent boundary test."""
        self.temporary = tempfile.TemporaryDirectory(prefix='casadega-sonar-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.runner = self.root / 'runner'
        self.bin = self.root / 'bin'
        for directory in (self.workspace, self.runner, self.bin):
            directory.mkdir()
        self.env = {**os.environ, 'RUNNER_TEMP': str(self.runner), 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'SONAR_HOST_URL': 'https://sonar.example.test', 'EXPECTED_HOST': 'https://sonar.example.test',
                    'SONAR_PROJECT_KEY': 'fixture', 'SONAR_TOKEN': 'synthetic-token', 'EXPECTED_REVISION': 'a' * 40,
                    'USE_PULL_REQUEST_SCAN': 'true', 'SONAR_PULL_REQUEST': '12', 'SONAR_BRANCH_NAME': 'main',
                    'ISSUE_GATE_SCOPE': 'all-open', 'GITHUB_STEP_SUMMARY': str(self.root / 'summary')}
        self.settings = self.workspace / 'sonar-project.properties'
        self.settings.write_text('sonar.host.url=https://sonar.example.test\nsonar.projectKey=fixture\n')

    def execute(self, name, timeout=10):
        """Run the checked-in step with literal fixture values and a bounded local subprocess."""
        return subprocess.run(['bash', '-c', shell(name)], cwd=self.workspace, env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def fake_api(self, responses):
        """Replace curl and sleep with deterministic replies, retaining every actual request argument."""
        self.env['RESPONSES'] = json.dumps(responses)
        self.env['CALLS'] = str(self.root / 'calls')
        script = f'#!{sys.executable}\n' + textwrap.dedent('''
            import json, os, sys
            from pathlib import Path
            calls = Path(os.environ['CALLS'])
            previous = calls.read_text().splitlines() if calls.exists() else []
            with calls.open('a') as output:
                output.write(json.dumps(sys.argv[1:]) + '\\n')
            replies = json.loads(os.environ['RESPONSES'])
            reply = replies[min(len(previous), len(replies) - 1)]
            if reply == 'http-error':
                sys.exit(22)
            print(json.dumps(reply))
        ''')
        (self.bin / 'curl').write_text(script)
        (self.bin / 'curl').chmod(0o755)
        (self.bin / 'sleep').write_text('#!/bin/sh\nexit 0\n')
        (self.bin / 'sleep').chmod(0o755)

    def test_malformed_counts_and_transport_failures_cannot_pass(self):
        """Only a validated numeric count may support a clean issue-gate conclusion."""
        for reply in ({}, {'total': None, 'issues': []}, {'total': '0', 'issues': []},
                      {'total': -1, 'issues': []}, {'total': False, 'issues': []}, {'total': 1, 'issues': []}, {'total': 0, 'issues': [{}]}, 'http-error'):
            with self.subTest(reply=reply):
                self.fake_api([reply])
                result = self.execute('SonarQube Issue Gate')
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_index_lag_requires_two_empty_reads_and_stable_findings_fail_early(self):
        """The oracle is the specified read sequence, including a stale positive count."""
        finding = {'total': 1, 'issues': [{'key': 'issue-1', 'component': 'fixture:src/a.ts', 'message': 'Fix this', 'severity': 'MAJOR', 'type': 'BUG'}]}
        zero = {'total': 0, 'issues': []}
        self.fake_api([finding, zero, zero])
        self.assertEqual(self.execute('SonarQube Issue Gate').returncode, 0)
        self.assertEqual(len(Path(self.env['CALLS']).read_text().splitlines()), 3)
        Path(self.env['CALLS']).unlink()
        self.fake_api([finding])
        result = self.execute('SonarQube Issue Gate')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(Path(self.env['CALLS']).read_text().splitlines()), 3)
        self.assertIn('Fix this', Path(self.env['GITHUB_STEP_SUMMARY']).read_text())

    def test_issue_scope_and_network_bounds_survive_both_analysis_modes(self):
        """Assert public request parameters for branch/PR and new-code/all-open combinations."""
        for pr in ('true', 'false'):
            for scope in ('all-open', 'new-code'):
                with self.subTest(pr=pr, scope=scope):
                    calls = self.root / 'calls'
                    calls.unlink(missing_ok=True)
                    self.env.update(USE_PULL_REQUEST_SCAN=pr, ISSUE_GATE_SCOPE=scope)
                    self.fake_api([{'total': 0, 'issues': []}])
                    self.assertEqual(self.execute('SonarQube Issue Gate').returncode, 0)
                    args = json.loads(calls.read_text().splitlines()[0])
                    self.assertIn('componentKeys=fixture', args)
                    self.assertIn('pullRequest=12' if pr == 'true' else 'branch=main', args)
                    self.assertEqual('inNewCodePeriod=true' in args, scope == 'new-code')
                    self.assertIn('--connect-timeout', args)
                    self.assertIn('--max-time', args)

    def test_hotspot_gate_rejects_malformed_counts_and_transport_failures(self):
        """Strict zero-review policy must not coerce counts or ignore inconsistent lists."""
        for reply in ({}, {'paging': {'total': '0'}, 'hotspots': []},
                      {'paging': {'total': False}, 'hotspots': []},
                      {'paging': {'total': -1}, 'hotspots': []},
                      {'paging': {'total': 0.5}, 'hotspots': []},
                      {'paging': {'total': 0}, 'hotspots': [{}]},
                      {'paging': {'total': 1}, 'hotspots': []}, 'http-error'):
            with self.subTest(reply=reply):
                self.fake_api([reply])
                self.assertNotEqual(self.execute('SonarQube Hotspot Gate').returncode, 0)

    def test_hotspot_gate_requires_stability_and_reports_locations(self):
        """Both analysis modes preserve strict review policy with bounded requests and useful output."""
        zero = {'paging': {'total': 0}, 'hotspots': []}
        finding = {'paging': {'total': 1}, 'hotspots': [
            {'key': 'review-1', 'component': 'fixture:app/example.py', 'line': 9,
             'message': 'Review this boundary', 'vulnerabilityProbability': 'HIGH'}]}
        for pr in ('true', 'false'):
            self.env['USE_PULL_REQUEST_SCAN'] = pr
            for replies, expected_status, expected_reads in (([finding, zero, zero], 0, 3), ([finding], 1, 3)):
                calls = self.root / 'calls'
                calls.unlink(missing_ok=True)
                self.fake_api(replies)
                result = self.execute('SonarQube Hotspot Gate')
                self.assertEqual(result.returncode, expected_status, result.stderr)
                recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(len(recorded), expected_reads)
                self.assertIn('pullRequest=12' if pr == 'true' else 'branch=main', recorded[0])
                self.assertIn('status=TO_REVIEW', recorded[0])
                self.assertIn('--connect-timeout', recorded[0])
                self.assertIn('--max-time', recorded[0])
                if expected_status:
                    self.assertIn('fixture:app/example.py:9', Path(self.env['GITHUB_STEP_SUMMARY']).read_text())
        # Alternating evidence never establishes a clean result; the retry limit must fail closed.
        (self.root / 'calls').unlink(missing_ok=True)
        self.fake_api([zero, finding] * 6)
        self.assertNotEqual(self.execute('SonarQube Hotspot Gate').returncode, 0)
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 12)

    def test_hotspot_policy_is_opt_in_forwarded_and_independent_of_issue_failure(self):
        """The reusable interface preserves current defaults while enabling stricter consumers."""
        orchestrator = (WORKFLOW.parent / 'sonar-scan-after-tests.yml').read_text()
        for source in (SOURCE, orchestrator):
            policy = source.split('      enforce-zero-unreviewed-hotspots:', 1)[1].split('      enforce-quality-gate:', 1)[0]
            self.assertIn('type: boolean', policy)
            self.assertIn('default: false', policy)
        self.assertIn('enforce-zero-unreviewed-hotspots: ${{ inputs.enforce-zero-unreviewed-hotspots }}', orchestrator)
        step = SOURCE.split('      - name: SonarQube Hotspot Gate', 1)[1].split('      - name: SonarQube Quality Gate Diagnostics', 1)[0]
        self.assertIn("!cancelled() && steps.sonar-ce-wait.outputs.analysis_id != '' && inputs.enforce-zero-unreviewed-hotspots", step)
        self.assertNotIn('success()', step)

    def test_settings_guard_accepts_data_but_refuses_executables_network_and_identity_overrides(self):
        """Exercise Java syntax variants and generated overrides against the actual final guard."""
        valid = self.settings.read_text()
        self.settings.write_text(valid + 'sonar.sources = src,\\\n  scripts\n')
        self.assertEqual(self.execute('Apply and validate scanner settings').returncode, 0)
        for override in ('sonar.nodejs.executable=/tmp/payload', 'sonar.scanner.javaOpts : -javaagent:payload',
                         'http.proxyHost attacker.test', 'javax.net.ssl.trustStore=/tmp/store',
                         'sonar.host.url=https://attacker.test', 'sonar.projectKey=other',
                         'sonar.scanner.\\\n javaOpts=-javaagent:payload', r'sonar.\u006eodejs.executable=/tmp/payload',
                         'sonar.scanner.javaOpts=-javaagent:payload\rsonar.sources=src'):
            with self.subTest(override=override):
                self.settings.write_text(valid)
                (self.workspace / 'sonar-project.coverage.generated.properties').write_text(override + '\n')
                self.assertNotEqual(self.execute('Apply and validate scanner settings').returncode, 0)

    def archive(self, name, members):
        """Construct exact malicious or benign tar members without ever extracting them in the test."""
        handoff = self.runner / 'sonar-handoff'
        handoff.mkdir(exist_ok=True)
        with tarfile.open(handoff / name, 'w:gz') as archive:
            for path, kind, value in members:
                entry = tarfile.TarInfo(path)
                if kind == 'file':
                    data = value.encode()
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))
                else:
                    entry.type = tarfile.SYMTYPE if kind == 'link' else tarfile.LNKTYPE
                    entry.linkname = value
                    archive.addfile(entry)
        (handoff / 'manifest.json').write_text(json.dumps({'revision': 'a' * 40, 'workspace': '/old/runner'}))

    def test_handoff_relocates_reports_and_preserves_confined_dependency_links(self):
        """Legitimate pnpm-like links stay usable, while reports remain ordinary non-executable data."""
        self.archive('inputs.tar.gz', [('coverage/lcov.info', 'file', 'SF:/old/runner/src/a.ts\n')])
        self.archive('modules.tar.gz', [('node_modules/.pnpm/pkg/index.d.ts', 'file', 'export {};'),
                                       ('node_modules/pkg', 'link', '.pnpm/pkg')])
        result = self.execute('Unpack validated analysis data')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / 'coverage/lcov.info').read_text(), f'SF:{self.workspace}/src/a.ts\n')
        self.assertEqual((self.workspace / 'node_modules/pkg/index.d.ts').read_text(), 'export {};')

    def test_handoff_rejects_traversal_links_hardlinks_source_overwrite_and_wrong_revision(self):
        """Each independent hostile archive must fail before it can write outside the checkout."""
        for member in [('../escape.info', 'file', 'bad'), ('/tmp/escape.info', 'file', 'bad'),
                       ('coverage/lcov.info', 'link', '/tmp/escape'), ('coverage/lcov.info', 'hardlink', 'sonar-project.properties'),
                       ('sonar-project.properties', 'file', 'bad')]:
            with self.subTest(member=member):
                self.archive('inputs.tar.gz', [member])
                self.archive('modules.tar.gz', [])
                self.assertNotEqual(self.execute('Unpack validated analysis data').returncode, 0)
        for target in ('/tmp/escape', '../../escape'):
            self.archive('inputs.tar.gz', [])
            self.archive('modules.tar.gz', [('node_modules/pkg', 'link', target)])
            self.assertNotEqual(self.execute('Unpack validated analysis data').returncode, 0)
        self.env['EXPECTED_REVISION'] = 'b' * 40
        self.assertNotEqual(self.execute('Unpack validated analysis data').returncode, 0)
        self.assertFalse((self.root / 'escape.info').exists())

    def test_failed_preparation_is_a_failed_scan_and_hosted_runner_is_required(self):
        """Skipped/cancelled/failed prerequisites may never be reported as a successful analysis."""
        self.env['IDENTITY_RESULT'] = 'success'
        for outcome in ('failure', 'skipped', 'cancelled', ''):
            self.env['PREPARE_RESULT'] = outcome
            self.assertNotEqual(self.execute('Require prepared analysis').returncode, 0)
        self.env.update(PREPARE_RESULT='success', RUNNER_ENVIRONMENT='github-hosted', RUNNER_OS='Linux')
        self.assertEqual(self.execute('Require prepared analysis').returncode, 0)
        self.assertEqual(self.execute('Require disposable Linux runner').returncode, 0)
        self.env['RUNNER_ENVIRONMENT'] = 'self-hosted'
        self.assertNotEqual(self.execute('Require disposable Linux runner').returncode, 0)

    def test_job_structure_preserves_the_credential_boundary_and_immutable_actions(self):
        """Static orchestration assertions complement executable guards without imitating their logic."""
        preparation = SOURCE.split('  prepare:\n', 1)[1].split('  sonarqube:\n', 1)[0]
        scan = SOURCE.split('  sonarqube:\n', 1)[1]
        self.assertNotIn('secrets.SONAR_TOKEN', preparation)
        self.assertNotIn('pre-sonar-command', scan)
        self.assertNotIn('npm ci', scan)
        self.assertIn('if: ${{ !cancelled() }}', scan)
        self.assertIn('!cancelled() && steps.sonar-ce-wait.outputs.analysis_id', scan)
        self.assertEqual(len(re.findall(r'uses: SonarSource/sonarqube-scan-action@[0-9a-f]{40}', scan)), 4)
        self.assertEqual(scan.count('-Dsonar.javascript.node.maxspace=${{ inputs.sonar-node-max-space }}'), 4)
        self.assertEqual(SOURCE.count('persist-credentials: false'), 4)
        self.assertTrue(all(re.search(r'@[0-9a-f]{40}(?:\s|$)', line) for line in SOURCE.splitlines() if 'uses: ' in line))

    def test_packaging_round_trip_transfers_only_reports_and_dependency_data(self):
        """Actual preparation output must preserve analysis inputs without bundling arbitrary code/secrets."""
        for name, data in [('coverage/lcov.info', 'SF:' + str(self.workspace) + '/src/a.ts\n'),
                           ('src/new.ts', 'throw new Error("never execute");'), ('secret.json', '{"private":"fixture"}'),
                           ('tracked.xml', 'tracked fixture'), ('node_modules/pkg/index.d.ts', 'export {};')]:
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data)
        # Git is only asked for tracked names; this fixture exposes the exact documented -z boundary.
        (self.bin / 'git').write_text('#!/bin/sh\nprintf "sonar-project.properties\\0tracked.xml\\0"\n')
        (self.bin / 'git').chmod(0o755)
        result = self.execute('Package analysis data')
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.runner / 'sonar-handoff/inputs.tar.gz') as archive:
            self.assertEqual(archive.getnames(), ['coverage/lcov.info'])
        with tarfile.open(self.runner / 'sonar-handoff/modules.tar.gz') as archive:
            self.assertIn('node_modules/pkg/index.d.ts', archive.getnames())
        analysis = self.root / 'analysis'
        analysis.mkdir()
        (analysis / 'sonar-project.properties').write_text(self.settings.read_text())
        self.workspace = analysis
        self.assertEqual(self.execute('Unpack validated analysis data').returncode, 0)
        self.assertEqual((analysis / 'coverage/lcov.info').read_text(), f'SF:{analysis}/src/a.ts\n')
        self.assertFalse((analysis / 'src/new.ts').exists())
        self.assertFalse((analysis / 'secret.json').exists())

    def test_preparation_materializes_hardlinked_files_without_following_dependency_symlinks(self):
        """pnpm's shared inodes must transfer as regular bytes; hostile archive links stay rejected."""
        modules = self.workspace / 'node_modules'
        modules.mkdir()
        original = modules / 'original.d.ts'
        original.write_text('export interface Reviewed {}')
        os.link(original, modules / 'same-inode.d.ts')
        (modules / 'alias.d.ts').symlink_to('original.d.ts')
        (self.bin / 'git').write_text('#!/bin/sh\nprintf "sonar-project.properties\\0"\n')
        (self.bin / 'git').chmod(0o755)
        self.assertEqual(self.execute('Package analysis data').returncode, 0)
        with tarfile.open(self.runner / 'sonar-handoff/modules.tar.gz') as archive:
            self.assertTrue(archive.getmember('node_modules/original.d.ts').isfile())
            self.assertTrue(archive.getmember('node_modules/same-inode.d.ts').isfile())
            self.assertTrue(archive.getmember('node_modules/alias.d.ts').issym())
        analysis = self.root / 'analysis'
        analysis.mkdir()
        (analysis / 'sonar-project.properties').write_text(self.settings.read_text())
        self.workspace = analysis
        result = self.execute('Unpack validated analysis data')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((analysis / 'node_modules/same-inode.d.ts').read_text(), 'export interface Reviewed {}')

    def test_placed_dependency_links_cannot_escape_through_checked_out_links(self):
        """A link safe inside staging can become unsafe after placement; check the final real target."""
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'sentinel').write_text('untouched')
        (self.workspace / 'vendor').symlink_to(outside, target_is_directory=True)
        self.archive('inputs.tar.gz', [])
        self.archive('modules.tar.gz', [('node_modules/pkg', 'link', '../vendor')])
        self.assertNotEqual(self.execute('Unpack validated analysis data').returncode, 0)
        self.assertEqual((outside / 'sentinel').read_text(), 'untouched')

    def test_reports_cannot_write_through_dangling_checked_out_links(self):
        """A nonexistent external target still escapes if a dangling destination link is followed."""
        outside = self.root / 'outside'
        outside.mkdir()
        coverage = self.workspace / 'coverage'
        coverage.mkdir()
        leaked = outside / 'leaked.info'
        (coverage / 'lcov.info').symlink_to(leaked)
        self.archive('inputs.tar.gz', [('coverage/lcov.info', 'file', 'untrusted report')])
        self.archive('modules.tar.gz', [])
        self.assertNotEqual(self.execute('Unpack validated analysis data').returncode, 0)
        self.assertFalse(leaked.exists())

    def test_missing_task_metadata_fails_and_hotspot_diagnostics_name_locations(self):
        """Absence cannot count as processed analysis, while read-only diagnostics retain actionable code locations."""
        self.assertNotEqual(self.execute('Wait for SonarQube background task').returncode, 0)
        self.env['ANALYSIS_ID'] = 'synthetic-analysis'
        self.fake_api([{'projectStatus': {'status': 'ERROR', 'conditions': [{'metricKey': 'new_security_hotspots_reviewed', 'status': 'ERROR'}]}},
                       {'hotspots': [{'component': 'fixture:src/a.ts', 'line': 7, 'vulnerabilityProbability': 'LOW', 'message': 'Review this'}]}])
        result = self.execute('SonarQube Quality Gate Diagnostics')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('fixture:src/a.ts:7', Path(self.env['GITHUB_STEP_SUMMARY']).read_text())


if __name__ == '__main__':
    unittest.main()
