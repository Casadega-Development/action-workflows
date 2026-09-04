# Casadega action-workflows

Public reusable GitHub Actions workflows for Casadega projects. This repository is intentionally
**public** so personal repositories (for example `reggieofarrell/flintfire`) can call it with
`uses:` the same way org-internal repos call a shared workflow catalog.

Only SonarQube scan workflows live here. Do not copy unrelated catalogs (native-app CI, rust-cli,
validate, and so on) into this repo.

## Workflows

| Workflow                   | Path                                           | Role                                                                                                                  |
| -------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| SonarQube Scan             | `.github/workflows/sonar-scan.yml`             | Inner scan: PR vs branch params, coverage download, Compute Engine wait, issue gate, and official quality-gate action |
| SonarQube Scan After Tests | `.github/workflows/sonar-scan-after-tests.yml` | Orchestrator for `workflow_run` or manual `test-run-id`; calls `sonar-scan.yml`                                       |

Action pins:

- `SonarSource/sonarqube-scan-action@v8.1.0`
- `sonarsource/sonarqube-quality-gate-action@v1.2.1` with `pollingTimeoutSec: 600`

Do **not** pin the quality-gate action to `v1.3.1` (that tag does not exist) or to `@master`.

## Caller requirements

Configure these on the **calling** repository. This repo has no Sonar credentials of its own.

| Name              | Kind            | Purpose                                                                                                                                                                                                                   |
| ----------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SONAR_TOKEN`     | Secret          | **Project analysis token** (My Account → Security → Generate Tokens → type Project). A user token is for local CLI/precheck; CI must be a project analysis token (or a user token with Execute Analysis on that project). |
| `NODE_AUTH_TOKEN` | Optional secret | Package-registry read token used only while restoring dependencies for analysis. Required when the locked dependency graph contains private packages that the caller's `GITHUB_TOKEN` cannot read.                        |

The caller must check in `sonar-project.properties` with non-empty `sonar.host.url` and
`sonar.projectKey` values. The committed host is the workflow's only server authority. A legacy
`SONAR_HOST_URL` repository variable is compared for migration diagnostics and ignored when it
disagrees, so ambient GitHub configuration cannot redirect source to another SonarQube server.

**Pass `SONAR_TOKEN` explicitly.** `secrets: inherit` only works inside the same GitHub organization or user. A personal repository calling this public org workflow will see an empty token (HTTP 401, and GitHub will log `SONAR_TOKEN:` with no `***` mask). Same-org Casadega callers may use inherit; everyone else must map. If the project consumes private packages, map its package token into the workflow's conventional `NODE_AUTH_TOKEN` secret at the same boundary:

```yaml
secrets:
  SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
  NODE_AUTH_TOKEN: ${{ secrets.CASADEGA_PACKAGES_TOKEN || github.token }}
```

The calling job and reusable job expose `NODE_AUTH_TOKEN` only to the selected package-manager
install step. The scanner, gate APIs, and diagnostic steps never receive it.

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

## Type-aware JavaScript and TypeScript analysis

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
repository. Registry credentials must still be mapped through `NODE_AUTH_TOKEN`; repository config
does not grant authentication by itself.

## Example (same-repo PR + main baseline)

```yaml
sonarqube:
  name: SonarQube
  needs: test
  if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
  permissions:
    actions: read
    contents: read
    packages: read
  uses: Casadega-Development/action-workflows/.github/workflows/sonar-scan.yml@main
  secrets:
    SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
    NODE_AUTH_TOKEN: ${{ secrets.CASADEGA_PACKAGES_TOKEN || github.token }}
  with:
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
