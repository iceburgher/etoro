"""Vercel Cron: STRATEGY_JOB, 2 minuter efter varje färdig 4H-stapel (UTC)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.jobs import handle  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = handle("strategy", self.headers.get("authorization"))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
