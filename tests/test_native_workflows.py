"""Exercise opt-in native workflow contracts with actual embedded code and synthetic processes."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import test_sonar

ROOT = Path(__file__).resolve().parents[1]
DOTNET = (ROOT / '.github/workflows/sonar-dotnet.yml').read_text()
GATE = (ROOT / '.github/workflows/required-checks.yml').read_text()


def step(source, name):
    """Extract the literal executable step without implementing a second copy of its behavior."""
    block = source.split('      - name: ' + name + '\n', 1)[1].split('        run: |\n', 1)[1]
    lines = []
    for line in block.splitlines():
        if line and not line.startswith('          '):
            break
        lines.append(line[10:] if line else '')
    return '\n'.join(lines)


class JavaArtifacts(unittest.TestCase):
    """Use the generic workflow's independent packaging/extraction boundary for Java data."""

    def setUp(self):
        """Reuse the synthetic filesystem harness without inheriting and rerunning its test methods."""
        self.fixture = test_sonar.WorkflowScripts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.env.update(JAVA_VERSION='17.0.12+7', JAVA_HOME='/opt/jdk-fixture',
                                JAVA_ARTIFACTS_JSON=json.dumps([{'path': 'java/build/classes', 'kind': 'binaries'},
                                                               {'path': 'java/build/libs', 'kind': 'libraries'}]))
        (self.fixture.bin / 'git').write_text('#!/bin/sh\nprintf "sonar-project.properties\\0"\n')
        (self.fixture.bin / 'git').chmod(0o755)

    def create_outputs(self):
        """Model actual compiler output alongside source and non-bytecode data that must stay behind."""
        for name, content in [('java/build/classes/com/Example.class', 'compiled fixture'),
                              ('java/build/libs/dependency.jar', 'library fixture'),
                              ('java/build/classes/private.txt', 'do not transfer'),
                              ('java/Example.java', 'class Example {}')]:
            target = self.fixture.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def test_java_round_trip_preserves_only_declared_bytecode_and_scanner_scope(self):
        """The public artifact contract and JDK must produce exact scanner configuration."""
        fixture = self.fixture
        self.create_outputs()
        self.assertEqual(fixture.execute('Package analysis data').returncode, 0)
        analysis = fixture.root / 'analysis'
        analysis.mkdir()
        (analysis / 'sonar-project.properties').write_text(fixture.settings.read_text())
        fixture.workspace = analysis
        result = fixture.execute('Unpack validated analysis data')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((analysis / 'java/build/classes/com/Example.class').read_text(), 'compiled fixture')
        self.assertEqual((analysis / 'java/build/libs/dependency.jar').read_text(), 'library fixture')
        self.assertFalse((analysis / 'java/build/classes/private.txt').exists())
        self.assertFalse((analysis / 'java/Example.java').exists())
        self.assertEqual((analysis / 'sonar-project.coverage.generated.properties').read_text(),
                         '\nsonar.java.binaries=java/build/classes\nsonar.java.libraries=java/build/libs/**/*.jar'
                         '\nsonar.java.source=17\nsonar.java.jdkHome=/opt/jdk-fixture\n')
        self.assertEqual(fixture.execute('Apply and validate scanner settings').returncode, 0)

    def test_real_compiled_java_survives_the_data_handoff(self):
        """Compile and execute an independent assertion before and after transferring actual bytecode."""
        if not all(shutil.which(tool) for tool in ('javac', 'java', 'jar')):
            self.skipTest('The optional native smoke test requires an installed JDK.')
        fixture = self.fixture
        source = fixture.workspace / 'Example.java'
        source.write_text('public final class Example { public static void main(String[] args) { if (Integer.parseInt("6") * 7 != 42) throw new AssertionError(); System.out.println("fixture passed"); } }')
        classes = fixture.workspace / 'java/build/classes'
        libraries = fixture.workspace / 'java/build/libs'
        classes.mkdir(parents=True)
        libraries.mkdir(parents=True)
        subprocess.run(['javac', '--release', '17', '-d', str(classes), str(source)], check=True, capture_output=True)
        subprocess.run(['jar', '--create', '--file', str(libraries / 'fixture.jar'), '-C', str(classes), '.'], check=True, capture_output=True)
        expected = (classes / 'Example.class').read_bytes()
        self.assertEqual(expected[:4], bytes.fromhex('cafebabe'))
        self.assertEqual(fixture.execute('Package analysis data').returncode, 0)
        analysis = fixture.root / 'analysis'
        analysis.mkdir()
        (analysis / 'sonar-project.properties').write_text(fixture.settings.read_text())
        fixture.workspace = analysis
        result = fixture.execute('Unpack validated analysis data')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((analysis / 'java/build/classes/Example.class').read_bytes(), expected)
        result = subprocess.run(['java', '-cp', str(analysis / 'java/build/classes'), 'Example'], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'fixture passed')

    def test_java_contract_rejects_missing_unsafe_overlapping_and_unpinned_inputs(self):
        """Every malformed declaration fails on both sides before analysis can silently omit Java."""
        fixture = self.fixture
        for value in ('[]', '{}', '[{"path":"../escape","kind":"binaries"}]',
                      '[{"path":"java/*","kind":"binaries"}]',
                      '[{"path":"java","kind":"libraries"}]',
                      '[{"path":"java","kind":"binaries"},{"path":"java/child","kind":"binaries"}]',
                      '[{"path":"java/classes","kind":"binaries","extra":true}]'):
            with self.subTest(value=value):
                fixture.env['JAVA_ARTIFACTS_JSON'] = value
                self.assertNotEqual(fixture.execute('Package analysis data').returncode, 0)
                self.assertNotEqual(fixture.execute('Unpack validated analysis data').returncode, 0)
        fixture.env.update(JAVA_ARTIFACTS_JSON='[{"path":"java/classes","kind":"binaries"}]', JAVA_VERSION='17')
        self.assertNotEqual(fixture.execute('Package analysis data').returncode, 0)
        fixture.env['JAVA_VERSION'] = '17.0.12+7'
        self.assertNotEqual(fixture.execute('Package analysis data').returncode, 0)

    def test_java_extraction_rejects_wrong_types_missing_roots_and_archive_links(self):
        """Independent extraction cannot trust a malicious builder's wider or incomplete archive."""
        fixture = self.fixture
        fixture.env['JAVA_ARTIFACTS_JSON'] = '[{"path":"java/build/classes","kind":"binaries"}]'
        fixture.archive('inputs.tar.gz', [])
        fixture.archive('modules.tar.gz', [])
        for member in [('java/build/classes/Bad.java', 'file', 'source'),
                       ('other/Bad.class', 'file', 'outside allowlist'),
                       ('java/build/classes/Bad.class', 'link', 'elsewhere'),
                       ('java/build/classes/Bad.class', 'hardlink', 'sonar-project.properties')]:
            with self.subTest(member=member):
                fixture.archive('java.tar.gz', [member])
                self.assertNotEqual(fixture.execute('Unpack validated analysis data').returncode, 0)
        fixture.archive('java.tar.gz', [])
        self.assertNotEqual(fixture.execute('Unpack validated analysis data').returncode, 0)

    def test_java_sources_and_checked_out_links_cannot_be_overwritten(self):
        """The destination checkout remains authoritative even for an otherwise allowed bytecode path."""
        fixture = self.fixture
        fixture.env['JAVA_ARTIFACTS_JSON'] = '[{"path":"java/build/classes","kind":"binaries"}]'
        fixture.archive('inputs.tar.gz', [])
        fixture.archive('modules.tar.gz', [])
        fixture.archive('java.tar.gz', [('java/build/classes/Example.class', 'file', 'compiled')])
        destination = fixture.workspace / 'java/build/classes'
        destination.mkdir(parents=True)
        sentinel = destination / 'Example.class'
        sentinel.write_text('tracked original')
        self.assertNotEqual(fixture.execute('Unpack validated analysis data').returncode, 0)
        self.assertEqual(sentinel.read_text(), 'tracked original')
        shutil.rmtree(destination)
        outside = fixture.root / 'outside'
        outside.mkdir()
        destination.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(fixture.execute('Unpack validated analysis data').returncode, 0)
        self.assertFalse((outside / 'Example.class').exists())

    def test_java_packaging_refuses_symlinks_and_empty_bytecode(self):
        """Build output must be materialized data, and a declared root must actually contain it."""
        fixture = self.fixture
        fixture.env['JAVA_ARTIFACTS_JSON'] = '[{"path":"java/build/classes","kind":"binaries"}]'
        classes = fixture.workspace / 'java/build/classes'
        classes.mkdir(parents=True)
        self.assertNotEqual(fixture.execute('Package analysis data').returncode, 0)
        shutil.rmtree(fixture.runner / 'sonar-handoff')
        (classes / 'bad.class').symlink_to(fixture.settings)
        self.assertNotEqual(fixture.execute('Package analysis data').returncode, 0)


class NativeWorkflowScripts(unittest.TestCase):
    """Verify trusted event selection and native command contracts without network or credentials."""

    def setUp(self):
        """Allocate an isolated caller checkout with synthetic pins, identity, and result paths."""
        self.temporary = tempfile.TemporaryDirectory(prefix='casadega-native-workflow-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.runner = self.root / 'runner'
        self.bin = self.root / 'bin'
        for directory in (self.workspace, self.runner, self.bin):
            directory.mkdir()
        self.env = {**os.environ, 'RUNNER_TEMP': str(self.runner), 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'EVENT_NAME': 'push', 'EVENT_REF': 'refs/heads/main', 'DEFAULT_BRANCH': 'main',
                    'EVENT_REVISION': 'a' * 40, 'SOURCE_REVISION': 'a' * 40, 'PREREQUISITE_RESULT': 'success',
                    'REPOSITORY': 'quantca/example', 'PR_STATE': '', 'PR_NUMBER': '', 'PR_HEAD_REPOSITORY': '',
                    'PR_BASE_REPOSITORY': '', 'PR_HEAD_REVISION': '', 'PR_HEAD_BRANCH': '',
                    'PR_BASE_BRANCH': '', 'PR_BASE_REVISION': '', 'USE_PULL_REQUEST_SCAN': 'false',
                    'SONAR_PULL_REQUEST': '', 'SONAR_PULL_REQUEST_BRANCH': '', 'SONAR_PULL_REQUEST_BASE': '',
                    'SONAR_BRANCH_NAME': 'main', 'SONAR_HOST_URL': 'https://sonar.example.test',
                    'SONAR_PROJECT_KEY': 'fixture', 'GITHUB_STEP_SUMMARY': str(self.root / 'summary'),
                    'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'Linux', 'PROJECT_DIRECTORY': '.',
                    'PROJECT_FILE': 'App.csproj', 'TEST_PROJECTS_JSON': '["Tests/Tests.csproj"]',
                    'BUILD_CONFIGURATION': 'Release', 'ISSUE_GATE_SCOPE': 'all-open',
                    'GITHUB_OUTPUT': str(self.root / 'output'), 'SONAR_TOKEN': 'synthetic-token'}
        (self.workspace / '.config').mkdir()
        (self.workspace / '.config/dotnet-tools.json').write_text(json.dumps({'version': 1, 'isRoot': True, 'tools': {
            'dotnet-sonarscanner': {'version': '10.4.1', 'commands': ['dotnet-sonarscanner']}}}))
        (self.workspace / 'global.json').write_text('{"sdk":{"version":"10.0.100","rollForward":"disable"}}')
        (self.workspace / 'App.csproj').write_text('<Project/>')
        (self.workspace / 'Tests').mkdir()
        (self.workspace / 'Tests/Tests.csproj').write_text('<Project/>')
        (self.workspace / 'sonar-project.properties').write_text('sonar.host.url=https://sonar.example.test\nsonar.projectKey=fixture\n')
        self.assertEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)

    def execute(self, source, name):
        """Execute the checked-in shell using controlled environment values and bounded runtime."""
        return subprocess.run(['bash', '-c', step(source, name)], cwd=self.workspace, env=self.env,
                              capture_output=True, text=True, timeout=10)

    def invocation(self):
        """Capture only this invocation's actual outputs so stale successful evidence cannot mask failure."""
        output = Path(self.env['GITHUB_OUTPUT'])
        output.write_text('')
        result = self.execute(DOTNET, 'Require trusted invocation')
        return result, dict(line.split('=', 1) for line in output.read_text().splitlines())

    def pull_request(self):
        """Supply independent source, target, and merge revisions to expose incorrect checkout selection."""
        self.env.update(EVENT_NAME='pull_request', EVENT_REF='refs/pull/42/merge', EVENT_REVISION='d' * 40,
                        PR_STATE='open', PR_NUMBER='42', PR_HEAD_REPOSITORY='quantca/example',
                        PR_BASE_REPOSITORY='quantca/example', PR_HEAD_REVISION='b' * 40,
                        PR_HEAD_BRANCH='feature/risk-check', PR_BASE_BRANCH='main', PR_BASE_REVISION='c' * 40)

    def apply_invocation_environment(self, name, outputs):
        """Follow the real step's output references, allowing routing regressions to change observed argv/API calls."""
        block = DOTNET.split('      - name: ' + name + '\n', 1)[1].split('        run: |\n', 1)[0]
        references = re.findall(r'^          ([A-Z_]+): \$\{\{ steps\.invocation\.outputs\.([a-z-]+) \}\}$',
                                block, re.MULTILINE)
        for variable, output in references:
            self.env[variable] = outputs[output]

    def test_dotnet_rejects_every_untrusted_event_ref_and_failed_prerequisite(self):
        """Baseline analysis retains its default-branch restriction; unsupported events and missing evidence fail."""
        for event in ('push', 'workflow_dispatch'):
            self.env['EVENT_NAME'] = event
            result, outputs = self.invocation()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(outputs, {'revision': 'a' * 40, 'use-pull-request-scan': 'false',
                                       'pull-request-key': '', 'pull-request-branch': '', 'pull-request-base': '',
                                       'branch-name': 'main', 'base-revision': ''})
        for key, values in {'EVENT_NAME': ('pull_request', 'pull_request_target', 'workflow_run', 'schedule'),
                            'EVENT_REF': ('refs/heads/feature', 'refs/tags/main', ''),
                            'EVENT_REVISION': ('main', '', 'a' * 39),
                            'PREREQUISITE_RESULT': ('failure', 'cancelled', 'skipped', ''),
                            'RUNNER_ENVIRONMENT': ('self-hosted',), 'RUNNER_OS': ('Windows', 'macOS', '')}.items():
            original = self.env[key]
            for value in values:
                with self.subTest(key=key, value=value):
                    self.env[key] = value
                    result, outputs = self.invocation()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(outputs, {})
            self.env[key] = original

    def test_same_repository_pr_selects_source_revision_and_exact_sonar_scope(self):
        """A trusted PR analyzes its source commit, not the synthetic merge or target commit."""
        self.pull_request()
        result, outputs = self.invocation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, {'revision': 'b' * 40, 'use-pull-request-scan': 'true',
                                   'pull-request-key': '42', 'pull-request-branch': 'feature/risk-check',
                                   'pull-request-base': 'main', 'branch-name': '', 'base-revision': 'c' * 40})
        checkout = DOTNET.split('      - uses: actions/checkout@', 1)[1].split('      - name:', 1)[0]
        self.assertIn('ref: ${{ steps.invocation.outputs.revision }}', checkout)
        self.assertIn('fetch-depth: 0', checkout)
        self.assertNotIn('${{ github.sha }}', checkout)

    def test_pr_rejects_forks_incomplete_metadata_and_changed_scope(self):
        """Repository identity, open status, refs, revisions, and prerequisite success all precede credential use."""
        self.pull_request()
        invalid = {
            'EVENT_NAME': ('pull_request_target', 'workflow_run'),
            'EVENT_REF': ('refs/pull/41/merge', 'refs/heads/main', 'refs/pull/42/head', ''),
            'REPOSITORY': ('', 'elsewhere/example'),
            'PR_STATE': ('closed', ''),
            'PR_NUMBER': ('', '0', '-1', '42\nbranch-name=evil'),
            'PR_HEAD_REPOSITORY': ('fork/example', ''),
            'PR_BASE_REPOSITORY': ('elsewhere/example', ''),
            'PR_HEAD_REVISION': ('', 'feature', 'b' * 39, 'b' * 40 + '\n'),
            'PR_BASE_REVISION': ('', 'main', 'c' * 39),
            'PR_HEAD_BRANCH': ('', '../feature', 'feature\nbranch-name=evil'),
            'PR_BASE_BRANCH': ('', '../main', 'main\nbranch-name=evil'),
            'PREREQUISITE_RESULT': ('failure', 'cancelled', 'skipped', ''),
            'RUNNER_ENVIRONMENT': ('self-hosted',),
            'RUNNER_OS': ('Windows', 'macOS', ''),
        }
        for key, values in invalid.items():
            original = self.env[key]
            for value in values:
                with self.subTest(key=key, value=value):
                    self.env[key] = value
                    result, outputs = self.invocation()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(outputs, {})
            self.env[key] = original

    def test_dotnet_configuration_refuses_unpinned_versions_empty_tests_and_escape(self):
        """Invalid project contracts fail before restoring packages or supplying the scanner token."""
        for key, values in {'PROJECT_FILE': ('../outside.csproj', '/tmp/outside.csproj', '-p:Bad.csproj'),
                            'PROJECT_DIRECTORY': ('../', '/tmp'), 'TEST_PROJECTS_JSON': ('[]', '{}', '["missing.csproj"]'),
                            'BUILD_CONFIGURATION': ('Release; echo bad',), 'ISSUE_GATE_SCOPE': ('unknown',)}.items():
            original = self.env[key]
            for value in values:
                with self.subTest(key=key, value=value):
                    self.env[key] = value
                    self.assertNotEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)
            self.env[key] = original
        (self.workspace / 'global.json').write_text('{"sdk":{"version":"10.0.100","rollForward":"latestMajor"}}')
        self.assertNotEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)

    def fake_dotnet(self):
        """Record the real argument arrays and manufacture independent test/coverage report outcomes."""
        self.env['CALLS'] = str(self.root / 'calls')
        executable = self.bin / 'dotnet'
        executable.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import json, os, sys
            from pathlib import Path
            arguments = sys.argv[1:]
            with Path(os.environ['CALLS']).open('a') as stream:
                stream.write(json.dumps(arguments) + '\\n')
            if arguments == ['--version']:
                print('10.0.100')
            if arguments[0] == 'test':
                path = Path(arguments[arguments.index('--results-directory') + 1])
                path.mkdir(parents=True)
                mode = os.environ.get('TEST_MODE', 'passed')
                if mode != 'missing':
                    executed = '0' if mode == 'empty' else '3'
                    failed = '1' if mode == 'failed' else '0'
                    (path / 'results.trx').write_text('<TestRun><ResultSummary><Counters executed="' + executed + '" failed="' + failed + '"/></ResultSummary></TestRun>')
                    sequence_points = '0' if mode == 'no-coverage' else '4'
                    (path / 'coverage.opencover.xml').write_text('<CoverageSession><Summary numSequencePoints="' + sequence_points + '"/></CoverageSession>')
        '''))
        executable.chmod(0o755)

    def test_locked_restore_refuses_new_untracked_locks_and_accepts_existing_graph(self):
        """Real locked restore can bootstrap files; the independent tracked inventory must catch it."""
        self.fake_dotnet()
        git = self.bin / 'git'
        git.write_text('#!/bin/sh\nif [ "$1" = ls-files ]; then printf "Tests/packages.lock.json\\0"; fi\n')
        git.chmod(0o755)
        result = self.execute(DOTNET, 'Restore the pinned scanner and build graph')
        self.assertNotEqual(result.returncode, 0)
        (self.workspace / 'new').mkdir()
        (self.workspace / 'new/packages.lock.json').write_text('{}')
        result = self.execute(DOTNET, 'Restore the pinned scanner and build graph')
        self.assertNotEqual(result.returncode, 0)
        (self.workspace / 'new/packages.lock.json').unlink()
        (self.workspace / 'Tests/packages.lock.json').write_text('{}')
        result = self.execute(DOTNET, 'Restore the pinned scanner and build graph')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in Path(self.env['CALLS']).read_text().splitlines()]
        self.assertIn(['restore', str(self.workspace / 'App.csproj'), '--locked-mode', '-p:RestorePackagesWithLockFile=true', '-p:Configuration=Release'], calls)

    def test_preexisting_state_and_unrecognized_settings_fail_before_analysis(self):
        """A clean checkout and translated settings are required; unsupported options cannot be ignored."""
        state = self.workspace / '.sonarqube'
        state.mkdir()
        self.assertNotEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)
        state.rmdir()
        settings = self.workspace / 'sonar-project.properties'
        settings.write_text(settings.read_text() + 'sonar.nodejs.executable=./payload\n')
        self.assertNotEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)

    def test_dotnet_build_test_contract_requires_actual_tests_and_instrumented_coverage(self):
        """Zero exit from dotnet is insufficient without fresh populated reports for every project."""
        self.fake_dotnet()
        for mode in ('missing', 'empty', 'failed', 'no-coverage', 'passed'):
            with self.subTest(mode=mode):
                shutil.rmtree(self.runner / 'sonar-dotnet-reports', ignore_errors=True)
                self.env['TEST_MODE'] = mode
                result = self.execute(DOTNET, 'Build and test the analysis graph')
                self.assertEqual(result.returncode == 0, mode == 'passed', result.stderr)
        calls = [json.loads(line) for line in Path(self.env['CALLS']).read_text().splitlines()]
        self.assertIn(['build', str(self.workspace / 'App.csproj'), '--no-restore', '--no-incremental',
                       '--disable-build-servers', '--configuration', 'Release'], calls)
        test = next(arguments for arguments in calls if arguments[0] == 'test')
        self.assertIn('DataCollectionRunSettings.DataCollectors.DataCollector.Configuration.Format=opencover', test)
        self.assertNotIn('synthetic-token', Path(self.env['CALLS']).read_text())

    def fake_scanner(self):
        """Record the scanner process boundary without supplying a real token or contacting any server."""
        scanner = self.runner / 'sonar-dotnet-tools/dotnet-sonarscanner'
        scanner.parent.mkdir()
        self.env['CALLS'] = str(self.root / 'scanner-calls')
        scanner.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import json, os, sys
            from pathlib import Path
            with Path(os.environ['CALLS']).open('a') as stream:
                stream.write(json.dumps(sys.argv[1:]) + '\\n')
            if sys.argv[1] == 'end':
                path = Path('.sonarqube/out/.sonar')
                path.mkdir(parents=True)
                (path / 'report-task.txt').write_text('ceTaskId=fixture-task\\nserverUrl=https://sonar.example.test\\n')
        '''))
        scanner.chmod(0o755)

    def test_dotnet_scanner_receives_explicit_identity_and_pin_without_token_logging(self):
        """Baseline begin/end retains branch analysis and literal argv without logging the synthetic credential."""
        self.fake_scanner()
        invocation, outputs = self.invocation()
        self.assertEqual(invocation.returncode, 0, invocation.stderr)
        self.apply_invocation_environment('Begin .NET analysis', outputs)
        for name in ('Begin .NET analysis', 'End .NET analysis'):
            result = self.execute(DOTNET, name)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('synthetic-token', result.stdout + result.stderr)
        calls = [json.loads(line) for line in Path(self.env['CALLS']).read_text().splitlines()]
        self.assertIn('/d:sonar.host.url=https://sonar.example.test', calls[0])
        self.assertIn('/d:sonar.branch.name=main', calls[0])
        self.assertIn('/d:sonar.scm.revision=' + 'a' * 40, calls[0])
        self.assertFalse(any(argument.startswith('/d:sonar.pullrequest.') for argument in calls[0]))
        self.assertIn('/d:sonar.scanner.scanAll=false', calls[0])
        self.assertIn('/d:sonar.token=synthetic-token', calls[0])
        self.assertEqual(calls[1], ['end', '/d:sonar.token=synthetic-token'])
        self.assertTrue((self.workspace / '.scannerwork/report-task.txt').is_file())

    def test_pr_scanner_receives_source_revision_and_exclusive_pull_request_parameters(self):
        """Validated PR metadata must reach the real scanner command, without a competing branch analysis."""
        self.fake_scanner()
        self.pull_request()
        invocation, outputs = self.invocation()
        self.assertEqual(invocation.returncode, 0, invocation.stderr)
        self.apply_invocation_environment('Begin .NET analysis', outputs)
        result = self.execute(DOTNET, 'Begin .NET analysis')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('synthetic-token', result.stdout + result.stderr)
        arguments = json.loads(Path(self.env['CALLS']).read_text().splitlines()[0])
        self.assertIn('/d:sonar.scm.revision=' + 'b' * 40, arguments)
        self.assertIn('/d:sonar.pullrequest.key=42', arguments)
        self.assertIn('/d:sonar.pullrequest.branch=feature/risk-check', arguments)
        self.assertIn('/d:sonar.pullrequest.base=main', arguments)
        self.assertFalse(any(argument.startswith('/d:sonar.branch.name=') for argument in arguments))
        self.assertNotIn('/d:sonar.scm.revision=' + 'd' * 40, arguments)

    def test_pr_sonar_identity_must_match_the_exact_target_revision(self):
        """A PR cannot redirect its scan token by changing the committed server or project identity."""
        self.pull_request()
        invocation, outputs = self.invocation()
        self.assertEqual(invocation.returncode, 0, invocation.stderr)
        self.apply_invocation_environment('Resolve .NET scan configuration', outputs)
        settings = self.workspace / 'sonar-project.properties'
        original = settings.read_text()
        self.env['BASE_PROPERTIES'] = original
        self.env['GIT_CALLS'] = str(self.root / 'identity-calls')
        git = self.bin / 'git'
        git.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import json, os, sys
            from pathlib import Path
            with Path(os.environ['GIT_CALLS']).open('a') as stream:
                stream.write(json.dumps(sys.argv[1:]) + '\\n')
            if sys.argv[1:] != ['show', 'c' * 40 + ':sonar-project.properties']:
                sys.exit(1)
            print(os.environ['BASE_PROPERTIES'])
        '''))
        git.chmod(0o755)
        result = self.execute(DOTNET, 'Resolve .NET scan configuration')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in Path(self.env['GIT_CALLS']).read_text().splitlines()]
        self.assertEqual(calls, [['show', 'c' * 40 + ':sonar-project.properties']])
        for replacement in (original.replace('https://sonar.example.test', 'https://attacker.example.test'),
                            original.replace('sonar.projectKey=fixture', 'sonar.projectKey=other')):
            with self.subTest(replacement=replacement):
                settings.write_text(replacement)
                self.assertNotEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)
        settings.write_text(original)
        for unavailable in ('', 'sonar.projectKey=fixture\n', original + 'sonar.projectKey=other\n'):
            self.env['BASE_PROPERTIES'] = unavailable
            self.assertNotEqual(self.execute(DOTNET, 'Resolve .NET scan configuration').returncode, 0)

    def test_native_issue_hotspot_and_diagnostic_requests_follow_the_analyzed_scope(self):
        """Follow actual workflow output wiring to prove PR gates never read an unrelated green main branch."""
        for pull_request in (False, True):
            if pull_request:
                self.pull_request()
            invocation, outputs = self.invocation()
            self.assertEqual(invocation.returncode, 0, invocation.stderr)
            expected = 'pullRequest=42' if pull_request else 'branch=main'
            forbidden_prefix = 'branch=' if pull_request else 'pullRequest='
            cases = [
                ('SonarQube Issue Gate', [{'total': 0, 'issues': []}]),
                ('SonarQube Hotspot Gate', [{'paging': {'total': 0}, 'hotspots': []}]),
                ('SonarQube Quality Gate Diagnostics', [
                    {'projectStatus': {'status': 'ERROR', 'conditions': [
                        {'metricKey': 'new_security_hotspots_reviewed', 'status': 'ERROR'}]}},
                    {'hotspots': [{'component': 'fixture:App.cs', 'line': 7,
                                   'vulnerabilityProbability': 'LOW', 'message': 'Review this'}]}]),
            ]
            for name, responses in cases:
                with self.subTest(pull_request=pull_request, step=name):
                    calls = self.root / 'calls'
                    calls.unlink(missing_ok=True)
                    self.env['ANALYSIS_ID'] = 'exact-analysis'
                    self.apply_invocation_environment(name, outputs)
                    # Reuse only the synthetic transport; execution still comes from the .NET workflow.
                    test_sonar.WorkflowScripts.fake_api(self, responses)
                    result = self.execute(DOTNET, name)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                    scoped = [arguments for arguments in recorded
                              if any('/issues/search' in value or '/hotspots/search' in value for value in arguments)]
                    self.assertTrue(scoped)
                    for arguments in scoped:
                        self.assertIn(expected, arguments)
                        self.assertFalse(any(value.startswith(forbidden_prefix) for value in arguments))
                    if name == 'SonarQube Quality Gate Diagnostics':
                        self.assertIn('https://sonar.example.test/api/qualitygates/project_status?analysisId=exact-analysis',
                                      recorded[0])

    def test_required_gate_rejects_missing_failed_cancelled_and_implicit_skips(self):
        """An explicit expected inventory prevents an empty or partial needs object from passing."""
        self.env.update(REQUIRED_JOBS_JSON='["check","test"]', ALLOWED_SKIPS_JSON='[]')
        for result in (None, 'failure', 'cancelled', 'skipped', '', True):
            evidence = {'check': {'result': 'success'}}
            if result is not None:
                evidence['test'] = {'result': result}
            self.env['JOB_RESULTS_JSON'] = json.dumps(evidence)
            self.assertNotEqual(self.execute(GATE, 'Require complete successful evidence').returncode, 0)
        self.env['ALLOWED_SKIPS_JSON'] = '["test"]'
        self.env['JOB_RESULTS_JSON'] = '{"check":{"result":"success"},"test":{"result":"skipped"}}'
        self.assertEqual(self.execute(GATE, 'Require complete successful evidence').returncode, 0)
        self.env['ALLOWED_SKIPS_JSON'] = '["typo"]'
        self.assertNotEqual(self.execute(GATE, 'Require complete successful evidence').returncode, 0)
        self.env.update(ALLOWED_SKIPS_JSON='[]', REQUIRED_JOBS_JSON='[]')
        self.assertNotEqual(self.execute(GATE, 'Require complete successful evidence').returncode, 0)

    def test_dotnet_uses_event_identity_without_arbitrary_checkout_or_unpinned_actions(self):
        """The event supplies analysis identity; callers cannot relabel arbitrary code as a trusted PR."""
        self.assertNotIn('checkout-ref:', DOTNET)
        self.assertNotIn('use-pull-request-scan:', DOTNET)
        self.assertIn('ref: ${{ steps.invocation.outputs.revision }}', DOTNET)
        self.assertIn('PR_HEAD_REVISION: ${{ github.event.pull_request.head.sha }}', DOTNET)
        self.assertIn('PR_HEAD_REPOSITORY: ${{ github.event.pull_request.head.repo.full_name }}', DOTNET)
        self.assertIn('PR_BASE_REPOSITORY: ${{ github.event.pull_request.base.repo.full_name }}', DOTNET)
        self.assertIn('persist-credentials: false', DOTNET)
        pre_begin = DOTNET.split('      - name: Begin .NET analysis', 1)[0]
        self.assertNotIn('${{ secrets.SONAR_TOKEN }}', pre_begin)
        build = DOTNET.split('      - name: Build and test the analysis graph', 1)[1].split('      - name: End .NET analysis', 1)[0]
        self.assertNotIn('${{ secrets.SONAR_TOKEN }}', build)
        for line in DOTNET.splitlines():
            if 'uses: ' in line:
                self.assertRegex(line, r'@[0-9a-f]{40}(?:\s|$)')
