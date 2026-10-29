# Casadega action-workflows

Public reusable GitHub Actions workflows for Casadega projects. This repository is intentionally
**public** so personal repositories (for example `reggieofarrell/flintfire`) can call it with
`uses:` the same way org-internal repos call a shared workflow catalog.

This catalog distributes SonarQube analysis and an optional required-check aggregator. The local
`check.yml` validates these contracts; deployment workflows remain outside its scope.

## Workflows

| Workflow                   | Path                                           | Role                                                                                                                  |
| -------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| SonarQube Scan             | `.github/workflows/sonar-scan.yml`             | Inner scan: PR vs branch params, coverage download, Compute Engine wait, issue gate, and official quality-gate action |
| SonarQube Scan After Tests | `.github/workflows/sonar-scan-after-tests.yml` | Orchestrator for `workflow_run` or manual `test-run-id`; calls `sonar-scan.yml`                                       |
| SonarQube .NET Trusted Analysis | `.github/workflows/sonar-dotnet.yml` | Build-integrated C# analysis for trusted same-repository PRs and default-branch commits |
| Required checks | `.github/workflows/required-checks.yml` | Explicit inventory of required job results, with deliberate permitted skips |

Action pins:

- `SonarSource/sonarqube-scan-action@v8.1.0`
- `sonarsource/sonarqube-quality-gate-action@v1.2.1` with `pollingTimeoutSec: 600`

The YAML pins these releases, checkout, dependency setup, and artifact actions to their full commit
SHAs. Release names remain in adjacent comments for review; updates must resolve the reviewed tag
to its commit rather than silently following a mutable tag.

Do **not** pin the quality-gate action to `v1.3.1` (that tag does not exist) or to `@master`.

## Caller requirements

Configure these on the **calling** repository. This repo has no Sonar credentials of its own.

| Name              | Kind            | Purpose                                                                                                                                                                                                                   |
| ----------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SONAR_TOKEN`     | Secret          | **Project analysis token** (My Account → Security → Generate Tokens → type Project). A user token is for local CLI/precheck; CI must be a project analysis token (or a user token with Execute Analysis on that project). |
| `NODE_AUTH_TOKEN` | Optional secret | Package-registry read token used only while restoring dependencies for analysis. Required when the locked dependency graph contains private packages that the caller's `GITHUB_TOKEN` cannot read.                        |

The caller must check in `sonar-project.properties` with non-empty `sonar.host.url` and
`sonar.projectKey` values. Commit that identity on the PR base/default branch before enabling scans:
the generic scan's identity job reads the PR base commit, or the default branch for other events,
on a runner that never executes repository code. The analyzed head must retain that same host and project. A legacy
`SONAR_HOST_URL` repository variable is compared for migration diagnostics and ignored when it
disagrees, so ambient GitHub configuration cannot redirect source to another SonarQube server.

Identity changes therefore need to reach the trusted branch before analyzing a head that uses the
new identity. Plain property keys and a plain HTTP(S) endpoint are required; escaped keys, executable
overrides, embedded credentials, proxies, trust-store settings, and scanner-directory overrides are
rejected before a scanner or credentialed API request runs.

**Pass `SONAR_TOKEN` explicitly.** `secrets: inherit` only works inside the same GitHub organization or user. A personal repository calling this public org workflow will see an empty token (HTTP 401, and GitHub will log `SONAR_TOKEN:` with no `***` mask). Same-org Casadega callers may use inherit; everyone else must map. If the project consumes private packages, map its package token into the workflow's conventional `NODE_AUTH_TOKEN` secret at the same boundary:

```yaml
secrets:
  SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
  NODE_AUTH_TOKEN: ${{ secrets.CASADEGA_PACKAGES_TOKEN || github.token }}
```

The reusable job exposes `NODE_AUTH_TOKEN` only while writing a runner-local GitHub Packages auth
placeholder and running the selected package-manager install. The scanner, gate APIs, and
diagnostic steps never receive it. Consumers need only the scope-to-registry mapping in project
configuration; they must not commit a token or a CI-only auth placeholder.

### Permissions

The job that `uses:` these workflows must grant:

```yaml
permissions:
  actions: read # download coverage artifacts
  contents: read # checkout the analyzed commit
  packages: read # allow the caller's GITHUB_TOKEN to restore permitted private packages
```

Pull-request comments come from SonarQube's configured pull-request decoration integration. The
shared workflow does not publish a second GitHub Actions comment and therefore does not require
`pull-requests: write`.

The scan-after-tests orchestrator additionally requires `pull-requests: read` so it can recover the
pull-request number and branch metadata from the completed test run.

## Isolated preparation and analysis

The reusable scan resolves one immutable source commit, then runs preparation and analysis in
separate jobs. Preparation restores dependencies, downloads coverage, and runs `pre-sonar-command`
without `SONAR_TOKEN`. Analysis checks out the resolved commit without persisted checkout
credentials and consumes the preparation artifact as untrusted data. Only ordinary reports, the
declared generated coverage properties file, and confined `node_modules` data are accepted by default. Explicit Java opt-in adds only the
declared bytecode/library directories described below. Archive
traversal, hard links, source-file replacement, and links escaping through either the artifact or
the checked-out tree fail validation. Package-manager commands are blocked in analysis; no repository build step runs there. The
separate .NET workflow has a different, deliberately narrower trust contract described below.

Both jobs require **GitHub-hosted Linux runners**. `runner-labels-json` can select a larger hosted
Linux runner; persistent self-hosted runners are rejected because they do not establish the required
separation. The artifact has a unique name per invocation and one-day retention. A failed identity
or preparation job explicitly fails the existing `sonarqube` job instead of skipping it.

`pre-sonar-command` may generate `sonar-project.coverage.generated.properties` and untracked LCOV,
`.info`, XML, or JSON-under-`coverage` reports. It cannot transfer arbitrary generated application
code or overwrite checked-out files in analysis. Absolute LCOV source paths are relocated to the
analysis workspace. The final properties are checked again after generated properties are applied.

The shared workflow cannot repair a caller that is itself skipped after `needs: test` fails. Callers
should run the reusable job with `if: ${{ !cancelled() }}` (combined with their fork policy) and pass
`prerequisite-result: ${{ needs.test.result }}`. Keep an aggregate required test check that runs with
`if: always()` and explicitly asserts its dependencies succeeded. Copy the actual resulting context
names from a PR when configuring branch requirements; workflow titles alone are insufficient.

## Issue gates and diagnostics

Issue responses must contain a nonnegative integer count and a consistent issues array. Two
consecutive empty reads pass the gate; three identical nonempty reads fail early. Other responses
retry up to twelve reads with five seconds between reads, allowing a short indexing delay to settle.
HTTP and malformed-data failures fail the gate. Each API request has a five-second connect timeout
and a fifteen-second total timeout. Missing scanner task metadata also fails rather than claiming
that analysis completed.

Failures list ordinary issues in the job summary. Quality-gate diagnostics also run after an issue
gate failure and list hotspots when a hotspot condition failed. Listings are limited to the first
100 returned entries; the issue gate uses the server's total count for its decision. Hotspots are
never automatically reviewed, dismissed, or marked Safe.

## Type-aware JavaScript and TypeScript analysis

`sonar-node-max-space` sets `sonar.javascript.node.maxspace` for every scan and retry, including calls
through `sonar-scan-after-tests.yml`. Its default is `4096` MiB. The input must be a positive integer;
choose a ceiling below available runner RAM and select a larger hosted runner when necessary. This
is scanner-side memory configuration and does not change the SonarQube server's JVM settings.

SonarJS uses the TypeScript compiler's semantic model. A scan can complete without installed
dependencies, but unresolved external declarations reduce type-inference precision and can both
hide valid findings and create misleading ones. The workflow therefore restores locked Node
dependencies before scanning by default. SonarSource likewise recommends installing unavailable
dependencies to improve TypeScript type-inference precision in its
[JavaScript and TypeScript analysis documentation](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/languages/javascript-typescript-css/#unavailable-dependencies).

`install-node-dependencies` accepts three modes:

| Mode    | Behavior                                                                                                                 |
| ------- | ------------------------------------------------------------------------------------------------------------------------ |
| `auto`  | Install when `node-project-directory` contains `package.json` and exactly one recognized lockfile. This is the default.  |
| `true`  | Require that locked Node-project shape and fail with an actionable error if it is absent.                                |
| `false` | Skip dependency setup explicitly. Use only when analysis genuinely does not need the repository's Node dependency graph. |

Recognized lockfiles are `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, and
`yarn.lock`. Multiple lockfiles fail closed because choosing one silently could analyze a graph
different from the graph exercised by CI. When `package.json` declares `packageManager`, its name
must agree with the lockfile.

Installs are deterministic and do not run lifecycle scripts:

- npm: `npm ci --ignore-scripts --no-audit --no-fund`
- pnpm: `pnpm install --frozen-lockfile --ignore-scripts`
- Yarn: `yarn install --frozen-lockfile --ignore-scripts`

`node-project-directory` defaults to the repository root and may select a repository-relative
subdirectory for another layout. `node-version-file` defaults to `.nvmrc` within that directory.
When that file is absent, the workflow asks `actions/setup-node` to resolve the version from
`package.json` (`volta.node`, `engines.node`, or `devEngines.runtime`), then falls back to the
current LTS release. pnpm is installed before `actions/setup-node` so pnpm caching works; its exact
version comes from `packageManager` unless `pnpm-version` is supplied.

Package-manager caches remain scoped to the calling repository and are keyed by its authoritative
lockfile. A project-level `.npmrc` is read normally because installation runs inside the checked-out
repository. When `NODE_AUTH_TOKEN` is supplied, the workflow writes a runner-only
`//npm.pkg.github.com/:_authToken=${NODE_AUTH_TOKEN}` entry before installation; repository config
still does not grant authentication by itself.

## Example (same-repo PR + main baseline)

```yaml
sonarqube:
  name: SonarQube
  needs: test
  if: ${{ !cancelled() && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) }}
  permissions:
    actions: read
    contents: read
    packages: read
  uses: Casadega-Development/action-workflows/.github/workflows/sonar-scan.yml@main
  secrets:
    SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
    NODE_AUTH_TOKEN: ${{ secrets.CASADEGA_PACKAGES_TOKEN || github.token }}
  with:
    prerequisite-result: ${{ needs.test.result }}
    sonar-node-max-space: '4096'
    coverage-artifacts-json: '[{"name":"coverage-unit","path":"coverage/unit"}]'
    issue-gate-scope: new-code
    enforce-quality-gate: true
    use-pull-request-scan: ${{ github.event_name == 'pull_request' }}
    pull-request-key: ${{ github.event.pull_request.number || '' }}
    pull-request-branch: ${{ github.head_ref || '' }}
    pull-request-base: ${{ github.base_ref || '' }}
    scm-revision: ${{ github.event.pull_request.head.sha || '' }}
    checkout-ref: ${{ github.event.pull_request.head.sha || github.sha }}
    branch-name: ${{ github.ref_name }}
```

Manual re-scan of a completed test run:

```yaml
jobs:
  sonarqube:
    permissions:
      actions: read
      contents: read
      packages: read
      pull-requests: read
    uses: Casadega-Development/action-workflows/.github/workflows/sonar-scan-after-tests.yml@main
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
      NODE_AUTH_TOKEN: ${{ secrets.CASADEGA_PACKAGES_TOKEN || github.token }}
    with:
      test-run-id: ${{ github.event.inputs.test-run-id }}
      coverage-artifacts-json: '[{"name":"coverage-unit","path":"coverage/unit"}]'
      issue-gate-scope: new-code
```

## Repository validation

Run `actionlint` and `python3 -m unittest discover -s tests -v` from this repository. The Python suite
uses only the standard library and executes the actual embedded gate and isolation scripts with
synthetic HTTP responses and hostile archive/property fixtures. It never contacts SonarQube or uses
real credentials. The local CI workflow runs both checks on PRs and pushes to `main`.

### Explicit unreviewed-hotspot policy

Consumers that require every security hotspot to be reviewed can set
`enforce-zero-unreviewed-hotspots: true` on either reusable workflow. The default is false,
so existing server quality-gate policies remain unchanged. This API gate checks all `TO_REVIEW`
hotspots in the selected PR or branch independently of ordinary issue scope and server thresholds.
It uses bounded requests, two consecutive empty reads to pass, and three identical nonempty reads
to fail early. Malformed counts, inconsistent first-page results, and request failures block.
Failures list up to 100 hotspot locations. The gate runs in the isolated analysis job without
executing repository-owned code, including after an ordinary issue-gate failure.

## Opt-in Java and Ant analysis

The generic scan can now transfer Java bytecode without changing its existing report/dependency
contract. Both new inputs default to the empty string and are forwarded by the after-tests workflow:

| Input | Meaning |
| --- | --- |
| `java-artifacts-json` | A nonempty array of literal output-directory descriptors when Java is enabled. Each descriptor has exactly `path` and `kind`. |
| `java-version` | Exact Temurin JDK release **including build**, such as `17.0.12+7`; choose the reviewed release for the consumer. Floating versions are rejected. |

Allowed `kind` values map to the scanner as follows:

| Kind | Accepted files | Scanner property |
| --- | --- | --- |
| `binaries` | `.class` | `sonar.java.binaries` |
| `test-binaries` | `.class` | `sonar.java.test.binaries` |
| `libraries` | `.jar` | `sonar.java.libraries` |
| `test-libraries` | `.jar` | `sonar.java.test.libraries` |

At least one production `binaries` directory is required. Every declared directory must contain at
least one file of its declared type; missing bytecode fails the scan. Paths are repository-relative,
non-overlapping literal directories, never globs. The trusted workflow configures both preparation
and analysis with the selected JDK, and sets `sonar.java.source` and `sonar.java.jdkHome` from that
selection. Library directory paths become recursive JAR patterns for the scanner.

For example, add these inputs to the existing generic scan caller, adjusting paths and the reviewed
JDK to the actual Ant build:

```yaml
with:
  java-version: '17.0.12+7'
  java-artifacts-json: >-
    [{"path":"java/build/classes","kind":"binaries"},
     {"path":"java/build/libraries","kind":"libraries"}]
  pre-sonar-command: ant -f java/build.xml clean test jar
```

The example assumes the build creates those output directories and materializes its dependency
JARs there. Ant and other build prerequisites must already be available on the selected hosted
preparation runner, or be installed by the consumer's preparation command. No Ant/Maven/Gradle
migration is required. The ordinary required tests remain authoritative; `pre-sonar-command` is
preparation for analysis, not a substitute for them.

The separate Java archive is bound to the same immutable source revision as other analysis data.
Packaging and extraction validate the declaration independently. Symlinks, archive hardlinks,
traversal, unexpected file types, undeclared roots, and overwriting checked-out files are rejected.
The Java archive cannot supply scripts or expand its own allowed paths. Compiled files must be
untracked output, so versioned vendor JARs must be copied into the declared build output directory.
Other files in those directories are not transferred. This intentionally remains a data-only
handoff; the scanner job does not run Ant, tests, or application classes.

[Sonar's Java documentation](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/languages/java)
requires compiled bytecode for multi-file projects and describes the JDK and binary-path settings.

## Trusted .NET pull-request and default-branch analysis

`.github/workflows/sonar-dotnet.yml` is a new opt-in workflow. It accepts open `pull_request` events
whose source and target repositories both match the caller, plus `push` or `workflow_dispatch`
on the caller's default branch. PR analysis checks out the exact event head SHA and uses the PR's
number, source branch, and target branch; default-branch analysis uses the triggering commit SHA.
There is no caller-supplied checkout override. Forks, `pull_request_target`, `workflow_run`, closed
PRs, feature-branch dispatch, tags, and malformed event metadata are rejected before credentials
are used. The runner is disposable GitHub-hosted Linux with read-only repository access.

**Make both the ordinary quality check and Sonar required before merging internal PRs.** The
caller must pass `prerequisite-result: ${{ needs.check.result }}` from an ordinary quality job
that checks out the same PR head (or default-branch commit). The reusable workflow requires
`success`; it does not independently look up the caller's checks. Keep formatting, analyzer-enabled
builds, tests, coverage thresholds, and dependency checks in that ordinary job. Keep a default-branch
Sonar scan after merge to maintain the branch baseline.

Same-repository PR support deliberately trusts repository contributors and their build code with
the project analysis token. It does not prove that a human approved a commit. Protect workflows,
build definitions, and the default branch through the consumer's review policy. Fork analysis
needs a separate trust policy and is not enabled here. A skipped Sonar job must not silently pass
a required-check aggregate. Use a project analysis token rather than an administrator or broad
personal token. The PR's committed Sonar host and project must match its base commit; identity
changes must reach the target branch first.

[SonarScanner for .NET](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/scanners/dotnet/introduction)
uses a begin/build/end sequence. The build executes repository-defined MSBuild targets in the same
job as credentialed scanner steps. Passing the token only to begin/end and server-gate steps is
**not** a credential-isolation guarantee against that build. This workflow does not inherit the
generic scan's separation between untrusted preparation and credentialed analysis.

The separate ordinary build remains required because the scanner's begin phase changes analyzer
settings and disables `WarningsAsErrors`.
[Documented scanner behavior](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/scanners/dotnet/using)

The consumer must provide:

- `project-directory` (default `.`) containing `global.json` with an exact stable SDK version and
  `rollForward: "disable"`.
- `.config/dotnet-tools.json` in that directory with an exact stable `dotnet-sonarscanner` version
  10 or newer and the `dotnet-sonarscanner` command. The workflow installs precisely that version
  into its own temporary tool directory using NuGet.org; no floating scanner version is selected.
- `project-file`, a `.sln`, `.slnx`, or `.csproj` path relative to that directory.
- `test-projects-json`, a nonempty array of test `.csproj` paths relative to that directory.
  Each test project must use VSTest and a pinned `coverlet.collector` dependency. Executable custom
  harnesses and Microsoft.Testing.Platform require a future explicitly supported workflow adapter.
- Tracked, current `packages.lock.json` files for the restored graph. Locked restore also enables
  lock generation and then rejects any new/untracked or modified lock, including ignored files.
- Root `sonar-project.properties` with plain `sonar.host.url` (HTTPS) and `sonar.projectKey` values.
  The .NET scanner does not consume this file directly: the workflow explicitly translates identity
  and optional `sonar.exclusions`, `sonar.coverage.exclusions`, `sonar.cpd.exclusions`, and
  `sonar.test.exclusions`. Other properties fail with a diagnostic rather than being silently ignored.

The workflow builds the analysis project and every declared test project, then requires fresh,
nonempty TRX execution and OpenCover coverage for every test project. A successful `dotnet test`
exit with zero executed tests or missing/uninstrumented coverage fails before upload. Coverage
thresholds belong to the ordinary required quality check and server quality gate. C# coverage is
imported through `sonar.cs.opencover.reportsPaths` and test results through
`sonar.cs.vstest.reportsPaths`.
[Sonar's .NET coverage contract](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/dotnet-test-coverage)

`configuration` defaults to `Release`. `issue-gate-scope` defaults to `all-open` and accepts
`new-code` or `off`. The server quality gate is always required. The optional independent
`enforce-zero-unreviewed-hotspots` policy defaults to false. Missing scanner task metadata, failed
server processing, malformed issue/hotspot evidence, and quality-gate failures block the job. The
server must support the selected branch or PR analysis before enabling this workflow. Unsupported
PR analysis fails rather than falling back to a branch scan or passing without evidence. Offline
tests do not establish the capabilities of any live Sonar instance. On PR runs, issue and hotspot
queries select that PR; on default-branch runs, `all-open` can include existing unresolved debt.

The initial adapter explicitly sets `sonar.scanner.scanAll=false`: it establishes C# analysis,
not complete coverage of a mixed C#/Python/TypeScript/Java repository. Configure other components
separately, with separate Sonar project identities if their uploads would otherwise replace the
same project/branch analysis. Multiple .NET components should normally share one solution analysis.

Enable PR and default-branch events in the caller, replacing `main` if its default branch differs:

```yaml
on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:
```

In the existing ordinary quality job, check out the exact same source revision as the scan:

```yaml
- uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
  with:
    ref: ${{ github.event.pull_request.head.sha || github.sha }}
    persist-credentials: false
```

Sonar requires the PR source checkout rather than GitHub's synthetic merge commit, with target
branch history available. The scan uses a full checkout for this purpose.
[Sonar PR prerequisites](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/setting-up-the-pull-request-analysis)

Add this job alongside the existing ordinary quality job, replacing the placeholder with the
reviewed workflow commit before use:

```yaml
sonar-dotnet:
  needs: check
  if: >-
    ${{ !cancelled() &&
        ((github.event_name == 'pull_request' &&
          github.event.pull_request.head.repo.full_name == github.repository &&
          github.event.pull_request.base.repo.full_name == github.repository) ||
         ((github.event_name == 'push' || github.event_name == 'workflow_dispatch') &&
          github.ref == format('refs/heads/{0}', github.event.repository.default_branch))) }}
  permissions:
    contents: read
  uses: Casadega-Development/action-workflows/.github/workflows/sonar-dotnet.yml@REVIEWED_COMMIT_SHA
  with:
    prerequisite-result: ${{ needs.check.result }}
    project-file: Trading.sln
    test-projects-json: '["Tests/Trading.Tests.csproj"]'
    enforce-zero-unreviewed-hotspots: true
  secrets:
    SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

Observe the actual check name on a hosted PR before adding it to branch requirements. Deployment
protection is separate: a release can wait for the default-branch scan of the same commit, but this
workflow does not configure deployments or roll back a merge.

## Optional required-check aggregator

`.github/workflows/required-checks.yml` accepts `job-results-json` from `toJSON(needs)` and an explicit
nonempty `required-jobs-json` inventory. Missing jobs, invalid results, failures, cancellation, and
unexpected skips fail. `allowed-skips-json` defaults to `[]`; only explicitly listed required jobs
may skip, and unknown/duplicate names fail. This can express a consumer's deliberate fork policy
without treating every skip as success.

The caller **must** use `if: always()` and declare every prerequisite in `needs`; the reusable
workflow cannot revive a caller that GitHub already skipped. Adoption can change the displayed
required-check context, so keep existing aggregate jobs in place until the new context has been
observed on a real PR and branch requirements have been updated deliberately.

```yaml
required:
  needs: [check, tests, sonarqube]
  if: always()
  permissions: {}
  uses: Casadega-Development/action-workflows/.github/workflows/required-checks.yml@REVIEWED_COMMIT_SHA
  with:
    job-results-json: ${{ toJSON(needs) }}
    required-jobs-json: '["check","tests","sonarqube"]'
```

Existing generic workflow names, inputs, defaults, and job identifiers retain their behavior when
Java opt-in is absent. Consumers pinned to commits adopt these additions through a reviewed pin
update. Consumers following `@main` receive repository changes immediately; migrating those callers
to reviewed immutable pins is a separate adoption task. No server quality gate, profile assignment,
branch requirement, or consuming repository is changed by this catalog update.

The test suite additionally executes the native input guards, restore/build/test contracts,
required-result gate, and hostile Java artifacts offline. A JDK smoke test compiles an assertion,
transfers the resulting class/JAR data, and executes the transferred class. No test contacts Sonar
or uses real credentials; GitHub execution and a live .NET scan remain rollout validation steps.
