<#
Lock down the GitHub repository. Idempotent: run it again any time to re-apply.

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\secure-repo.ps1 [-Repo owner/name] [-RequireReview]

Needs the gh CLI logged in as a repo admin (gh auth login).

What it does:
  main branch (ruleset "protect-main", no bypass, admins included):
    - changes only through pull requests, with CI job "test" passing on the latest main
    - no force pushes, no deletion, linear history, conversations resolved
    - -RequireReview: also 1 approving review (only useful with a second maintainer;
      GitHub never lets you approve your own pull request)
  release tags v*: cannot be moved or deleted (ruleset "protect-release-tags")
  security: secret scanning + push protection, Dependabot alerts and security fixes,
    private vulnerability reporting, CodeQL default setup
  Actions: GITHUB_TOKEN read-only, cannot approve pull requests, only GitHub-owned
    actions allowed, approval needed before any outside contributor's workflow runs
  merging: squash only, head branches deleted after merge, wiki and projects off
#>
param(
    [string]$Repo = '',
    [switch]$RequireReview
)
$ErrorActionPreference = 'Stop'

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh CLI not found: winget install GitHub.cli' }
if (-not $Repo) { $Repo = (gh repo view --json nameWithOwner --jq .nameWithOwner) }
if (-not $Repo) { throw 'pass -Repo owner/name (or run inside a clone)' }
if ((gh api "repos/$Repo" --jq .permissions.admin) -ne 'true') { throw "you are not an admin of $Repo" }

$script:failed = 0
# gh api with an optional JSON body sent through a temp file (no shell quoting surprises).
function Api([string]$method, [string]$path, $body = $null, [switch]$Optional) {
    $ghArgs = @('api', '-X', $method, "repos/$Repo$path", '-H', 'Accept: application/vnd.github+json')
    $tmp = $null
    if ($null -ne $body) {
        $tmp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($tmp, ($body | ConvertTo-Json -Depth 10 -Compress), (New-Object Text.UTF8Encoding $false))
        $ghArgs += @('--input', $tmp)
    }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = & gh @ghArgs 2>&1 } finally { $ErrorActionPreference = $prev; if ($tmp) { Remove-Item $tmp -Force } }
    if ($LASTEXITCODE -ne 0) {
        $msg = ($out | Out-String).Trim() -replace '\s+', ' '
        if ($Optional) { Write-Host "  skipped ($method $path): $msg" -ForegroundColor Yellow; return $null }
        Write-Host "  FAILED ($method $path): $msg" -ForegroundColor Red; $script:failed++; return $null
    }
    if ($out) { try { return ($out | Out-String | ConvertFrom-Json) } catch { } }
    return $true   # success with an empty (204) reply
}
function Ok([string]$what) { Write-Host "  ok: $what" -ForegroundColor Green }

Write-Host "Securing $Repo" -ForegroundColor Cyan

Write-Host '[1/6] Merge and feature settings'
Api PATCH '' @{
    allow_squash_merge = $true; allow_merge_commit = $false; allow_rebase_merge = $false
    delete_branch_on_merge = $true; allow_auto_merge = $true
    has_wiki = $false; has_projects = $false
} | Out-Null
Ok 'squash merges only, branches deleted after merge, wiki and projects off'

Write-Host '[2/6] Secret scanning, Dependabot, vulnerability reporting'
Api PATCH '' @{ security_and_analysis = @{
    secret_scanning = @{ status = 'enabled' }
    secret_scanning_push_protection = @{ status = 'enabled' }
} } | Out-Null
Ok 'secret scanning + push protection'
Api PATCH '' @{ security_and_analysis = @{
    secret_scanning_non_provider_patterns = @{ status = 'enabled' }
    secret_scanning_validity_checks = @{ status = 'enabled' }
} } -Optional | Out-Null
Api PUT '/vulnerability-alerts' | Out-Null; Ok 'Dependabot alerts'
Api PUT '/automated-security-fixes' | Out-Null; Ok 'Dependabot security fixes'
Api PUT '/private-vulnerability-reporting' | Out-Null; Ok 'private vulnerability reporting'

Write-Host '[3/6] CodeQL code scanning (default setup)'
if ($null -ne (Api PATCH '/code-scanning/default-setup' @{ state = 'configured'; query_suite = 'extended' } -Optional)) { Ok 'CodeQL default setup' }

Write-Host '[4/6] GitHub Actions'
Api PUT '/actions/permissions' @{ enabled = $true; allowed_actions = 'selected' } | Out-Null
Api PUT '/actions/permissions/selected-actions' @{ github_owned_allowed = $true; verified_allowed = $false; patterns_allowed = @() } | Out-Null
Ok 'only GitHub-owned actions may run'
Api PUT '/actions/permissions/workflow' @{ default_workflow_permissions = 'read'; can_approve_pull_request_reviews = $false } | Out-Null
Ok 'GITHUB_TOKEN read-only and cannot approve pull requests'
if ($null -ne (Api PUT '/actions/permissions/fork-pr-contributor-approval' @{ approval_policy = 'all_external_contributors' } -Optional)) {
    Ok 'outside contributors need approval before workflows run'
}

# Create the ruleset, or replace an existing one with the same name (keeps the run idempotent).
function Ruleset([hashtable]$rs) {
    $existing = (Api GET '/rulesets?includes_parents=false') | Where-Object { $_.name -eq $rs.name } | Select-Object -First 1
    if ($existing) { Api PUT "/rulesets/$($existing.id)" $rs | Out-Null } else { Api POST '/rulesets' $rs | Out-Null }
}

Write-Host '[5/6] Ruleset: protect main'
$reviews = if ($RequireReview) { 1 } else { 0 }
Ruleset @{
    name = 'protect-main'; target = 'branch'; enforcement = 'active'
    bypass_actors = @()   # nobody bypasses, not even admins
    conditions = @{ ref_name = @{ include = @('~DEFAULT_BRANCH'); exclude = @() } }
    rules = @(
        @{ type = 'deletion' }
        @{ type = 'non_fast_forward' }
        @{ type = 'required_linear_history' }
        @{ type = 'pull_request'; parameters = @{
            required_approving_review_count = $reviews
            dismiss_stale_reviews_on_push = $true
            require_code_owner_review = $false
            require_last_push_approval = [bool]$RequireReview
            required_review_thread_resolution = $true
            allowed_merge_methods = @('squash')
        } }
        @{ type = 'required_status_checks'; parameters = @{
            strict_required_status_checks_policy = $true
            do_not_enforce_on_create = $false
            required_status_checks = @(@{ context = 'test'; integration_id = 15368 })   # 15368 = GitHub Actions
        } }
    )
}
Ok "main: PR only, CI 'test' required, no force push or deletion, $reviews required review(s)"

Write-Host '[6/6] Ruleset: protect release tags'
Ruleset @{
    name = 'protect-release-tags'; target = 'tag'; enforcement = 'active'; bypass_actors = @()
    conditions = @{ ref_name = @{ include = @('refs/tags/v*'); exclude = @() } }
    rules = @(@{ type = 'deletion' }, @{ type = 'non_fast_forward' }, @{ type = 'update' })
}
Ok 'v* tags cannot be moved or deleted'

Write-Host ''
Write-Host 'Current state:' -ForegroundColor Cyan
$r = Api GET ''
'  visibility={0} squash_only={1} delete_branch_on_merge={2}' -f $r.visibility, (-not $r.allow_merge_commit -and -not $r.allow_rebase_merge), $r.delete_branch_on_merge
$r.security_and_analysis.PSObject.Properties | ForEach-Object { '  {0}={1}' -f $_.Name, $_.Value.status }
(Api GET '/rulesets') | ForEach-Object { '  ruleset {0}: {1}' -f $_.name, $_.enforcement }
if ($script:failed) { Write-Host "$script:failed step(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'Done.' -ForegroundColor Green
