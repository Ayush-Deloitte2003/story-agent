#!/usr/bin/env bash
# Codespace-only: build Kind + Dapr + Step 4 with MOCK advisors, then enable the
# simulated ops feed and poller through an overlay (checked-in manifests stay off).
# No secrets are read and no model is called. Run from anywhere: bash step4/scripts/codespace-up.sh
set -euo pipefail

CLUSTER=flight-booking
DAPR_CLI_VERSION=1.18.2
DAPR_RUNTIME_VERSION=1.19.0
KIND_VERSION=v0.24.0
cd "$(dirname "$0")/.."
export AGENT_MODE=mock
unset OPENAI_API_KEY || true

mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
arch=$(dpkg --print-architecture)

command -v kubectl >/dev/null || {
  curl -fsSLo "$HOME/.local/bin/kubectl" "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/$arch/kubectl"
  chmod +x "$HOME/.local/bin/kubectl"
}
command -v kind >/dev/null || {
  curl -fsSLo "$HOME/.local/bin/kind" "https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-$arch"
  chmod +x "$HOME/.local/bin/kind"
}
command -v dapr >/dev/null || {
  wget -q https://raw.githubusercontent.com/dapr/cli/master/install/install.sh -O - | /bin/bash -s "$DAPR_CLI_VERSION"
}

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-$CLUSTER"
kubectl get ns dapr-system >/dev/null 2>&1 || dapr init -k --runtime-version "$DAPR_RUNTIME_VERSION" --wait --timeout 600

docker build -t flight-booking:dev .
kind load docker-image flight-booking:dev --name "$CLUSTER"

kubectl apply -f k8s/redis.yaml -f k8s/postgres.yaml
kubectl rollout status deployment/step4-redis --timeout=180s
kubectl rollout status deployment/step4-postgres --timeout=180s
kubectl apply -f k8s/components.yaml -f k8s/apps.yaml
bash ui-demo/enable-simulation.sh
for d in flight-api booking-worker disruption-worker cancellation-worker support-service; do
  kubectl rollout status "deployment/$d" --timeout=300s
done

pkill -f "kubectl port-forward svc/flight-api" 2>/dev/null || true
pkill -f "kubectl port-forward svc/support-service" 2>/dev/null || true
nohup kubectl port-forward svc/flight-api 8000:8000 >/tmp/pf-api.log 2>&1 &
nohup kubectl port-forward svc/support-service 8001:8000 >/tmp/pf-support.log 2>&1 &
for port in 8000 8001; do
  for _ in $(seq 1 30); do
    curl --fail --silent "http://127.0.0.1:$port/healthz" >/dev/null && break
    sleep 2
  done
done

echo
echo "Ready. Open the forwarded port 8000 (private) and add /ui. Support API: port 8001."
echo "Mock advisors, simulated data. Delete the Codespace when finished."
