# Casadega action-workflows

Public reusable GitHub Actions workflows for Casadega projects. This repository is intentionally
**public** so personal repositories (for example `reggieofarrell/flintfire`) can call it with
`uses:` the same way org-internal repos call a shared workflow catalog.

Only SonarQube scan workflows live here. Do not copy unrelated catalogs (native-app CI, rust-cli,
validate, and so on) into this repo.

## Workflows

| Workflow | Path | Role |
| --- | --- | --- |
| SonarQube Scan | `.github/workflows/sonar-scan.yml` | Inner scan: PR vs branch params, coverage download, Compute Engine wait, issue gate, official quality-gate action, optional sticky PR comment |
| SonarQube Scan After Tests | `.github/workflows/sonar-scan-after-tests.yml` | Orchestrator for `workflow_run` or manual `test-run-id`; calls `sonar-scan.yml` |

Action pins:

- `SonarSource/sonarqube-scan-action@v8.1.0`
- `sonarsource/sonarqube-quality-gate-action@v1.2.1` with `pollingTimeoutSec: 600`

Do **not** pin the quality-gate action to `v1.3.1` (that tag does not exist) or to `@master`.

## Caller requirements

Configure these on the **calling** repository. This repo has no Sonar credentials of its own.

| Name | Kind | Purpose |
| --- | --- | --- |
| `SONAR_TOKEN` | Secret | **Project analysis token** (My Account → Security → Generate Tokens → type Project). A user token is for local CLI/precheck; CI must be a project analysis token (or a user token with Execute Analysis on that project). |

The caller must check in `sonar-project.properties` with non-empty `sonar.host.url` and
`sonar.projectKey` values. The committed host is the workflow's only server authority. A legacy
`SONAR_HOST_URL` repository variable is compared for migration diagnostics and ignored when it
disagrees, so ambient GitHub configuration cannot redirect source to another SonarQube server.

**Pass `SONAR_TOKEN` explicitly.** `secrets: inherit` only works inside the same GitHub organization or user. A personal repository calling this public org workflow will see an empty token (HTTP 401, and GitHub will log `SONAR_TOKEN:` with no `***` mask). Same-org Casadega callers may use inherit; everyone else must map:

```yaml
secrets:
  SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

### Permissions

The job that `uses:` these workflows must grant:

```yaml
permissions:
  actions: read      # download coverage artifacts
  contents: read     # checkout the analyzed commit
  pull-requests: write  # sticky PR comment (required when post-pr-comment is true)
```

`pull-requests: write` is required for comments; `secrets: inherit` is not enough for either
comments or the analysis token across organization boundaries. If `pull-requests: write` is
missing, the comment step fails closed with an error that names this requirement.

Set `post-pr-comment: false` to skip the comment (branch/`main` scans skip it regardless).

## Example (same-repo PR + main baseline)

```yaml
sonarqube:
  name: SonarQube
  needs: test
  if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
  permissions:
    actions: read
    contents: read
    pull-requests: write
  uses: Casadega-Development/action-workflows/.github/workflows/sonar-scan.yml@main
  secrets:
    SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
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
      pull-requests: write
    uses: Casadega-Development/action-workflows/.github/workflows/sonar-scan-after-tests.yml@main
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
    with:
      test-run-id: ${{ github.event.inputs.test-run-id }}
      coverage-artifacts-json: '[{"name":"coverage-unit","path":"coverage/unit"}]'
      issue-gate-scope: new-code
```

## Sticky PR comment

On pull-request scans the inner workflow upserts a comment marked `<!-- casadega-sonarqube -->`
(`if: always()`, so a red gate still comments). The body includes quality-gate status, unresolved
issues for the configured `issue-gate-scope`, and a dashboard URL from `sonar.projectKey` plus
the committed `sonar.host.url`. Pushes to `main` do not comment.
