# Brrrtgate observability

HTTP correlation and monitoring use metadata only. They are not prompt logging or distributed tracing.

## Correlation and logging

Every HTTP attempt receives a server-generated UUID in `X-Request-ID`, including validation, authentication and body-size errors. Inbound request IDs are ignored, preventing attacker-controlled log values. The same value appears in the `gateway.http` JSON completion event and `request.state.request_id`. Inference accounting reservation identifiers appear separately as `X-Reservation-ID`; provider correlation, when available, uses `X-Provider-Request-ID`.

The structured event contains only `event`, `request_id`, registered route template, bounded HTTP method/status, duration in seconds and `response_complete`. It does not contain raw paths, queries, client IPs, authorization, cookies, user IDs, prompts, model outputs or exception details. The supplied CLI/container configures the `gateway.http` logger at INFO with a message-only formatter and disables Uvicorn raw access logs. If invoking Uvicorn directly, use `--no-access-log` and configure this logger explicitly. Do not enable verbose HTTP client logs or standard raw access logs: reverse proxies, Uvicorn and tracing agents can independently expose query strings, including OIDC authorization codes. Apply the same exclusions throughout your log pipeline.

Duration covers the full HTTP lifetime, including streaming, not merely time to first token. Interrupted streams retain their sent HTTP status but have `response_complete:false`; dashboards based only on 5xx rates will miss these. Cancellation/disconnect is not retried and can have status 499 if no headers were sent. Requests terminated before a response cannot receive a response header. Completed inference response headers include the separate accounting identifier for support and reconciliation.

## Prometheus

`GET /metrics` requires the metrics bearer credential. Store it in your monitoring platform's secret store, not in dashboards or committed scrape configuration. Restrict metrics network access. Scrape each replica separately, then sum across replicas; do not scrape only a load-balanced service and assume it represents every process. Use one Uvicorn process per container: multiprocess Prometheus aggregation is not configured here.

Metrics:
- `gateway_http_requests_total{route,method,status}`: HTTP attempts, including errors and monitoring requests.
- `gateway_http_request_duration_seconds{route,method,status}`: total HTTP duration histogram.
- `gateway_inference_total{endpoint,outcome}`: inference outcome counters.
- `gateway_upstream_seconds{endpoint}`: upstream connection latency histogram.

Labels never use request IDs, raw paths/queries, users, model aliases or provider URLs. Unknown paths are `unmatched`, unknown methods are `OTHER`, static files share `/admin/static/*`. Registered dynamic routes use templates, such as `/admin/api/users/{user_id}`. Request IDs belong in logs, not metric labels. Metrics reset when a process restarts; use rates/increases, not raw counter comparisons.

[Example alert rules](../deploy/monitoring/prometheus-alerts.yaml) and [Grafana dashboard](../deploy/monitoring/grafana-dashboard.json) are starting points. Set your actual Prometheus job label (`thunderbolt` in examples), provision the datasource, validate queries against your deployment and tune thresholds to workload/SLOs. Streaming requests naturally have longer durations; the latency alert intentionally targets admin/auth routes. These files do not configure a Prometheus server, credential distribution, paging destination, retention or an availability SLA.

This release has no OpenTelemetry span exporter, per-tenant dashboards, immutable audit export or automated alert delivery. Database usage/audit records remain distinct from operational logs; apply access control and retention to both.

## Overload signals

- `gateway_inflight_requests{pool}`: current HTTP work admitted by this process;
  `pool` is `application` or `operations`. The dashboard sums each pool across
  independently scraped replicas; it is not utilization percentage or a global
  admission limit. Long streams retain slots until completion.
- `gateway_overload_rejections_total{pool,reason}`: fail-fast capacity rejections;
  `reason` is `inflight` or `accounting`. Accounting is the conservative retained
  task admission threshold, including stream watchdogs, not measured queue latency
  or a hard task cap. Rejections also appear as HTTP 503 attempts.

The example application-overload alert requires more than 20 rejections in a
rolling five-minute window, sustained for five minutes, on an individual replica.
A separate operational-pool alert requires more than five rejections per window
for five minutes. Small counts, brief bursts and other scrape jobs do not trigger
these rules. These are volume thresholds, not error ratios: healthy probe traffic
cannot dilute application overload, but low-volume rejected requests intentionally
may not alert. Both reasons contribute to application overload; inspect the
pool/reason dashboard panel to distinguish in-flight pressure from accounting.
Check slow providers, database health and cleanup before increasing limits. Use
per-instance series to diagnose imbalance hidden by the dashboard's aggregate.

CI checks rule syntax and executes synthetic `promtool test rules` scenarios,
including sustained overload, low counts, operational isolation, transient bursts,
per-replica isolation and counter resets. These examples neither install monitoring
nor establish measured capacity, a paging destination or an SLO.
