param(
    [ValidateSet('start', 'status', 'collect', 'verify', 'check_dsh', 'member', 'member-review', 'member-access', 'next', 'catalog', 'catalog-progress')]
    [string]$Action = 'status',
    [ValidateRange(1, 65535)]
    [int]$Port = 8767,
    [string]$Python = $env:WORLDANALYSIS_PYTHON,
    [string[]]$CatalogArguments = @()
)

$ErrorActionPreference = 'Stop'
if (!$Python) { $Python = 'python' }
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
Push-Location -LiteralPath $PSScriptRoot
try {
    switch ($Action) {
        'catalog' { & $Python -X utf8 -B catalog_tasks.py @CatalogArguments }
        'catalog-progress' { & $Python -X utf8 -B catalog_progress.py @CatalogArguments }
        'start' { & $Python -X utf8 -B server.py --port $Port }
        'member' { & $Python -X utf8 -B member_queue.py --resume --limit 72 }
        'member-review' { & $Python -X utf8 -B member_queue.py --only-review --limit 72 }
        'member-access' { & $Python -X utf8 -B member_queue.py --only-access-required --limit 72 }
        'next' {
            $course = Join-Path $project '.runtime/course-production-worktree/_tools/course_production/course.ps1'
            if (!(Test-Path -LiteralPath $course -PathType Leaf)) {
                Write-Error '找不到现有正式课程运行入口。请检查项目的正式工作树。'
            }
            $savedPython = $env:WORLDANALYSIS_PYTHON
            try {
                $env:WORLDANALYSIS_PYTHON = $Python
                & $course batch dispatch (Join-Path $project '分析目标下载地址') --registry (Join-Path $project '整理结果/.course-dispatch/分类索引.json')
            } finally {
                $env:WORLDANALYSIS_PYTHON = $savedPython
            }
        }
        default { & $Python -X utf8 -B pipeline_control.py --port $Port $Action }
    }
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
