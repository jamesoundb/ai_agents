#!/usr/bin/env bash
# plant.sh CONTEXT [FIXTURE_DIR] -- plant every test case on a throwaway cluster and (optionally) save the
# sanitized snapshot that tests/fixture is made of. Refuses any context but a minikube/kind one.
#   tests/plant.sh agents-test                 # plant only
#   tests/plant.sh agents-test tests/fixture   # plant, wait for the failure states, save the fixture
set -euo pipefail
CTX="${1:?usage: plant.sh CONTEXT [FIXTURE_DIR]}"; OUT="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(kubectl config view -o jsonpath="{.contexts[?(@.name=='$CTX')].context.cluster}")" in
  agents-test|minikube|kind-*) ;;
  *) echo "plant.sh: $CTX is not a throwaway minikube/kind cluster; refusing" >&2; exit 2 ;;
esac
K=(kubectl --context "$CTX")
"${K[@]}" apply -f "$HERE/faults.yaml" -f "$HERE/faults-2.yaml" -f "$HERE/faults-3.yaml" 2>&1 | grep -v memcache.go || true
"${K[@]}" wait --for condition=established crd/testenvs.kg.example.com crd/applications.argoproj.io \
  crd/helmreleases.helm.toolkit.fluxcd.io --timeout=60s
"${K[@]}" apply -f "$HERE/testenvs.yaml" -f "$HERE/gitops.yaml" 2>&1 | grep -v memcache.go || true
# Released PV: bind a claim to it, then delete the claim (reclaim policy Retain keeps the PV). Only while the
# PV is still Available, so a second run leaves it Released.
if [ "$("${K[@]}" get pv kg-retained -o jsonpath='{.status.phase}' 2>/dev/null)" = Available ]; then
  "${K[@]}" apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: kg-claim, namespace: kg-faults}
spec: {accessModes: [ReadWriteOnce], storageClassName: manual, volumeName: kg-retained, resources: {requests: {storage: 1Gi}}}
EOF
  "${K[@]}" -n kg-faults wait --for=jsonpath='{.status.phase}'=Bound pvc/kg-claim --timeout=60s
  "${K[@]}" -n kg-faults delete pvc kg-claim --wait=false
fi
# stuck rollout: revision 1 must be Available before revision 2 (an image that cannot be pulled) goes in
"${K[@]}" -n kg-faults rollout status deploy/checkout --timeout=120s
"${K[@]}" -n kg-faults set image deploy/checkout app=registry.invalid/team/checkout:2.0 2>&1 | grep -v memcache.go || true
# Helm releases from tests/helm/kg-shop: kg-shop installs, then an upgrade to an unpullable image fails;
# kg-cart installs, then an upgrade is killed mid-flight, which leaves Helm's pending-upgrade lock.
H=(helm --kube-context "$CTX" -n kg-helm)
if ! "${H[@]}" status kg-shop >/dev/null 2>&1; then
  "${H[@]}" install kg-shop "$HERE/helm/kg-shop" --wait --timeout 120s
  "${H[@]}" upgrade kg-shop "$HERE/helm/kg-shop" --set image=registry.invalid/team/shop:2.0 --wait --timeout 30s || true
fi
if ! "${H[@]}" status kg-cart >/dev/null 2>&1; then
  "${H[@]}" install kg-cart "$HERE/helm/kg-shop" --set app=kg-cart --wait --timeout 120s
  "${H[@]}" upgrade kg-cart "$HERE/helm/kg-shop" --set app=kg-cart --set image=registry.invalid/team/cart:2.0 \
    --wait --timeout 300s & pid=$!
  sleep 8; kill -9 "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
fi
# node failure: stop the kubelet of a minikube profile's second node (minikube node add -p PROFILE)
if "${K[@]}" get node "$CTX-m02" >/dev/null 2>&1 && command -v minikube >/dev/null; then
  "${K[@]}" -n kg-faults rollout status deploy/on-m02 --timeout=120s || true
  minikube ssh -p "$CTX" -n "$CTX-m02" -- sudo systemctl stop kubelet
fi
if [ -n "$OUT" ]; then
  echo "plant.sh: waiting 330s for crash loops, probe failures, rollout deadlines and the node's pod eviction"
  sleep 330
  rm -rf "$OUT"
  "$HERE/../scripts/run.sh" snapshot --context "$CTX" --db "$(mktemp -d)/fixture.db" --save "$OUT" \
    -n kg-faults -n kg-quota -n kg-preview-123 -n kg-net -n kg-helm -n kg-gitops -n kube-system \
    --exclude clusterroles.rbac.authorization.k8s.io --exclude clusterrolebindings.rbac.authorization.k8s.io \
    --exclude flowschemas.flowcontrol.apiserver.k8s.io --exclude prioritylevelconfigurations.flowcontrol.apiserver.k8s.io
  if grep -rlE 'hunter2|aHVudGVyMg' "$OUT"; then echo "plant.sh: secret value in the fixture" >&2; exit 1; fi
fi
