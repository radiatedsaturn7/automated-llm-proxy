"""Deterministic HTTPS upstream for disposable integration tests, never production."""
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No credentials, prompts, or response content in CI logs.

    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.end_headers()

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer integration-only-not-a-real-key':
            self.send_response(401)
            self.end_headers()
            return
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 1048576:
            self.send_response(400)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(size))
        usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        if self.path == '/v1/embeddings':
            data = {'object': 'list', 'model': body['model'], 'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.1, 0.2, 0.3]}], 'usage': {'prompt_tokens': 3, 'total_tokens': 3}}
        elif self.path == '/v1/chat/completions':
            if body.get('stream'):
                chunks = [
                    {'id': 'mock-stream', 'object': 'chat.completion.chunk', 'model': body['model'], 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Mock upstream response'}, 'finish_reason': None}]},
                    {'id': 'mock-stream', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage},
                ]
                output = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
                self.respond(output.encode(), 'text/event-stream')
                return
            data = {'id': 'mock-chat', 'object': 'chat.completion', 'model': body['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Mock upstream response'}, 'finish_reason': 'stop'}], 'usage': usage}
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.respond(json.dumps(data).encode(), 'application/json')

    def respond(self, body, content_type):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Request-ID', 'mock-upstream-123')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('/tls/server.crt', '/tls/server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
