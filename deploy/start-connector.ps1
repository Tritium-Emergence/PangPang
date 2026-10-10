param(
    [Parameter(Mandatory=$true)][string]$Repo,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Data
)
$ErrorActionPreference = "Stop"
# Pair this machine interactively once before scheduling this script.
# The connector reads its cloud URL, device token and workspace policy from $Data.
Set-Location -LiteralPath $Repo
& $Python -m pangpang.connector --data $Data
exit $LASTEXITCODE
