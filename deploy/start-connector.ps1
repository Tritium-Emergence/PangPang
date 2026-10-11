param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Data
)
$ErrorActionPreference = "Stop"
# pip install pangpang first. Pair this machine interactively once before scheduling.
# The connector reads its cloud URL, device token and workspace policy from $Data.
& $Python -m pangpang.cli connector --data $Data
exit $LASTEXITCODE
