"""Fail-fast, process-local capacity protection before request-body buffering."""
from prometheus_client import Counter, Gauge
from starlette.responses import JSONResponse

from .config import get_settings

ACTIVE = Gauge('gateway_inflight_requests', 'Requests holding local capacity', ['pool'])
REJECTED = Counter('gateway_overload_rejections_total', 'Local capacity rejections', ['pool', 'reason'])
OPS = frozenset({'/health/live', '/health/ready', '/metrics'})


class OverloadMiddleware:
    def __init__(self, app, accounting_tasks):
        self.app = app
        self.accounting_tasks = accounting_tasks
        self.active = {'application': 0, 'operations': 0}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        pool = ('operations' if scope.get('method') == 'GET' and scope.get('path') in OPS
                else 'application')
        cfg = get_settings()
        limit = cfg.max_inflight_operations if pool == 'operations' else cfg.max_inflight_requests
        reason = None
        # No await between checking and incrementing: atomic within this event loop.
        if self.active[pool] >= limit:
            reason = 'inflight'
        elif pool == 'application' and len(self.accounting_tasks()) >= 2 * cfg.max_inflight_requests:
            reason = 'accounting'
        if reason:
            REJECTED.labels(pool, reason).inc()
            headers = {'Retry-After': '1', 'Cache-Control': 'no-store',
                       'X-Content-Type-Options': 'nosniff'}
            if scope.get('http_version', '1.1') in {'1.0', '1.1'}:
                headers['Connection'] = 'close'
            return await JSONResponse({'detail': 'Gateway capacity temporarily exhausted'},
                                      status_code=503, headers=headers)(scope, receive, send)
        self.active[pool] += 1
        ACTIVE.labels(pool).inc()
        try:
            await self.app(scope, receive, send)
        finally:
            self.active[pool] -= 1
            ACTIVE.labels(pool).dec()
