"""Serve Cloudflare/clef locally as a Jev/SystemOne-compatible endpoint.

    .venv/bin/python clef_server.py --port 8791 [--device mps] [--model Cloudflare/clef]

`POST /v1/systemone` takes the same request body TypeSafe's Jev API takes and
returns the same response body, via the model repo's own `systemone()`. Bound to
127.0.0.1 so `sopbench_judge.py --jev-url` treats it as local and sends no key.

Requests are served one at a time (a lock around the forward pass): a single
27B model on one device gains nothing from concurrent forwards, and verdicts
must not depend on batching.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
from huggingface_hub import snapshot_download


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Cloudflare/clef")
    ap.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cpu")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--max-length", type=int, default=16384)
    args = ap.parse_args()

    path = Path(snapshot_download(args.model))
    sys.path.insert(0, str(path))
    from joint_schema_model import load_release_model, systemone

    t0 = time.time()
    model, processor = load_release_model(path, device=args.device)
    print(f"loaded {args.model} on {args.device} in {time.time() - t0:.0f}s", flush=True)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"ok": True, "model": args.model, "device": args.device})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/v1/systemone":
                self._send(404, {"error": "not found"})
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                t = time.time()
                with lock:
                    resp = systemone(model, processor, req, max_length=args.max_length)
                resp["usage"]["latency_ms"] = round((time.time() - t) * 1000, 1)
                self._send(200, resp)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # surfaced to the judge as an HTTP 500 row
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, *a) -> None:
            pass

    print(f"serving POST http://127.0.0.1:{args.port}/v1/systemone", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
