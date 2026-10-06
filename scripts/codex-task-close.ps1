# Mannaz: podsumowanie / zamkniecie worktree. ASCII, PowerShell 5.1 / 7.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Slug,
    [switch]$Usun
)
$ErrorActionPreference = 'Stop'
function Get-CodexBaseline {
    param([string]$Repo)
    $common = & git -C $Repo rev-parse --git-common-dir
    if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac git-common-dir.' }
    if (-not [IO.Path]::IsPathRooted($common)) { $common = Join-Path $Repo $common }
    $common = [IO.Path]::GetFullPath($common)
    $main = & git -C $Repo rev-parse main
    if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac main.' }
    $configHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $common 'config')).Hash
    $hooksDir = Join-Path $common 'hooks'
    $hooks = @()
    if (Test-Path -LiteralPath $hooksDir) {
        $hooks = @(Get-ChildItem -LiteralPath $hooksDir -File -Recurse |
            Where-Object { $_.Name -notlike '*.sample' } |
            Sort-Object FullName |
            ForEach-Object {
                [pscustomobject][ordered]@{
                    name = $_.FullName.Substring($hooksDir.Length + 1).Replace('\', '/')
                    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
                }
            })
    }
    return [pscustomobject][ordered]@{ main = $main; configSha256 = $configHash; hooks = $hooks }
}
try {
    if ($Slug -cnotmatch '\A[a-z0-9-]+\z') { throw 'Nieprawidlowy slug.' }
    $repo = Split-Path -Parent $PSScriptRoot
    $branch = 'codex/' + $Slug
    $root = [IO.Path]::GetFullPath('D:\codex\worktrees\mannaz')
    $wt = [IO.Path]::GetFullPath((Join-Path $root $Slug))
    if (-not $wt.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Worktree poza dozwolonym katalogiem.'
    }
    $baselinePath = Join-Path $wt ('tmp\codex\' + $Slug + '.baseline')
    if (-not (Test-Path -LiteralPath $baselinePath -PathType Leaf)) {
        throw 'Brak baseline: nie wolno scalac bez wyjasnienia.'
    }
    $before = Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json
    $current = Get-CodexBaseline -Repo $repo
    $checks = [ordered]@{
        main = ($before.main -ceq $current.main)
        config = ($before.configSha256 -ceq $current.configSha256)
        hooks = ((ConvertTo-Json -InputObject @($before.hooks) -Depth 5 -Compress) -ceq
                 (ConvertTo-Json -InputObject @($current.hooks) -Depth 5 -Compress))
    }
    $drift = $false
    foreach ($key in $checks.Keys) {
        if ($checks[$key]) { Write-Output ($key + ': ZGODNE') }
        else { Write-Output ($key + ': ROZJAZD'); $drift = $true }
    }
    if ($drift) { throw 'Baseline ROZJAZD: nie wolno scalac bez wyjasnienia.' }
    & git -C $repo log ('main..' + $branch) --oneline
    if ($LASTEXITCODE -ne 0) { throw 'git log: STOP.' }
    & git -C $repo diff --stat ('main...' + $branch)
    if ($LASTEXITCODE -ne 0) { throw 'git diff: STOP.' }
    if (-not $Usun) { Write-Output 'Bez -Usun: nic nie usunieto.'; exit 0 }
    $registered = @(& git -C $repo worktree list --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac worktree.' }
    $expected = 'worktree ' + $wt.Replace('\', '/')
    if (-not ($registered -icontains $expected)) { throw 'Worktree nie nalezy do tego repo.' }
    $actualBranch = & git -C $wt branch --show-current
    if ($LASTEXITCODE -ne 0 -or $actualBranch -cne $branch) { throw 'Inna galaz w worktree.' }
    $dirty = @(& git -C $wt status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac statusu.' }
    if ($dirty.Count -ne 0) { throw 'Brudne drzewo: odmowa usuniecia.' }
    & git -C $repo merge-base --is-ancestor $branch main
    if ($LASTEXITCODE -ne 0) { throw 'Galaz niescalona do main: nic nie usunieto.' }
    $pytestTemp = [IO.Path]::GetFullPath((Join-Path $wt 'tmp\codex\pytest-temp'))
    if (-not $pytestTemp.StartsWith($wt + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Katalog tymczasowy poza worktree: odmowa usuniecia.'
    }
    if (Test-Path -LiteralPath $pytestTemp) {
        foreach ($relative in @('tmp', 'tmp\codex', 'tmp\codex\pytest-temp')) {
            $part = Get-Item -LiteralPath (Join-Path $wt $relative) -Force
            if (($part.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Katalog tymczasowy przez reparse point: odmowa usuniecia.'
            }
        }
        try {
            Remove-Item -LiteralPath $pytestTemp -Recurse -Force -ErrorAction Stop
        } catch {
            throw ('Nie usunieto pytest-temp; worktree pozostaje. Blad: ' + $_.Exception.Message)
        }
        Write-Output 'pytest-temp: USUNIETO'
    } else {
        Write-Output 'pytest-temp: BRAK'
    }
    & git -C $repo worktree remove $wt
    if ($LASTEXITCODE -ne 0) { throw 'git worktree remove: STOP, bez ponawiania.' }
    & git -C $repo branch -d $branch
    if ($LASTEXITCODE -ne 0) { throw 'git branch -d: STOP, bez ponawiania.' }
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
