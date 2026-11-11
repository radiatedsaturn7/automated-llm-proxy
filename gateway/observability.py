"""Low-cardinality HTTP telemetry. Never log request bodies, headers or queries."""
import json
import logging
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.responses import JSONResponse

HTTP_REQUESTS = Counter('gateway_http_requests_total', 'Completed HTTP attempts', ['route', 'method', 'status'])
HTTP_DURATION = Histogram('gateway_http_request_duration_seconds', 'HTTP lifetime including streaming',
                          ['route', 'method', 'status'], buckets=(.01, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 120, 300))
LOGGER = logging.getLogger('gateway.http')
METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT', 'TRACE'})


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = str(uuid.uuid4())
        scope.setdefault('state', {})['request_id'] = request_id
        method = scope.get('method', 'OTHER')
        if method not in METHODS:
            method = 'OTHER'
        started = time.monotonic()
        status = 499
        sent_headers = False
        complete = False

        async def observed_send(message):
            nonlocal status, sent_headers, complete
            if message['type'] == 'http.response.start':
                status = message['status']
                sent_headers = True
                headers = []
                for key, value in message.get('headers', []):
                    if key.lower() == b'x-reservation-id':
                        # Only an internally generated, valid reservation UUID is forwarded.
                        try:
                            reservation = str(uuid.UUID(value.decode('ascii')))
                        except (ValueError, UnicodeError):
                            continue
                        headers.append((b'x-reservation-id', reservation.encode('ascii')))
                    elif key.lower() != b'x-request-id':
                        headers.append((key, value))
                headers.append((b'x-request-id', request_id.encode('ascii')))
                message = {**message, 'headers': headers}
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                complete = True

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            if sent_headers:
                # A partial stream cannot be converted to a successful JSON response.
                # Do not expose original exception text to the server's error logger.
                raise RuntimeError('Gateway response interrupted') from None
            status = 500
            response = JSONResponse({'detail': 'Internal gateway error'}, status_code=500)
            await response(scope, receive, observed_send)
        finally:
            duration = max(0, time.monotonic() - started)
            route_object = scope.get('route')
            route = getattr(route_object, 'path', None)
            # Only router-owned templates become labels; never raw paths/queries.
            if not route:
                route = '/admin/static/*' if scope.get('path', '').startswith('/admin/static/') else 'unmatched'
            status_label = str(status) if isinstance(status, int) and 100 <= status <= 599 else 'unknown'
            HTTP_REQUESTS.labels(route, method, status_label).inc()
            HTTP_DURATION.labels(route, method, status_label).observe(duration)
            LOGGER.info(json.dumps({'event': 'http.request', 'request_id': request_id, 'route': route,
                'method': method, 'status': status_label, 'duration_seconds': round(duration, 6),
                'response_complete': complete}, separators=(',', ':')))
