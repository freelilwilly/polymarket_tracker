$ErrorActionPreference = 'Continue'

Set-Location 'C:/workspace/polymarket_tracker'
$Host.UI.RawUI.WindowTitle = 'Polymarket Watchdog'

$CheckIntervalSeconds = 300
$MaxWorkbookStaleMinutes = 12

function Start-Tracker80 {
    $cmd = @'
$env:DRY_RUN='true'
$env:V2_EXCEL_MODE='true'
$env:V2_CANDIDATE_LIMIT='300'
$env:V2_TAIL_BASE_RISK_PCT='0.01'
$env:V2_TAIL_MAX_MARKET_NOTIONAL_PCT='0.75'
$env:V2_TAIL_MAX_ACCOUNT_NOTIONAL_PCT='0.75'
$env:V2_POLL_COUNT_ONLY='true'
$env:V2_PROFILE_LABEL='80'
$env:V2_MIN_WIN_RATE='80'
$env:V2_TOP_USERS='10'
$env:V2_EXCEL_WORKBOOK='tail_performance.xlsx'
Set-Location 'C:/workspace/polymarket_tracker'
& 'C:/workspace/polymarket_tracker/.venv/Scripts/python.exe' -u main.py
'@
    Start-Process -FilePath powershell -ArgumentList @('-NoExit', '-Command', $cmd) | Out-Null
}

function Start-Tracker75 {
    $cmd = @'
$env:DRY_RUN='true'
$env:V2_EXCEL_MODE='true'
$env:V2_CANDIDATE_LIMIT='300'
$env:V2_TAIL_BASE_RISK_PCT='0.01'
$env:V2_TAIL_MAX_MARKET_NOTIONAL_PCT='0.75'
$env:V2_TAIL_MAX_ACCOUNT_NOTIONAL_PCT='0.75'
$env:V2_POLL_COUNT_ONLY='true'
$env:V2_PROFILE_LABEL='75'
$env:V2_MIN_WIN_RATE='75'
$env:V2_TOP_USERS='99999'
$env:V2_EXCEL_WORKBOOK='tail_performance_75.xlsx'
Set-Location 'C:/workspace/polymarket_tracker'
& 'C:/workspace/polymarket_tracker/.venv/Scripts/python.exe' -u main.py
'@
    Start-Process -FilePath powershell -ArgumentList @('-NoExit', '-Command', $cmd) | Out-Null
}

function Start-Reporter80 {
    $cmd = @'
Set-Location 'C:/workspace/polymarket_tracker'
& 'C:/workspace/polymarket_tracker/.venv/Scripts/python.exe' -u hourly_reporter.py --workbook tail_performance.xlsx --label 80 --warn-roi -25 --interval-seconds 3600
'@
    Start-Process -FilePath powershell -ArgumentList @('-NoExit', '-Command', $cmd) | Out-Null
}

function Start-Reporter75 {
    $cmd = @'
Set-Location 'C:/workspace/polymarket_tracker'
& 'C:/workspace/polymarket_tracker/.venv/Scripts/python.exe' -u hourly_reporter.py --workbook tail_performance_75.xlsx --label 75 --warn-roi -25 --interval-seconds 3600
'@
    Start-Process -FilePath powershell -ArgumentList @('-NoExit', '-Command', $cmd) | Out-Null
}

function Test-ReporterRunning([string]$WorkbookName, [string]$Label) {
    $needleA = "hourly_reporter.py --workbook $WorkbookName"
    $needleB = "--label $Label"
    $proc = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match 'python' -and $_.CommandLine -and
        $_.CommandLine.Contains($needleA) -and $_.CommandLine.Contains($needleB)
    }
    return ($proc.Count -gt 0)
}

function Test-WorkbookFresh([string]$WorkbookName, [int]$MaxAgeMinutes) {
    if (-not (Test-Path $WorkbookName)) {
        return $false
    }

    $last = (Get-Item $WorkbookName).LastWriteTime
    $age = (New-TimeSpan -Start $last -End (Get-Date)).TotalMinutes
    return ($age -le $MaxAgeMinutes)
}

function Get-MainProcessCount {
    $proc = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match 'python' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains('main.py')
    }
    return $proc.Count
}

Write-Output "[$((Get-Date).ToString('u'))] Watchdog started. Interval=$CheckIntervalSeconds sec, stale limit=$MaxWorkbookStaleMinutes min"

while ($true) {
    try {
        if (-not (Test-ReporterRunning -WorkbookName 'tail_performance.xlsx' -Label '80')) {
            Write-Output "[$((Get-Date).ToString('u'))] Reporter 80 missing -> relaunching"
            Start-Reporter80
        }

        if (-not (Test-ReporterRunning -WorkbookName 'tail_performance_75.xlsx' -Label '75')) {
            Write-Output "[$((Get-Date).ToString('u'))] Reporter 75 missing -> relaunching"
            Start-Reporter75
        }

        $mainCount = Get-MainProcessCount
        if ($mainCount -lt 4) {
            if (-not (Test-WorkbookFresh -WorkbookName 'tail_performance.xlsx' -MaxAgeMinutes $MaxWorkbookStaleMinutes)) {
                Write-Output "[$((Get-Date).ToString('u'))] Tracker set below expected count and workbook 80 stale/missing -> relaunching tracker 80"
                Start-Tracker80
            }

            if (-not (Test-WorkbookFresh -WorkbookName 'tail_performance_75.xlsx' -MaxAgeMinutes $MaxWorkbookStaleMinutes)) {
                Write-Output "[$((Get-Date).ToString('u'))] Tracker set below expected count and workbook 75 stale/missing -> relaunching tracker 75"
                Start-Tracker75
            }
        }

        $mains = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match 'python' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains('main.py')
        }
        $reporters = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match 'python' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains('hourly_reporter.py')
        }

        Write-Output "[$((Get-Date).ToString('u'))] heartbeat mains=$($mains.Count) reporters=$($reporters.Count)"
    }
    catch {
        Write-Output "[$((Get-Date).ToString('u'))] watchdog error: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $CheckIntervalSeconds
}
