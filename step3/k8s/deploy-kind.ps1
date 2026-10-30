# Build the image, create a Kind cluster with Dapr, and deploy everything.
# Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File k8s\deploy-kind.ps1
# Needs: Docker Desktop (running), kind, kubectl, dapr CLI, and a .env file.

# "Continue", not "Stop": Windows PowerShell 5.1 turns any stderr line of a
# native program into a fatal error under "Stop", even harmless ones such as
# kind's "No kind clusters found." Each step checks its exit code instead.
$ErrorActionPreference = "Continue"
$cluster = "story"

function Step($title) { Write-Host ""; Write-Host "== $title" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $what" -ForegroundColor Red; exit 1 } }

if (-not (Test-Path .env)) { Write-Host "Create .env (with OPENAI_API_KEY) in the project folder first." -ForegroundColor Red; exit 1 }
foreach ($tool in "docker", "kind", "kubectl", "dapr") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "'$tool' was not found. Install it, then close and reopen VS Code." -ForegroundColor Red; exit 1
    }
}
docker info *> $null; Check "Docker is not running. Start Docker Desktop and wait until it says it is running."

Step "Kind cluster '$cluster'"
$clusters = (kind get clusters 2>&1 | Out-String)
if ($clusters -notmatch "(?m)^$cluster\s*$") {
    kind create cluster --name $cluster; Check "kind create cluster"
} else { Write-Host "already exists" }
kubectl config use-context "kind-$cluster" | Out-Null; Check "kubectl context"

Step "Dapr control plane"
kubectl get namespace dapr-system *> $null
if ($LASTEXITCODE -ne 0) {
    dapr init -k --wait --timeout 600; Check "dapr init -k"
} else { Write-Host "already installed" }

Step "Building image story-agent:dev"
docker build -t story-agent:dev .; Check "docker build"
kind load docker-image story-agent:dev --name $cluster; Check "kind load docker-image"

Step "Secret from .env"
# Parse .env here (trimming spaces, quotes and Windows line endings) rather than
# kubectl --from-env-file, which can keep a trailing carriage return in values.
$literals = @()
foreach ($line in Get-Content .env) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        $literals += "--from-literal=$($Matches[1])=$value"
    }
}
kubectl create secret generic story-agent-env @literals --dry-run=client -o yaml | kubectl apply -f -; Check "secret"

Step "Applying manifests"
kubectl apply -f k8s/redis.yaml -f k8s/statestore.yaml -f k8s/publisher.yaml -f k8s/story-agent.yaml; Check "kubectl apply"
kubectl rollout restart deployment/publisher deployment/story-agent | Out-Null
foreach ($d in "redis", "publisher", "story-agent") {
    kubectl rollout status "deployment/$d" --timeout=300s; Check "rollout of $d (see: kubectl describe pod -l app=$d)"
}

Write-Host ""
Write-Host "Deployed. In a separate terminal run:" -ForegroundColor Green
Write-Host "  kubectl port-forward svc/story-agent 8000:8000"
Write-Host "then open http://127.0.0.1:8000 or run: .venv\Scripts\python scripts\verify_step3.py"
