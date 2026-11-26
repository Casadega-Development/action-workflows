"""Exercise the actual reusable-workflow scripts against offline API and hostile data fixtures."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/sonar-scan.yml'
SOURCE = WORKFLOW.read_text()


def shell(name, source=SOURCE):
    """Extract a literal named step, preserving its real shell/Python rather than copying logic."""
    section = source.split('      - name: ' + name + '\n', 1)[1]
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

    def execute(self, name, timeout=10, source=SOURCE):
        """Run the checked-in step with literal fixture values and a bounded local subprocess."""
        return subprocess.run(['bash', '-c', shell(name, source)], cwd=self.workspace, env=self.env, text=True,
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
                self.assertEqual([arg for arg in recorded[0] if arg.startswith('project')], ['project=fixture'])
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

    def test_single_job_keeps_identity_gate_and_pinned_scan_actions(self):
        """All steps share one checkout while authority is selected before project code runs."""
        self.assertIn('  sonarqube:\n', SOURCE)
        self.assertNotIn('  prepare:\n', SOURCE)
        self.assertNotIn('  identity:\n', SOURCE)
        self.assertLess(SOURCE.index('Resolve committed SonarQube identity'),
                        SOURCE.index('Resolve Node dependency installation'))
        self.assertLess(SOURCE.index('Install locked pnpm dependencies'),
                        SOURCE.index('SonarQube Scan (branch)'))
        self.assertNotIn('sonar-handoff', SOURCE)
        self.assertIn("!cancelled() && steps.sonar-ce-wait.outputs.analysis_id", SOURCE)
        self.assertEqual(len(re.findall(r'uses: SonarSource/sonarqube-scan-action@[0-9a-f]{40}', SOURCE)), 4)
        self.assertEqual(SOURCE.count('-Dsonar.javascript.node.maxspace=${{ inputs.sonar-node-max-space }}'), 4)
        self.assertEqual(SOURCE.count('persist-credentials: false'), 2)
        self.assertTrue(all(re.search(r'@[0-9a-f]{40}(?:\s|$)', line)
                            for line in SOURCE.splitlines() if 'uses: ' in line))

    def test_prerequisite_and_runner_policy_fail_closed(self):
        """Do not scan when the caller reports failure or a non-hosted runner is selected."""
        self.env.update(PREREQUISITE_RESULT='success', NODE_MAX_SPACE='4096',
                        RUNNER_ENVIRONMENT='github-hosted', RUNNER_OS='Linux')
        self.assertEqual(self.execute('Require successful prerequisite').returncode, 0)
        self.assertEqual(self.execute('Require disposable Linux runner').returncode, 0)
        self.env['PREREQUISITE_RESULT'] = 'failure'
        self.assertNotEqual(self.execute('Require successful prerequisite').returncode, 0)
        self.env['RUNNER_ENVIRONMENT'] = 'self-hosted'
        self.assertNotEqual(self.execute('Require disposable Linux runner').returncode, 0)

    def test_missing_task_metadata_fails_and_hotspot_diagnostics_name_locations(self):
        """Absence cannot count as processed analysis, while read-only diagnostics retain actionable code locations."""
        self.assertNotEqual(self.execute('Wait for SonarQube background task').returncode, 0)
        self.env['ANALYSIS_ID'] = 'synthetic-analysis'
        self.fake_api([{'projectStatus': {'status': 'ERROR', 'conditions': [{'metricKey': 'new_security_hotspots_reviewed', 'status': 'ERROR'}]}},
                       {'hotspots': [{'component': 'fixture:src/a.ts', 'line': 7, 'vulnerabilityProbability': 'LOW', 'message': 'Review this'}]}])
        result = self.execute('SonarQube Quality Gate Diagnostics')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('fixture:src/a.ts:7', Path(self.env['GITHUB_STEP_SUMMARY']).read_text())
        request = json.loads(Path(self.env['CALLS']).read_text().splitlines()[1])
        self.assertEqual([arg for arg in request if arg.startswith('project')], ['project=fixture'])

    def test_scanner_launcher_and_engine_use_the_verified_store(self):
        """Run each real setup script and reject unreadable, empty, and compromised stores."""
        java_home = self.root / 'pinned scanner java'
        java_bin = java_home / 'bin'
        store = java_home / 'lib/security/cacerts'
        java_bin.mkdir(parents=True)
        store.parent.mkdir(parents=True)
        for tool, script in (('java', 'exit 0'), ('keytool', 'printf "%s\\n" "$FIXTURE_CERTS"')):
            executable = java_bin / tool
            executable.write_text('#!/bin/sh\n' + script + '\n')
            executable.chmod(0o755)
        output = self.root / 'scanner-env'
        self.env.update(JAVA_HOME=str(java_home), GITHUB_ENV=str(output), JAVA_TOOL_OPTIONS='-Dlauncher=keep',
                        SONAR_SCANNER_JAVA_OPTS='-Xmx2g -Dengine=keep')
        blocked_fingerprints = (
            '86:A1:EC:BA:08:9C:4A:8D:3B:BE:27:34:C6:12:BA:34:1D:81:3E:04:3C:F9:E8:A8:62:CD:5C:57:A3:6B:BE:6B',
            'D7:A7:A0:FB:5D:7E:27:31:D7:71:E9:48:4E:BC:DE:F7:1D:5F:0C:3E:0A:29:48:78:2B:C8:3E:E0:EA:69:9E:F4',
        )
        dotnet = (WORKFLOW.parent / 'sonar-dotnet.yml').read_text()
        # Reusable workflows execute from the caller checkout, so keep these
        # inline scanner guards identical without a second checkout.
        self.assertEqual(shell('Configure Sonar scanner JVM trust'), shell('Configure Sonar scanner JVM trust', dotnet))
        for source in (SOURCE, dotnet):
            store.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            self.assertNotEqual(self.execute('Configure Sonar scanner JVM trust', source=source).returncode, 0)
            store.write_text('fixture store')
            for fingerprints in ('no certificates', *('SHA256: ' + blocked for blocked in blocked_fingerprints)):
                self.env['FIXTURE_CERTS'] = fingerprints
                self.assertNotEqual(self.execute('Configure Sonar scanner JVM trust', source=source).returncode, 0)
                self.assertFalse(output.exists())
            self.env['FIXTURE_CERTS'] = 'SHA256: AA:BB:CC'
            result = self.execute('Configure Sonar scanner JVM trust', source=source)
            self.assertEqual(result.returncode, 0, result.stderr)
            flags = f'-Djavax.net.ssl.trustStore="{store}" -XX:-UseBASE64Intrinsics'
            self.assertEqual(dict(line.split('=', 1) for line in output.read_text().splitlines()), {
                'JAVA_TOOL_OPTIONS': '-Dlauncher=keep ' + flags,
                'SONAR_SCANNER_JAVA_OPTS': '-Xmx2g -Dengine=keep ' + flags,
                'SONAR_SCANNER_SKIP_JRE_PROVISIONING': 'true',
                'SONAR_SCANNER_JAVA_EXE_PATH': str(java_bin / 'java'),
            })
        self.assertGreater(SOURCE.index('Configure Sonar scanner JVM trust'), SOURCE.index('Validate Java analysis inputs'))
        self.assertEqual(SOURCE.count('Configure Sonar scanner JVM trust'), 1)

    def rescan(self, run_event='pull_request', recorded=None, related=None, invocation='workflow_dispatch'):
        """Run the real rescan resolver against a synthetic run and paginated commit associations."""
        output = self.root / 'rescan-output'
        output.write_text('')
        calls = self.root / 'rescan-calls'
        calls.write_text('')
        self.env.update(EVENT_NAME=invocation, INPUT_TEST_RUN_ID='99', WORKFLOW_RUN_ID='99',
                        GITHUB_REPOSITORY='example/project', GITHUB_OUTPUT=str(output), CALLS=str(calls),
                        RUN_PAYLOAD=json.dumps({'event': run_event, 'head_branch': 'fix/example', 'head_sha': 'a' * 40,
                                                'head_repository': {'id': 13}, 'repository': {'id': 13},
                                                'pull_requests': recorded or []}),
                        COMMIT_PRS=json.dumps(related or [[]]))
        gh = self.bin / 'gh'
        gh.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import os, sys
            from pathlib import Path
            route = sys.argv[-1]
            with Path(os.environ['CALLS']).open('a') as output:
                print(route, file=output)
            if sys.argv[1:] == ['api', 'repos/example/project/actions/runs/99']:
                print(os.environ['RUN_PAYLOAD'])
            elif sys.argv[1:4] == ['api', '--paginate', '--slurp'] and route == (
                'repos/example/project/commits/' + 'a' * 40 + '/pulls?per_page=100'
            ):
                print(os.environ['COMMIT_PRS'])
            else:
                sys.exit('Unexpected GitHub route: ' + route)
        '''))
        gh.chmod(0o755)
        result = self.execute('Resolve from workflow_run or test-run-id',
                              source=(WORKFLOW.parent / 'sonar-scan-after-tests.yml').read_text())
        return result, dict(line.split('=', 1) for line in output.read_text().splitlines()), calls.read_text().splitlines()

    @staticmethod
    def pull(number, source=13, target=13):
        """Build matching and misleading associations without relying on names alone."""
        return {'number': number, 'head': {'ref': 'fix/example', 'repo': {'id': source}},
                'base': {'ref': 'main', 'repo': {'id': target}}}

    def test_rescan_recovers_the_merged_pull_request_by_tested_sha(self):
        """An empty run association must resolve the original PR and reuse its exact tested commit."""
        result, outputs, calls = self.rescan(related=[[self.pull(30, source=14)],
                                                      [self.pull(28), self.pull(31, target=14)]])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, {'coverage-run-id': '99', 'checkout-ref': 'a' * 40, 'scm-revision': 'a' * 40,
                                   'use-pull-request-scan': 'true', 'pull-request-key': '28',
                                   'pull-request-branch': 'fix/example', 'pull-request-base': 'main'})
        self.assertEqual(len(calls), 2)

    def test_rescan_uses_unambiguous_run_metadata_and_preserves_branch_runs(self):
        """Authoritative run metadata requires no extra lookup; a real branch remains branch-scoped."""
        result, outputs, calls = self.rescan(recorded=[self.pull(28)], invocation='workflow_run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['pull-request-key'], '28')
        self.assertEqual(len(calls), 1)
        result, outputs, calls = self.rescan(run_event='push')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['branch-name'], 'fix/example')
        self.assertEqual(outputs['use-pull-request-scan'], 'false')
        self.assertEqual(len(calls), 1)

    def test_unresolved_or_ambiguous_pr_never_produces_branch_scan_inputs(self):
        """Partial or conflicting PR evidence must stop the scanner without publishing any outputs."""
        for related in ([[]], [[self.pull(28), self.pull(29)]]):
            with self.subTest(related=related):
                result, outputs, _ = self.rescan(related=related)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('No unique pull request matches the tested commit', result.stdout)
                self.assertEqual(outputs, {})
        result, outputs, _ = self.rescan(run_event='pull_request_target')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})


if __name__ == '__main__':
    unittest.main()
