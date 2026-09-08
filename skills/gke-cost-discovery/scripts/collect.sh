#!/usr/bin/env bash
# collect.sh — read-only evidence collection for GKE cost discovery.
# Every step is optional: missing tools, credentials or variables produce a MISSING-* note instead
# of a failure, so the analyzer can report what is still needed.
#
#   collect.sh [OUT_DIR]            (default: ./discovery/<timestamp>)
# Environment (see ../gke-cost-discovery.env.example):
#   GCP_PROJECT, GKE_CLUSTER, GKE_LOCATION      cluster identity (required for gcloud steps)
#   KUBE_CONTEXT                                kubectl context (default: current)
#   BUILD_NAMESPACES                            comma list of namespaces used by builds (optional)
#   DISCOVERY_DAYS                              look-back window for metrics/logs (default 14)
#   TEAMCITY_URL, TEAMCITY_TOKEN                TeamCity REST (optional)
set -uo pipefail
OUT="${1:-./discovery/$(date +%Y%m%d-%H%M%S)}"
DAYS="${DISCOVERY_DAYS:-14}"
mkdir -p "$OUT"
note() { printf '%s\n' "$2" > "$OUT/MISSING-$1"; echo "  skip: $1 ($2)"; }
ok() { echo "  ok:   $1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "collecting into $OUT (window ${DAYS}d)"
cat > "$OUT/meta.json" <<JSON
{"collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "days": $DAYS, "project": "${GCP_PROJECT:-}", "cluster": "${GKE_CLUSTER:-}",
 "location": "${GKE_LOCATION:-}", "build_namespaces": "${BUILD_NAMESPACES:-}"}
JSON

# ---- gcloud: cluster and node pools ---------------------------------------------------------
if have gcloud && [ -n "${GCP_PROJECT:-}" ] && [ -n "${GKE_CLUSTER:-}" ] && [ -n "${GKE_LOCATION:-}" ]; then
  if timeout 120 gcloud container clusters describe "$GKE_CLUSTER" --location "$GKE_LOCATION" --project "$GCP_PROJECT" --format json > "$OUT/cluster.json" 2>"$OUT/cluster.err"; then
    ok "cluster.json (gcloud container clusters describe)"; rm -f "$OUT/cluster.err"
  else note "cluster" "gcloud describe failed: $(head -c 300 "$OUT/cluster.err")"; fi
else
  note "cluster" "needs gcloud + GCP_PROJECT, GKE_CLUSTER, GKE_LOCATION"
fi

# ---- kubectl snapshots ----------------------------------------------------------------------
if have kubectl; then
  K=(kubectl --request-timeout=15s); [ -n "${KUBE_CONTEXT:-}" ] && K+=(--context "$KUBE_CONTEXT")
  CTX="${KUBE_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo none)}"
  echo "  kubectl context: $CTX (read-only get/top; set KUBE_CONTEXT to choose another)"
  if [ "$CTX" != none ] && timeout 30 "${K[@]}" get nodes -o json > "$OUT/nodes.json" 2>/dev/null; then
    ok "nodes.json"
    "${K[@]}" get pods -A -o json > "$OUT/pods.json" 2>/dev/null && ok "pods.json"
    "${K[@]}" get deployments,statefulsets,daemonsets,jobs,cronjobs -A -o json > "$OUT/workloads.json" 2>/dev/null && ok "workloads.json"
    "${K[@]}" get pdb -A -o json > "$OUT/pdb.json" 2>/dev/null && ok "pdb.json"
    "${K[@]}" get resourcequota,limitrange -A -o json > "$OUT/quotas.json" 2>/dev/null && ok "quotas.json"
    "${K[@]}" get events -A -o json > "$OUT/events.json" 2>/dev/null && ok "events.json"
    "${K[@]}" top pods -A --containers --no-headers > "$OUT/top.txt" 2>/dev/null && ok "top.txt (point-in-time usage)" || note "top" "metrics-server not available"
  else
    rm -f "$OUT/nodes.json"; note "kubectl" "kubectl context '$CTX' unreachable or unset (run: gcloud container clusters get-credentials ...)"
  fi
else
  note "kubectl" "kubectl not installed"
fi

# ---- Cloud Monitoring time series (REST) -------------------------------------------------------
if have gcloud && have curl && [ -n "${GCP_PROJECT:-}" ] && [ -n "${GKE_CLUSTER:-}" ]; then
  TOKEN="$(timeout 60 gcloud auth print-access-token 2>/dev/null)"
  if [ -n "$TOKEN" ]; then
    END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; START="$(date -u -d "-${DAYS} days" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-${DAYS}d +%Y-%m-%dT%H:%M:%SZ)"
    ts() {  # ts <name> <metric> <aligner> <reducer> <groupby>
      local name="$1" metric="$2" aligner="$3" reducer="$4" group="$5" page="" first=1
      : > "$OUT/ts_$name.json"
      while :; do
        resp="$(timeout 120 curl -sS -G "https://monitoring.googleapis.com/v3/projects/$GCP_PROJECT/timeSeries" \
          -H "Authorization: Bearer $TOKEN" \
          --data-urlencode "filter=metric.type=\"$metric\" AND resource.labels.cluster_name=\"$GKE_CLUSTER\"" \
          --data-urlencode "interval.startTime=$START" --data-urlencode "interval.endTime=$END" \
          --data-urlencode "aggregation.alignmentPeriod=3600s" --data-urlencode "aggregation.perSeriesAligner=$aligner" \
          --data-urlencode "aggregation.crossSeriesReducer=$reducer" \
          $(for g in ${group//,/ }; do printf -- '--data-urlencode aggregation.groupByFields=%s ' "$g"; done) \
          ${page:+--data-urlencode "pageToken=$page"})"
        printf '%s\n' "$resp" >> "$OUT/ts_$name.json"
        page="$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("nextPageToken",""))' 2>/dev/null)"
        [ -n "$page" ] || break
      done
      if grep -q '"error"' "$OUT/ts_$name.json"; then note "ts_$name" "$(head -c 200 "$OUT/ts_$name.json")"; else ok "ts_$name.json"; fi
    }
    G="resource.labels.namespace_name,resource.labels.container_name,metadata.system_labels.top_level_controller_name"
    ts cpu_req_util  kubernetes.io/container/cpu/request_utilization    ALIGN_PERCENTILE_95 REDUCE_MEAN "$G"
    ts mem_req_util  kubernetes.io/container/memory/request_utilization ALIGN_MAX           REDUCE_MAX  "$G"   # memory: peak, not p95 (OOM kills)
    ts cpu_req_cores kubernetes.io/container/cpu/request_cores           ALIGN_MEAN          REDUCE_SUM  "$G"
    ts mem_req_bytes kubernetes.io/container/memory/request_bytes        ALIGN_MEAN          REDUCE_SUM  "$G"
    ts node_req_cores kubernetes.io/container/cpu/request_cores          ALIGN_MEAN          REDUCE_SUM  "resource.labels.node_name"
    ts node_req_bytes kubernetes.io/container/memory/request_bytes       ALIGN_MEAN          REDUCE_SUM  "resource.labels.node_name"
    ts node_alloc_cores kubernetes.io/node/cpu/allocatable_cores         ALIGN_MEAN          REDUCE_SUM  "resource.labels.node_name"
    ts node_alloc_bytes kubernetes.io/node/memory/allocatable_bytes      ALIGN_MEAN          REDUCE_SUM  "resource.labels.node_name"
    ts node_cpu_util kubernetes.io/node/cpu/allocatable_utilization      ALIGN_MEAN          REDUCE_MEAN "resource.labels.node_name"
    # cluster autoscaler visibility: why nodes were not removed
    if timeout 180 gcloud logging read "resource.type=\"k8s_cluster\" AND resource.labels.cluster_name=\"$GKE_CLUSTER\" AND logName:\"cluster-autoscaler-visibility\"" \
         --project "$GCP_PROJECT" --freshness "${DAYS}d" --limit 5000 --format json > "$OUT/autoscaler_logs.json" 2>"$OUT/autoscaler.err"; then
      ok "autoscaler_logs.json"; rm -f "$OUT/autoscaler.err"
    else note "autoscaler_logs" "gcloud logging read failed: $(head -c 200 "$OUT/autoscaler.err")"; fi
  else
    note "monitoring" "no gcloud access token (run: gcloud auth login / activate-service-account)"
  fi
else
  note "monitoring" "needs gcloud, curl, GCP_PROJECT and GKE_CLUSTER"
fi

# ---- TeamCity REST -------------------------------------------------------------------------------
if have curl && [ -n "${TEAMCITY_URL:-}" ] && [ -n "${TEAMCITY_TOKEN:-}" ]; then
  SINCE="$(date -u -d "-${DAYS} days" +%Y%m%dT%H%M%S%z 2>/dev/null || date -u -v-${DAYS}d +%Y%m%dT%H%M%S%z)"
  H=(-H "Authorization: Bearer $TEAMCITY_TOKEN" -H "Accept: application/json")
  if timeout 120 curl -sS "${H[@]}" "$TEAMCITY_URL/app/rest/builds?locator=sinceDate:${SINCE},state:finished,count:5000&fields=count,build(id,buildTypeId,status,queuedDate,startDate,finishDate,agent(name))" > "$OUT/teamcity_builds.json"; then
    ok "teamcity_builds.json"
  else note "teamcity_builds" "TeamCity builds request failed"; fi
  timeout 120 curl -sS "${H[@]}" "$TEAMCITY_URL/app/rest/buildTypes?fields=buildType(id,name,projectName,steps(step(name,type,properties(property(name,value)))),settings(property(name,value)))" > "$OUT/teamcity_buildtypes.json" && ok "teamcity_buildtypes.json"
  timeout 60 curl -sS "${H[@]}" "$TEAMCITY_URL/app/rest/cloud/profiles?fields=cloudProfile(id,name,cloudProviderId,images(cloudImage(id,name)))" > "$OUT/teamcity_cloud.json" 2>/dev/null && ok "teamcity_cloud.json (cloud profiles; pod templates live here or in VCS)"
else
  note "teamcity" "needs TEAMCITY_URL and TEAMCITY_TOKEN (read-only token)"
fi
echo "done: $OUT"
ls "$OUT" | grep -c '^MISSING-' | xargs -I{} echo "{} input(s) still missing; run analyze.py for the report anyway"
