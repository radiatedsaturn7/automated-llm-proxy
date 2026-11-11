# Thunderbolt observability

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
