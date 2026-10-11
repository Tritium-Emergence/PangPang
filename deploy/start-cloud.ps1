param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Data,
    [int]$Port
)
$ErrorActionPreference = "Stop"
# pip install pangpang first.
# PANGPANG_TOKEN must already be in the process environment (at least 24 characters).
# Do not pass the token as an argument. It would show up in the process list.
if (-not $env:PANGPANG_TOKEN) { throw "PANGPANG_TOKEN is required" }
$cloudArgs = @("-m", "pangpang.cli", "cloud", "--data", $Data)
if ($PSBoundParameters.ContainsKey("Port")) {
    $cloudArgs += @("--port", "$Port")
}
& $Python @cloudArgs
exit $LASTEXITCODE
