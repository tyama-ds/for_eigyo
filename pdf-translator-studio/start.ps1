$ErrorActionPreference = 'Stop'

$nodeCommand = Get-Command node -CommandType Application -ErrorAction SilentlyContinue
$nodeExecutable = if ($nodeCommand) { $nodeCommand.Source } else { $null }

if (-not $nodeExecutable) {
    $bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    if (Test-Path -LiteralPath $bundledNode -PathType Leaf) {
        $nodeExecutable = $bundledNode
    }
}

if (-not $nodeExecutable) {
    throw 'Node.js was not found. Install Node.js 24.5 or newer, then run this script again.'
}

$nodeVersion = & $nodeExecutable -p 'process.versions.node'
if ([version]$nodeVersion -lt [version]'24.5.0') {
    throw "Node.js 24.5 or newer is required (found $nodeVersion)."
}

& $nodeExecutable (Join-Path $PSScriptRoot 'launch.mjs')
exit $LASTEXITCODE
