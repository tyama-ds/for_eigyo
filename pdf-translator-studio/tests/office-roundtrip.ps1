param(
    [Parameter(Mandatory = $true)][string]$FixtureRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot
)

# This QA tool opens generated fixtures read-only in native Office, with macros
# disabled and no document windows. It never opens or saves user documents.
$ErrorActionPreference = 'Stop'
$fixtures = [IO.Path]::GetFullPath($FixtureRoot)
$output = [IO.Path]::GetFullPath($OutputRoot)
[IO.Directory]::CreateDirectory($output) | Out-Null
$records = [System.Collections.Generic.List[object]]::new()
$wordQa = $null
$pptQa = $null
$wordWasRunning = @(Get-Process -Name WINWORD -ErrorAction SilentlyContinue).Count -gt 0
$pptWasRunning = @(Get-Process -Name POWERPNT -ErrorAction SilentlyContinue).Count -gt 0
try {
    $wordQa = New-Object -ComObject Word.Application
    $wordQa.Visible = $false
    $wordQa.DisplayAlerts = 0
    $wordQa.AutomationSecurity = 3
    $pptQa = New-Object -ComObject PowerPoint.Application
    $pptQa.AutomationSecurity = 3
    $pptQa.DisplayAlerts = 1

    foreach ($case in Get-ChildItem -LiteralPath $fixtures -Directory | Sort-Object Name) {
        if (-not (Test-Path -LiteralPath (Join-Path $case.FullName 'metadata.json'))) { continue }
        foreach ($file in Get-ChildItem -LiteralPath $case.FullName -File | Where-Object { $_.Name -match '^export-\d+\.(docx|pptx)$' } | Sort-Object Name) {
            $format = $file.Extension.Substring(1)
            $destination = Join-Path $output (Join-Path $case.Name (Join-Path $file.BaseName $format))
            [IO.Directory]::CreateDirectory($destination) | Out-Null
            $pdfPath = Join-Path $destination 'rendered.pdf'
            $docQa = $null
            $record = [ordered]@{ case = $case.Name; source = $file.FullName; format = $format; pdf = $pdfPath; status = 'failed' }
            try {
                if ($format -eq 'docx') {
                    $record.renderer = 'Microsoft Word'
                    $record.version = [string]$wordQa.Version
                    $record.build = [string]$wordQa.Build
                    # ConfirmConversions false; ReadOnly true; AddToRecentFiles false.
                    $docQa = $wordQa.Documents.Open($file.FullName, $false, $true, $false)
                    $docQa.Repaginate()
                    $record.pageCount = [int]$docQa.ComputeStatistics(2)
                    $record.sectionCount = [int]$docQa.Sections.Count
                    $docQa.ExportAsFixedFormat($pdfPath, 17, $false, 0)
                } else {
                    $record.renderer = 'Microsoft PowerPoint'
                    $record.version = [string]$pptQa.Version
                    $record.build = [string]$pptQa.Build
                    # ReadOnly true; Untitled false; WithWindow false.
                    $docQa = $pptQa.Presentations.Open($file.FullName, -1, 0, 0)
                    $record.pageCount = [int]$docQa.Slides.Count
                    $record.widthPt = [double]$docQa.PageSetup.SlideWidth
                    $record.heightPt = [double]$docQa.PageSetup.SlideHeight
                    # ppSaveAsPDF exports all slides at the deck's physical size.
                    $docQa.SaveAs($pdfPath, 32)
                }
                if (-not (Test-Path -LiteralPath $pdfPath)) { throw 'Native Office did not create the PDF.' }
                $record.status = 'passed'
                Write-Output ("Converted {0}/{1} using {2} {3}: {4} pages" -f $case.Name, $file.Name, $record.renderer, $record.version, $record.pageCount)
            } catch {
                $record.error = $_.Exception.Message
                Write-Output ("FAILED {0}/{1}: {2}" -f $case.Name, $file.Name, $record.error)
            } finally {
                if ($null -ne $docQa) {
                    if ($format -eq 'docx') { $docQa.Close(0) } else { $docQa.Close() }
                    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($docQa)
                }
                $records.Add([pscustomobject]$record)
                ConvertTo-Json -InputObject @($records.ToArray()) -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'office-conversion.json') -Encoding UTF8
            }
        }
    }
} finally {
    if ($null -ne $wordQa) {
        if (-not $wordWasRunning) { $wordQa.Quit() }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($wordQa)
    }
    if ($null -ne $pptQa) {
        if (-not $pptWasRunning) { $pptQa.Quit() }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($pptQa)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
if (@($records | Where-Object { $_.status -ne 'passed' }).Count -gt 0) { exit 1 }
if ($records.Count -eq 0) { throw 'No exported Office fixtures were found.' }
