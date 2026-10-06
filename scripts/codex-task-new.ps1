# Mannaz: nowy worktree Codexa. ASCII, PowerShell 5.1 / 7.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Slug,
    [string]$Pakiet
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
    $root = 'D:\codex\worktrees\mannaz'
    $wt = Join-Path $root $Slug
    if (Test-Path -LiteralPath $wt) { throw 'Katalog worktree juz istnieje.' }
    $refs = @(& git -C $repo for-each-ref --format='%(refname)' ('refs/heads/' + $branch))
    if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac galezi.' }
    if ($refs -contains ('refs/heads/' + $branch)) { throw 'Galaz juz istnieje.' }
    $source = $null
    if ($Pakiet) {
        $source = (Resolve-Path -LiteralPath $Pakiet).ProviderPath
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Pakiet nie jest plikiem.' }
    }
    $baseline = Get-CodexBaseline -Repo $repo
    & git -C $repo worktree add $wt -b $branch main
    if ($LASTEXITCODE -ne 0) { throw 'git worktree add: STOP, bez ponawiania.' }
    $targetDir = Join-Path $wt 'tmp\codex'
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    $target = Join-Path $targetDir ($Slug + '.md')
    if ($source) {
        Copy-Item -LiteralPath $source -Destination $target
    } else {
        Copy-Item -LiteralPath (Join-Path $repo 'docs\codex\_szablon-zadania.md') -Destination $target
    }
    $baselinePath = Join-Path $targetDir ($Slug + '.baseline')
    $json = ConvertTo-Json -InputObject $baseline -Depth 5
    [IO.File]::WriteAllText($baselinePath, $json + [Environment]::NewLine, [Text.Encoding]::ASCII)
    Write-Output ('Baseline: ' + $baselinePath)
    Write-Output ('Pakiet: ' + $target)
    Write-Output ('Uzupelnij pakiet przed startem. Start: codex-mannaz ' + $Slug)
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
