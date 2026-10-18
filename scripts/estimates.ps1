# FPE-RUN: python -m mannaz.run_p3 estimates [argumenty], potem kopia archiwum.
# PYTHONPATH i PYTHONIOENCODING sa ustawione tylko na czas tego procesu i
# przywrocone w finally. Dziala w Windows PowerShell 5.1 i PowerShell 7.
# Log: <repo>\data\estimates\logs\<YYYYMMDDTHHMMSSZ>.log (UTF-8). Log nie
# zawiera zmiennych srodowiska ani zawartosci .env.
# Kopia: robocopy do D:\mannaz-backup\estimates-raw, bez opcji lustra i
# czyszczenia (nic w kopii nie jest usuwane). Kod robocopy >= 8 = blad kopii.
# Weryfikacja: sha256 kazdego .body z tego przebiegu w kopii == raw_sha256 z meta.
# Kod wyjscia: kod Pythona, chyba ze kopia/weryfikacja zawiodla (3).
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$rawDir = Join-Path $repo 'data\estimates\raw'
# Kopia i weryfikacja dotycza katalogu, do ktorego pisze Python: --archive-root X
# albo --archive-root=X (ostatnie wystapienie, jak argparse); sciezka wzgledna
# liczona od biezacego katalogu, ktory dziedziczy proces Pythona.
for ($i = 0; $i -lt $args.Count; $i++) {
    $arg = [string]$args[$i]
    if ($arg -eq '--archive-root' -and ($i + 1) -lt $args.Count) { $rawDir = [string]$args[$i + 1]; $i++ }
    elseif ($arg.StartsWith('--archive-root=')) { $rawDir = $arg.Substring('--archive-root='.Length) }
}
if (-not [System.IO.Path]::IsPathRooted($rawDir)) {
    $rawDir = Join-Path (Get-Location -PSProvider FileSystem).ProviderPath $rawDir
}
$rawDir = [System.IO.Path]::GetFullPath($rawDir)
$logDir = Join-Path $repo 'data\estimates\logs'
$backupDir = 'D:\mannaz-backup\estimates-raw'
$startUtc = [DateTimeOffset]::UtcNow
$log = Join-Path $logDir ($startUtc.ToString('yyyyMMdd\THHmmss\Z') + '.log')
$utf8 = New-Object System.Text.UTF8Encoding($false)

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$line) {
    [System.IO.File]::AppendAllText($log, $line + "`r`n", $utf8)
    Write-Host $line
}

Write-Log ('start UTC: ' + $startUtc.ToString('o'))
Write-Log ('argumenty: ' + ($args -join ' '))
Write-Log ('archiwum: ' + $rawDir)

if (-not (Test-Path -LiteralPath $python)) {
    Write-Log "Brak interpretera: $python (uruchom skrypt w repo glownym z .venv)"
    Write-Log 'kod wrappera: 1'
    exit 1
}

$prevPath = $env:PYTHONPATH
$prevEnc = $env:PYTHONIOENCODING
$pyCode = 1
$roboCode = -1
$copyFailed = $false
$verifyText = 'nie wykonano'
try {
    $env:PYTHONPATH = Join-Path $repo 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    & $python -m mannaz.run_p3 estimates @args 2>&1 | ForEach-Object { Write-Log ("$_") }
    $pyCode = $LASTEXITCODE
    Write-Log "kod Pythona: $pyCode"

    New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
    if (-not (Test-Path -LiteralPath 'D:\')) {
        Write-Log 'BLAD KOPII: brak dysku D:, archiwum zostaje w repo, kopia pominieta'
        $copyFailed = $true
    }
    else {
        & robocopy $rawDir $backupDir /E /XO /R:1 /W:1 /NP /NJH 2>&1 | ForEach-Object { Write-Log ("$_") }
        $roboCode = $LASTEXITCODE
        Write-Log "kod robocopy: $roboCode"
        if ($roboCode -ge 8) {
            Write-Log 'BLAD KOPII: robocopy zwrocil kod >= 8'
            $copyFailed = $true
        }
        else {
            $total = 0
            $okCount = 0
            foreach ($metaFile in Get-ChildItem -LiteralPath $rawDir -Recurse -Filter '*.meta.json' -File) {
                $meta = Get-Content -LiteralPath $metaFile.FullName -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
                if ($null -eq $meta.raw_sha256) { continue }
                # PS 7 ConvertFrom-Json zamienia date ISO na [DateTime]; rzutowanie na
                # [string] obcieloby ulamki sekundy, wiec [DateTime] konwertowany wprost.
                $rawFetched = $meta.fetched_at_utc
                if ($rawFetched -is [DateTime]) { $fetched = [DateTimeOffset]$rawFetched }
                else { $fetched = [DateTimeOffset]::Parse([string]$rawFetched, [Globalization.CultureInfo]::InvariantCulture) }
                if ($fetched -lt $startUtc) { continue }
                $total++
                $copyBody = Join-Path $backupDir (([string]$meta.raw_ref) -replace '/', '\')
                if (Test-Path -LiteralPath $copyBody) {
                    $hash = (Get-FileHash -LiteralPath $copyBody -Algorithm SHA256 -ErrorAction Stop).Hash
                    if ($hash -ieq [string]$meta.raw_sha256) { $okCount++ }
                    else { Write-Log ('NIEZGODNY sha256 w kopii: ' + [string]$meta.raw_ref) }
                }
                else {
                    Write-Log ('BRAK pliku w kopii: ' + [string]$meta.raw_ref)
                }
            }
            $verifyText = "$okCount/$total"
            Write-Log "weryfikacja kopii: $verifyText"
            if ($okCount -ne $total) {
                Write-Log 'BLAD WERYFIKACJI: niezgodnosc kopii'
                $copyFailed = $true
            }
        }
    }
}
catch {
    # Wyjatek w kopii/weryfikacji (np. uszkodzony .meta.json): archiwum zostaje, kod 3.
    Write-Log ('BLAD KOPII/WERYFIKACJI: wyjatek ' + $_.Exception.GetType().Name)
    $copyFailed = $true
}
finally {
    if ($null -eq $prevPath) {
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $prevPath
    }
    if ($null -eq $prevEnc) {
        Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONIOENCODING = $prevEnc
    }
}
$code = $pyCode
if ($copyFailed) { $code = 3 }
Write-Log "kod wrappera: $code"
exit $code
