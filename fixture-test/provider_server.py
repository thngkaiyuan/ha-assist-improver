"""The household's provider for a replay worker in its own process (test_worker.py): Anthropic's model list and its
Messages API on 127.0.0.1, each message request answered with the next blocks in a script file and kept in a log file
(conftest.ScriptedProvider's, across processes). argv: the folder holding script.json, models.json and requests.jsonl.
Prints its port."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from anthropic_fixture import events_of  # noqa: E402

FOLDER = Path(sys.argv[1])


class Provider(BaseHTTPRequestHandler):
    def _send(self, status, body, kind="application/json"):
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        models = [{"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z",
                   "lifecycle": "active"} for m in json.loads((FOLDER / "models.json").read_text())]
        self._send(200, json.dumps({"data": models, "has_more": False, "first_id": models[0]["id"],
                                    "last_id": models[-1]["id"]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        with (FOLDER / "requests.jsonl").open("a") as log:
            log.write(json.dumps(body) + "\n")
        script = json.loads((FOLDER / "script.json").read_text())
        if not script:
            return self._send(400, json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "Unexpected inference call"}}).encode())
        (FOLDER / "script.json").write_text(json.dumps(script[1:]))
        stream = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events_of(script[0]))
        self._send(200, stream.encode(), "text/event-stream")

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
print(server.server_port, flush=True)
server.serve_forever()
