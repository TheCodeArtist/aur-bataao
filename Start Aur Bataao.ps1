$ErrorActionPreference = "Stop"

$serverExecutable = Join-Path $PSScriptRoot ".venv\Scripts\aur-bataao.exe"
$serverHost = if ($env:AUR_BATAAO_HOST) { $env:AUR_BATAAO_HOST } else { "127.0.0.1" }
$serverPort = if ($env:AUR_BATAAO_PORT) { $env:AUR_BATAAO_PORT } else { "8080" }

# Wildcard listen addresses cannot be opened in a browser. Connect over loopback instead.
$browserHost = if ($serverHost -in @("0.0.0.0", "::")) { "127.0.0.1" } else { $serverHost }
if ($browserHost.Contains(":")) {
    $browserHost = "[$browserHost]"
}
$appUrl = "http://${browserHost}:${serverPort}/"

$serverProcess = $null
try {
    $serverProcess = Start-Process `
        -FilePath $serverExecutable `
        -WorkingDirectory $PSScriptRoot `
        -NoNewWindow `
        -PassThru

    $readyDeadline = [DateTime]::UtcNow.AddSeconds(30)
    $serverReady = $false

    while ([DateTime]::UtcNow -lt $readyDeadline) {
        if ($serverProcess.HasExited) {
            throw "The Aur Bataao server exited before it became available."
        }

        try {
            Invoke-WebRequest -Uri $appUrl -UseBasicParsing -TimeoutSec 1 | Out-Null
            $serverReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }

    if (-not $serverReady) {
        throw "The Aur Bataao server did not become available within 30 seconds."
    }

    Start-Process $appUrl
    $serverProcess.WaitForExit()
    $serverExitCode = $serverProcess.ExitCode
}
finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id
    }
}

exit $serverExitCode
