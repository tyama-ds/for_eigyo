<#
.SYNOPSIS
Read-only validation of generated DOCX/PPTX packages with Microsoft's OpenXML SDK.
.DESCRIPTION
Optional QA tool. It does not download/install dependencies or start Office.
Use Windows PowerShell 5.1 with the net46 DLL from OpenXML SDK 2.20.0.
Schema checks complement native Office rendering; they do not compare appearance.
.EXAMPLE
powershell -NoProfile -File tests/validate-office-schema.ps1 -SDKDLLPath C:/tools/DocumentFormat.OpenXml.dll -FixtureRoot tmp/office-qa/fixtures/final-run -ReportPath tmp/office-qa/schema-validation.json
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
  [string]$SDKDLLPath,

  [Parameter(Mandatory = $true)]
  [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
  [string]$FixtureRoot,

  [string]$ReportPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName WindowsBase
Add-Type -Path (Resolve-Path -LiteralPath $SDKDLLPath).Path

$root = (Resolve-Path -LiteralPath $FixtureRoot).Path.TrimEnd('\', '/')
$files = @(Get-ChildItem -LiteralPath $root -File -Recurse |
  Where-Object { $_.Extension -in '.docx', '.pptx' } |
  Sort-Object FullName)
if ($files.Count -eq 0) { throw "No DOCX or PPTX packages found in $root" }

$validator = New-Object DocumentFormat.OpenXml.Validation.OpenXmlValidator([DocumentFormat.OpenXml.FileFormatVersions]::Office2013)
$results = @()
$errorCount = 0
foreach ($file in $files) {
  $package = $null
  try {
    if ($file.Extension -eq '.docx') {
      $package = [DocumentFormat.OpenXml.Packaging.WordprocessingDocument]::Open($file.FullName, $false)
    } else {
      $package = [DocumentFormat.OpenXml.Packaging.PresentationDocument]::Open($file.FullName, $false)
    }
    $errors = @($validator.Validate($package) | ForEach-Object {
      [ordered]@{
        Description = $_.Description
        Path = if ($_.Path) { $_.Path.XPath } else { $null }
        Part = if ($_.Part) { $_.Part.Uri.ToString() } else { $null }
      }
    })
    $errorCount += $errors.Count
    $results += [ordered]@{
      File = $file.FullName.Substring($root.Length).TrimStart('\', '/')
      Errors = $errors
    }
  } finally {
    if ($package) { $package.Dispose() }
  }
}

$report = [ordered]@{
  Validator = 'Microsoft OpenXML SDK'
  SDKVersion = [DocumentFormat.OpenXml.Validation.OpenXmlValidator].Assembly.GetName().Version.ToString()
  Schema = 'Office2013'
  FileCount = $files.Count
  ErrorCount = $errorCount
  Files = $results
}
$json = $report | ConvertTo-Json -Depth 8
if ($ReportPath) { Set-Content -LiteralPath $ReportPath -Value $json -Encoding UTF8 }
Write-Output $json
if ($errorCount -gt 0) { exit 1 }
