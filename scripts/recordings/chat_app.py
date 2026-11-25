"""Demo app for the recorded terminal sessions: one OpenAI chat call against a local stub server.

The stub answers on an ephemeral loopback port, so no network or API key is used; the
briefcase capture and replay around it are the real code.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import openai

import briefcase


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Paris"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=server.serve_forever, daemon=True).start()

briefcase.observe("runs.jsonl")
briefcase.auto(capture_content="full")
client = openai.OpenAI(api_key="sk-demo", base_url=f"http://127.0.0.1:{server.server_port}/v1")
reply = client.chat.completions.create(
    model="gpt-4o-mini", messages=[{"role": "user", "content": "What is the capital of France?"}])
print(reply.choices[0].message.content)
briefcase.shutdown()
server.shutdown()
