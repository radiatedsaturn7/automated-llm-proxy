"""Isolated TLS mock with deterministic hold/release controls. Never deploy publicly."""
import json
import ssl
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
counts = Counter()
release = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass  # Expected when a gateway process is deliberately killed.

    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, {'ok': True})
        if self.path == '/control/status':
            with lock:
                return self.respond(200, {'counts': dict(counts)})
        return self.respond(404, {})

    def do_POST(self):
        if self.path == '/control/release':
            release.set()
            return self.respond(200, {'ok': True})
        if self.path == '/control/hold':
            release.clear()
            return self.respond(200, {'ok': True})
        if self.path != '/v1/chat/completions':
            return self.respond(404, {})
        if self.headers.get('Authorization') != 'Bearer integration-only-not-a-real-key':
            return self.respond(401, {})
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 1048576:
            return self.respond(400, {})
        body = json.loads(self.rfile.read(size))
        marker = body['messages'][-1]['content']
        if not isinstance(marker, str) or len(marker) > 128:
            return self.respond(400, {})
        with lock:
            counts[marker] += 1
        if marker.startswith('hold:') and not release.wait(45):
            return self.respond(503, {'error': {'message': 'test hold expired'}})
        return self.respond(200, {'id': 'mock-load', 'model': body['model'], 'object': 'chat.completion',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('/tls/server.crt', '/tls/server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
