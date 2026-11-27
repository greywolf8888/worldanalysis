# Agent entry: preserve the full tool result in the project runtime and return a small receipt.
& (Join-Path $PSScriptRoot 'course.ps1') --agent-compact @args
exit $LASTEXITCODE
