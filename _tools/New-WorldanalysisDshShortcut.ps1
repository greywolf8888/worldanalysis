param([string]$Pythonw)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $Pythonw) {
    $installed = Join-Path $env:SystemDrive 'Python314\pythonw.exe'
    if (Test-Path -LiteralPath $installed -PathType Leaf) {
        $Pythonw = $installed
    } else {
        $Pythonw = (Get-Command -Name pythonw.exe -CommandType Application | Select-Object -First 1).Source
    }
}
$python = (Resolve-Path -LiteralPath $Pythonw).Path
if ([IO.Path]::GetFileName($python) -ine 'pythonw.exe') { throw '请指定 Python 的 pythonw.exe。' }
if ((Get-AuthenticodeSignature -FilePath $python).Status -ne 'Valid') { throw '请使用签名有效的 Python 安装。' }

$entry = Join-Path $project '_tools\dsh_dedicated.py'
$path = Join-Path $project 'Worldanalysis 专用 DSH.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $python
$shortcut.Arguments = '-B -X utf8 "' + $entry + '" desktop'
$shortcut.WorkingDirectory = $project
$shortcut.Description = '打开 Worldanalysis 专用 DeepSeek Harness'
$icon = Join-Path $project '.runtime\dsh-worldanalysis\runtime\DeepSeek Harness.exe'
if (Test-Path -LiteralPath $icon -PathType Leaf) { $shortcut.IconLocation = "$icon,0" }
$shortcut.Save()
Get-Item -LiteralPath $path
