# Casadega action-workflows

Public reusable GitHub Actions workflows for Casadega projects. This repository is intentionally
**public** so personal repositories (for example `reggieofarrell/flintfire`) can call it with
`uses:` the same way org-internal repos call a shared workflow catalog.

Only SonarQube scan workflows are distributed here. The local `check.yml` workflow validates this
repository's contracts; unrelated native-app and Rust workflow catalogs remain outside its scope.

## Workflows

| Workflow                   | Path                                           | Role                                                                                                                  |
| -------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| SonarQube Scan             | `.github/workflows/sonar-scan.yml`             | Inner scan: PR vs branch params, coverage download, Compute Engine wait, issue gate, and official quality-gate action |
| SonarQube Scan After Tests | `.github/workflows/sonar-scan-after-tests.yml` | Orchestrator for `workflow_run` or manual `test-run-id`; calls `sonar-scan.yml`                                       |

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
the identity job reads the PR base commit, or the default branch for other events, on a runner that
never executes repository code. The analyzed head must retain that same host and project. A legacy
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
declared generated coverage properties file, and confined `node_modules` data are accepted. Archive
traversal, hard links, source-file replacement, and links escaping through either the artifact or
the checked-out tree fail validation. Package managers and build tools are unavailable in analysis.

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
