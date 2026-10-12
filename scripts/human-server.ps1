[CmdletBinding()]
param(
    [string]$Python,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$clientPython = $Python
$prefix = @()
if (-not $clientPython) {
    $clientPython = $env:SUSHIERIC_PYTHON
}
if (-not $clientPython) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        Write-Error 'Python 3.12が必要です。-Pythonで64bit Python 3.12の絶対パスを指定してください。'
        exit 2
    }
    $clientPython = 'py'
    $prefix = @('-3.12')
}
$display = @('--human-readable')
if ($Arguments -contains '--json') {
    $display = @()
}
$previousEncoding = [Console]::OutputEncoding
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $resolvedPython = (Get-Command $clientPython -ErrorAction Stop).Source
    $values = @($prefix) + @('-B', (Join-Path $PSScriptRoot 'human-server.py')) + @($display)
    if ($null -ne $Arguments) { $values += $Arguments }
    $quoted = foreach ($value in $values) {
        '"' + ($value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }
    $info = [Diagnostics.ProcessStartInfo]::new($resolvedPython, ($quoted -join ' '))
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
    $stderr = $process.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
    $process.WaitForExit()
    [void]$stdout.GetAwaiter().GetResult()
    [void]$stderr.GetAwaiter().GetResult()
    $resultCode = $process.ExitCode
    $process.Dispose()
} finally {
    [Console]::OutputEncoding = $previousEncoding
}
exit $resultCode
