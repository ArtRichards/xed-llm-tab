"""Mock OpenAI Responses endpoint for the xed integration test.

Input containing FORCE_ERROR gets a 401; SLOW gets a slow three-part stream;
anything else gets "Reply #<n> done". Every request body is appended to
the log file given as the second argument.
"""
import http.server
import json
import os
import sys
import time

LOG = sys.argv[2]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(LOG, "a") as f:
            f.write(json.dumps({"auth": self.headers["Authorization"], "body": body}) + "\n")
        with open(LOG) as f:
            n = sum(1 for _ in f)

        if "FORCE_ERROR" in body["input"]:
            payload = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if "SLOW" in body["input"]:
            parts, pause = ["slow-1 ", "slow-2 ", "slow-3 "], 0.6
        else:
            parts, pause = ["Reply ", "#%d" % n, " done"], 0.05

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in ([{"type": "response.created"}]
                          + [{"type": "response.output_text.delta", "delta": p} for p in parts]
                          + [{"type": "response.completed"}]):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event))).encode())
                self.wfile.flush()
                time.sleep(pause)
        except BrokenPipeError:
            pass


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
