param(
    [ValidateSet('start', 'status', 'collect', 'collect-scheduled', 'verify', 'check_dsh', 'member', 'member-review', 'member-access', 'next', 'catalog', 'catalog-progress')]
    [string]$Action = 'status',
    [ValidateRange(1, 65535)]
    [int]$Port,
    [string]$Python = $env:WORLDANALYSIS_PYTHON,
    [string[]]$CatalogArguments = @()
)

$ErrorActionPreference = 'Stop'
if (!$Python) { $Python = 'python' }
$PortArguments = @()
if ($PSBoundParameters.ContainsKey('Port')) { $PortArguments = @('--port', $Port) }
Push-Location -LiteralPath $PSScriptRoot
try {
    switch ($Action) {
        'catalog' { & $Python -X utf8 -B catalog_tasks.py @CatalogArguments }
        'catalog-progress' { & $Python -X utf8 -B catalog_progress.py @CatalogArguments }
        'start' { & $Python -X utf8 -B server.py @PortArguments }
        'member' { & $Python -X utf8 -B member_queue.py --resume --limit 72 }
        'member-review' { & $Python -X utf8 -B member_queue.py --only-review --limit 72 }
        'member-access' { & $Python -X utf8 -B member_queue.py --only-access-required --limit 72 }
        'next' {
            $pathsJson = & $Python -X utf8 -B workspace_paths.py --dispatch-paths
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            $paths = $pathsJson | ConvertFrom-Json -ErrorAction Stop
            $savedPython = $env:WORLDANALYSIS_PYTHON
            try {
                $env:WORLDANALYSIS_PYTHON = $Python
                & $paths.entry batch dispatch $paths.input --registry $paths.registry
            } finally {
                $env:WORLDANALYSIS_PYTHON = $savedPython
            }
        }
        default { & $Python -X utf8 -B pipeline_control.py @PortArguments $Action }
    }
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
