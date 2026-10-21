param(
    [switch]$Deploy,
    [int]$Port = 8765,
    [string]$Profile = 'pricing-personal',
    [string]$Region = 'ap-south-1',
    [string]$StackName = 'dynamic-pricing-demo',
    [string]$Email = 'akshithreddy.t11@gmail.com'
)
$ErrorActionPreference = 'Stop'
$pricingRoot = Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $pricingRoot
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python 3.12+ is required.' }
    if ($Deploy) {
        $env:SAM_CLI_TELEMETRY = '0'
        $env:__SAM_CLI_APP_DIR = $null
        # Preserve all existing stack parameters, including schedule and eBay settings.
        $pricingRaw = aws cloudformation describe-stacks --stack-name $StackName --profile $Profile --region $Region --query 'Stacks[0].Parameters' --output json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read existing stack. Check the pricing-personal AWS sign-in.' }
        $pricingParameters = @()
        foreach ($pricingEntry in ($pricingRaw | ConvertFrom-Json)) {
            if ($pricingEntry.ParameterValue) { $pricingParameters += "$($pricingEntry.ParameterKey)=$($pricingEntry.ParameterValue)" }
        }
        sam build
        if ($LASTEXITCODE -ne 0) { throw 'SAM build failed.' }
        sam deploy --stack-name $StackName --profile $Profile --region $Region --resolve-s3 --capabilities CAPABILITY_IAM --no-confirm-changeset --no-fail-on-empty-changeset --parameter-overrides @pricingParameters
        if ($LASTEXITCODE -ne 0) { throw 'Cloud update failed. Inspect CloudFormation events.' }
    }
    Write-Host 'Opening the grocery demo. Inspect my AWS deployment, choose a grocery, then Run selected grocery through AWS.'
    Write-Host 'Grocery prices are dated Open Prices observations. Show their dates and same-store history during the presentation.'
    python scripts/dashboard.py --profile $Profile --region $Region --stack $StackName --email $Email --port $Port --open
    if ($LASTEXITCODE -ne 0) { throw 'Demo server stopped with an error. If port is busy, use -Port 8766.' }
} finally {
    Pop-Location
}
