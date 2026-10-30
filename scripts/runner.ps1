# One entry point for setup, verification and demos of Steps 1-3.
# Called by run.cmd:   .\run <action>      (see README.md for the list)

param([string]$Action = "help")

# "Continue": Windows PowerShell 5.1 treats a native program's stderr as fatal under "Stop".
$ErrorActionPreference = "Continue"
$Root  = Split-Path -Parent $PSScriptRoot
$Py    = Join-Path $Root ".venv\Scripts\python.exe"
$Step1 = Join-Path $Root "step1"
$Step2 = Join-Path $Root "step2"
$Step3 = Join-Path $Root "step3"
$DaprBin = Join-Path $env:USERPROFILE ".dapr\bin"
$DaprCli = Join-Path $Root "tools\dapr"   # Dapr CLI downloaded here when winget is not allowed

function Say($text, $color = "Cyan") { Write-Host ""; Write-Host "== $text" -ForegroundColor $color }
function Fail($text) { Write-Host ""; Write-Host "FAILED: $text" -ForegroundColor Red; exit 1 }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (Test-Path "C:\dapr") { $env:Path += ";C:\dapr" }
    if (Test-Path $DaprCli) { $env:Path += ";$DaprCli" }
}
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Listening($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }
function Free-Port($port) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
function Need-Setup { if (-not (Test-Path $Py)) { Fail "Run '.\run setup' first." } }

# Open a new PowerShell window that keeps running a command (so its log stays visible).
function Open-Window($title, $dir, $command) {
    $full = "`$host.UI.RawUI.WindowTitle = '$title'; Set-Location '$dir'; $command"
    Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $full | Out-Null
}
function Wait-Http($url, $seconds = 180) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true } catch { Start-Sleep 2 }
    }
    return $false
}
function Find-Python {
    foreach ($v in "3.13", "3.12", "3.11") {
        if (Has "py") { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return ,@("py", "-$v") } }
    }
    if (Has "python") {
        & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return ,@("python") }
    }
    return $null
}

# ---------------------------------------------------------------- setup
function Do-Setup {
    Say "1/5 Python 3.11 or newer"
    $python = Find-Python
    if (-not $python) {
        Write-Host "Installing Python 3.12 with winget..."
        winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
        Refresh-Path
        $python = Find-Python
        if (-not $python) { Fail "Python was installed but is not visible yet. Close VS Code, reopen it, and run '.\run setup' again." }
    }
    Write-Host "Using: $($python -join ' ')"

    Say "2/5 Virtual environment and packages (a few minutes the first time)"
    if (-not (Test-Path $Py)) {
        $exe = $python[0]; $pyArgs = @($python | Select-Object -Skip 1) + @("-m", "venv", (Join-Path $Root ".venv"))
        & $exe @pyArgs
        if (-not (Test-Path $Py)) { Fail "creating .venv" }
    }
    & $Py -m pip install --disable-pip-version-check -q -r (Join-Path $Step3 "requirements.txt") httpx
    if ($LASTEXITCODE -ne 0) { Fail "pip install (check your internet connection)" }
    Write-Host "Packages installed."

    Say "3/5 OpenAI key and .env files"
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        Write-Host "Using the existing .env (delete it and run setup again to enter a new key)."
    } else {
        $secure = Read-Host "Paste your OpenAI API key (input is hidden), then press Enter" -AsSecureString
        $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
        if (-not $key.StartsWith("sk-")) { Fail "That does not look like an OpenAI key (it should start with sk-)." }
        $template = Get-Content (Join-Path $Step3 ".env.example")
        $lines = $template | ForEach-Object { if ($_ -match '^OPENAI_API_KEY=') { "OPENAI_API_KEY=$key" } else { $_ } }
        # UTF-8 WITHOUT a byte-order mark: python-dotenv would misread the first key after a BOM.
        [IO.File]::WriteAllText($envFile, ($lines -join "`n") + "`n", (New-Object Text.UTF8Encoding $false))
        Write-Host ".env created."
    }
    foreach ($dir in $Step1, $Step2, $Step3) { Copy-Item $envFile (Join-Path $dir ".env") -Force }
    Write-Host "Copied .env into step1, step2 and step3."

    Say "4/5 Dapr CLI"
    Refresh-Path
    if (-not (Has "dapr")) {
        # Download the CLI into the project (no winget, no admin rights needed).
        Write-Host "Downloading the Dapr CLI from GitHub..."
        New-Item -ItemType Directory -Force $DaprCli | Out-Null
        $zip = Join-Path $DaprCli "dapr.zip"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest "https://github.com/dapr/cli/releases/download/v1.18.2/dapr_windows_amd64.zip" -OutFile $zip -UseBasicParsing
            Expand-Archive $zip -DestinationPath $DaprCli -Force
            Remove-Item $zip -ErrorAction SilentlyContinue
        } catch { Fail "Could not download the Dapr CLI ($($_.Exception.Message)). Is github.com reachable from this laptop?" }
        Refresh-Path
        if (-not (Has "dapr")) { Fail "Dapr CLI download finished but dapr.exe was not found in $DaprCli" }
    }
    dapr --version

    Say "5/5 Dapr runtime (slim mode: no Docker needed for Steps 1-3)"
    if (-not (Test-Path (Join-Path $DaprBin "daprd.exe"))) {
        dapr init --slim
        if ($LASTEXITCODE -ne 0) { Fail "dapr init --slim" }
    } else { Write-Host "Already installed in $DaprBin" }

    Say "Setup complete. Next: .\run step1-verify" "Green"
}

# ---------------------------------------------------------------- shared
function Stop-All {
    Refresh-Path
    if (Has "dapr") { dapr stop --app-id story-agent 2>$null | Out-Null }
    foreach ($p in 8000, 8765, 3500, 50001) { Free-Port $p }
}
function Start-DaprServices {
    if (-not (Listening 50005)) {
        Open-Window "Dapr placement" $Root "& '$DaprBin\placement.exe' --port 50005 --healthz-port 8093 --metrics-port 9094"
    }
    if (-not (Listening 50006)) {
        $data = Join-Path $env:USERPROFILE ".dapr\scheduler-data"
        Open-Window "Dapr scheduler" $Root "& '$DaprBin\scheduler.exe' --port 50006 --etcd-data-dir '$data' --healthz-port 8091 --metrics-port 9092 --override-broadcast-host-port localhost:50006"
    }
    $deadline = (Get-Date).AddSeconds(60)
    while (-not ((Listening 50005) -and (Listening 50006))) {
        if ((Get-Date) -gt $deadline) { Fail "Dapr placement/scheduler did not start; check their windows." }
        Start-Sleep 2
    }
}
function Start-Step3App {
    Refresh-Path
    Open-Window "Step 3 app (Dapr)" $Step3 "dapr run --app-id story-agent --app-port 8000 --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path .\dapr\components --placement-host-address localhost:50005 --scheduler-host-address localhost:50006 -- '$Py' -m uvicorn app.main:app --port 8000"
    Write-Host "Waiting for the app (up to 3 minutes)..."
    if (-not (Wait-Http "http://127.0.0.1:8000/")) { Fail "The Step 3 app did not start; check the 'Step 3 app (Dapr)' window." }
}

# ---------------------------------------------------------------- actions
switch ($Action) {
    "setup" { Do-Setup }

    "step1-verify" { Need-Setup; Set-Location $Step1; & $Py scripts\verify_step1.py real }
    "step1-demo" {
        Need-Setup; Stop-All
        Open-Window "Step 1 app" $Step1 "& '$Py' -m uvicorn app.main:app --port 8000"
        if (Wait-Http "http://127.0.0.1:8000/") { Start-Process "http://127.0.0.1:8000" } else { Fail "Step 1 app did not start; check its window." }
        Say "Step 1 is running. In the browser: Write story -> Reject with feedback -> Approve. Stop with: .\run stop" "Green"
    }

    "step2-verify" { Need-Setup; Set-Location $Step2; & $Py scripts\verify_step2.py real }
    "step2-demo" {
        Need-Setup; Stop-All
        Open-Window "Step 2 MCP server" $Step2 "& '$Py' mcp_server\publisher.py"
        Open-Window "Step 2 app" $Step2 "& '$Py' -m uvicorn app.main:app --port 8000"
        if (Wait-Http "http://127.0.0.1:8000/") { Start-Process "http://127.0.0.1:8000" } else { Fail "Step 2 app did not start; check its window." }
        Say "Step 2 is running. Write story -> Approve: publishing fails (see the MCP server window). Stop with: .\run stop" "Green"
    }

    "step3-demo" {
        Need-Setup; Stop-All; Start-DaprServices
        Open-Window "Step 3 MCP server" $Step3 "& '$Py' mcp_server\publisher.py"
        Start-Step3App
        Start-Process "http://127.0.0.1:8000"
        Say "Step 3 is running. Write story -> Approve: watch the MCP server window retry until attempt 8. Stop with: .\run stop" "Green"
    }
    "step3-verify" {
        Need-Setup
        if (-not (Wait-Http "http://127.0.0.1:8000/" 5)) { Fail "Start Step 3 first: .\run step3-demo" }
        Set-Location $Step3; & $Py scripts\verify_step3.py
    }
    "step3-restart-test" {
        Need-Setup
        if (-not (Wait-Http "http://127.0.0.1:8000/" 5)) { Fail "Start Step 3 first: .\run step3-demo" }
        Set-Location $Step3
        Say "Part 1: leave a story waiting for approval"
        & $Py scripts\verify_step3.py start
        if ($LASTEXITCODE -ne 0) { Fail "could not create the waiting story" }
        Say "Stopping the app and its Dapr sidecar"
        Refresh-Path; dapr stop --app-id story-agent 2>$null | Out-Null
        foreach ($p in 8000, 3500, 50001) { Free-Port $p }
        Start-Sleep 5
        Write-Host "App is down: $(-not (Wait-Http 'http://127.0.0.1:8000/' 3))"
        Say "Starting the app again"
        Start-Step3App
        Say "Part 2: the story should still be waiting; approve it"
        Set-Location $Step3; & $Py scripts\verify_step3.py resume
    }
    "step3-kind" {
        Need-Setup; Stop-All; Refresh-Path
        Set-Location $Step3
        & powershell -ExecutionPolicy Bypass -File k8s\deploy-kind.ps1
    }
    "kind-tools" {
        winget install --id Docker.DockerDesktop -e --accept-source-agreements --accept-package-agreements
        winget install --id Kubernetes.kind -e --accept-source-agreements --accept-package-agreements
        winget install --id Kubernetes.kubectl -e --accept-source-agreements --accept-package-agreements
        Say "Installed. Restart Windows, open Docker Desktop and wait until it is running, then: .\run step3-kind" "Green"
    }

    "stop" { Stop-All; Say "Stopped the apps and MCP servers (the Dapr placement/scheduler windows can stay open)." "Green" }

    default {
        Write-Host "Usage: .\run <action>"
        Write-Host "  setup               install everything, ask for your OpenAI key once"
        Write-Host "  step1-verify        Step 1 automated checks      step1-demo   open Step 1 in the browser"
        Write-Host "  step2-verify        Step 2 automated checks      step2-demo   open Step 2 in the browser"
        Write-Host "  step3-demo          start Step 3 under Dapr and open the browser"
        Write-Host "  step3-verify        Step 3 checks (needs step3-demo running)"
        Write-Host "  step3-restart-test  durability test: stops and restarts the app automatically"
        Write-Host "  kind-tools          install Docker Desktop, kind, kubectl (then restart Windows)"
        Write-Host "  step3-kind          deploy Step 3 to a Kind cluster"
        Write-Host "  stop                stop everything started by the demos"
    }
}
