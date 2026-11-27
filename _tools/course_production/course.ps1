$python = $env:WORLDANALYSIS_PYTHON
if (!$python) {
    $localRuntime = Join-Path $PSScriptRoot '../../.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $localRuntime -PathType Leaf) {
        $python = $localRuntime
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            & $launcher.Source -3.12 -X utf8 -B (Join-Path $PSScriptRoot 'run_course.py') @args
            exit $LASTEXITCODE
        }
    }
}
if (!$python) {
    Write-Error '请将 WORLDANALYSIS_PYTHON 指向安装项目依赖的 Python 3.12。'
    exit 2
}
& $python -X utf8 -B (Join-Path $PSScriptRoot 'run_course.py') @args
exit $LASTEXITCODE
