# B-50: uruchomienie cyklu: python -m mannaz.run_p3 cycle [argumenty]
# PYTHONPATH=<repo>\src jest ustawiony tylko na czas tego procesu i przywrocony
# w finally. Dziala w Windows PowerShell 5.1 i PowerShell 7. Kod wyjscia =
# kod wyjscia Pythona.
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Brak interpretera: $python (uruchom skrypt w repo glownym z .venv)"
    exit 1
}
$previous = $env:PYTHONPATH
$code = 1
try {
    $env:PYTHONPATH = Join-Path $repo 'src'
    & $python -m mannaz.run_p3 cycle @args
    $code = $LASTEXITCODE
}
finally {
    if ($null -eq $previous) {
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $previous
    }
}
exit $code
