#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import select
import sys
import termios
import time
import tty
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

try:
    import websocket  # websocket-client
except Exception:
    websocket = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_headers(headers: dict | None) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in {"authorization", "cookie", "set-cookie"}:
            out[str(k)] = "[redacted]"
        else:
            out[str(k)] = str(v)
    return out


def body_to_b64_limited(raw: bytes, limit: int) -> tuple[str, bool, int]:
    capped = raw[:limit] if limit > 0 else raw
    return base64.b64encode(capped).decode("ascii"), len(raw) > len(capped), len(raw)


@dataclass
class RawMode:
    enabled: bool = False
    fd: int = -1
    old_attrs: list | None = None

    def __enter__(self):
        if sys.stdin.isatty():
            self.fd = sys.stdin.fileno()
            self.old_attrs = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.enabled = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.enabled and self.old_attrs is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_attrs)


def read_key_nonblocking() -> str | None:
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return None
    return sys.stdin.read(1)


class CDPWSClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        if websocket is None or not hasattr(websocket, "create_connection"):
            raise RuntimeError(
                "Dependencia invalida para WebSocket. Instala: pip install websocket-client "
                "y elimina paquete conflictivo 'websocket' si lo tienes."
            )
        self.ws = websocket.create_connection(ws_url, timeout=2)
        self.next_id = 1

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def _send(self, payload: dict):
        self.ws.send(json.dumps(payload))

    def send_cmd_no_wait(self, method: str, params: dict | None = None) -> int:
        cid = self.next_id
        self.next_id += 1
        msg = {"id": cid, "method": method, "params": params or {}}
        self._send(msg)
        return cid

    def recv_json(self, timeout_sec: float = 0.1) -> dict | None:
        self.ws.settimeout(timeout_sec)
        try:
            raw = self.ws.recv()
        except Exception as exc:
            if websocket is not None and hasattr(websocket, "WebSocketTimeoutException"):
                if isinstance(exc, websocket.WebSocketTimeoutException):
                    return None
            if "timed out" in str(exc).lower():
                return None
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def send_cmd_wait(self, method: str, params: dict | None, dispatcher, timeout_sec: float = 3.0) -> dict:
        cid = self.send_cmd_no_wait(method, params)
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            msg = self.recv_json(timeout_sec=0.2)
            if not msg:
                continue

            # respuesta esperada
            if msg.get("id") == cid:
                return msg

            # otros mensajes (eventos u otras respuestas)
            dispatcher(msg)

        return {"id": cid, "error": {"message": f"Timeout esperando {method}"}}


def get_targets(cdp_http: str) -> list[dict]:
    base = cdp_http.rstrip("/")
    with urlopen(f"{base}/json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def pick_target(targets: list[dict], target_url_substr: str) -> dict | None:
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]

    if target_url_substr:
        for t in pages:
            if target_url_substr in str(t.get("url", "")):
                return t

    for t in pages:
        if str(t.get("url", "")) not in {"", "about:blank"}:
            return t

    return pages[0] if pages else None


def save_json(out_path: Path, args, started_at: str, entries: list[dict], target_info: dict):
    payload = {
        "createdAt": now_iso(),
        "startedAt": started_at,
        "note": "Captura CDP WebSocket Network.* (HTTP/HTTPS). TCP crudo no disponible.",
        "cdp": args.cdp,
        "targetUrl": args.target_url,
        "targetTitle": target_info.get("title"),
        "targetCurrentUrl": target_info.get("url"),
        "trafficCount": len(entries),
        "traffic": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Captura trafico via CDP WebSocket y finaliza con Q/Esc.")
    p.add_argument("--cdp", default="http://127.0.0.1:9222", help="CDP HTTP endpoint")
    p.add_argument(
        "--target-url",
        default="https://www.your-url-here
        help="Subcadena para elegir la pestaña objetivo",
    )
    p.add_argument("--output", default="traffic-debug-python.json", help="Ruta JSON salida")
    p.add_argument("--body-limit", type=int, default=0, help="Max bytes body por respuesta (0 = sin limite)")
    p.add_argument("--autosave-seconds", type=int, default=5)
    p.add_argument("--duration", type=int, default=0, help="Duracion total en segundos (0 = hasta Q/Esc)")
    p.add_argument("--list-targets", action="store_true", help="Solo lista targets y sale")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    started_at = now_iso()
    out_path = Path(args.output).expanduser().resolve()

    try:
        targets = get_targets(args.cdp)
    except Exception as exc:
        print(f"No se pudo consultar {args.cdp}/json: {exc}", file=sys.stderr)
        return 1

    if args.list_targets:
        for i, t in enumerate(targets, start=1):
            print(f"[{i}] type={t.get('type')} title={t.get('title')} url={t.get('url')}")
        return 0

    target = pick_target(targets, args.target_url)
    if not target:
        print("No se encontró un target 'page' con webSocketDebuggerUrl.", file=sys.stderr)
        return 1

    ws_url = target.get("webSocketDebuggerUrl")
    print(f"Target seleccionado: {target.get('title')} | {target.get('url')}")

    try:
        client = CDPWSClient(ws_url)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    entries: list[dict] = []
    req_by_id: dict[str, dict] = {}
    resp_by_id: dict[str, dict] = {}
    finished_ids: list[str] = []
    failed_ids: dict[str, str] = {}
    dirty = False

    def dispatch(msg: dict):
        nonlocal dirty
        method = msg.get("method")
        params = msg.get("params", {})

        if method == "Network.requestWillBeSent":
            rid = params.get("requestId")
            req = params.get("request", {})
            if rid:
                req_by_id[rid] = {
                    "ts": now_iso(),
                    "url": req.get("url"),
                    "method": req.get("method"),
                    "resourceType": params.get("type"),
                    "headers": sanitize_headers(req.get("headers", {})),
                    "postData": req.get("postData"),
                }
                dirty = True

        elif method == "Network.responseReceived":
            rid = params.get("requestId")
            rsp = params.get("response", {})
            if rid:
                resp_by_id[rid] = {
                    "ts": now_iso(),
                    "url": rsp.get("url"),
                    "status": rsp.get("status"),
                    "ok": int(rsp.get("status", 0) or 0) < 400,
                    "headers": sanitize_headers(rsp.get("headers", {})),
                }
                dirty = True

        elif method == "Network.loadingFinished":
            rid = params.get("requestId")
            if rid:
                finished_ids.append(rid)

        elif method == "Network.loadingFailed":
            rid = params.get("requestId")
            if rid:
                failed_ids[rid] = str(params.get("errorText") or "loadingFailed")

    # Enable domains
    client.send_cmd_wait("Network.enable", {}, dispatch, timeout_sec=3.0)
    client.send_cmd_wait("Page.enable", {}, dispatch, timeout_sec=3.0)

    print("Captura iniciada.")
    print("1) Recarga la página del visor")
    print("2) Haz scroll manual para cargar más páginas")
    print("Pulsa 'q' o 'Esc' para finalizar (sin Ctrl+C).")

    start = time.time()
    last_save = 0.0

    try:
        with RawMode():
            while True:
                # consume inbound messages
                msg = client.recv_json(timeout_sec=0.1)
                if msg:
                    dispatch(msg)

                # process finished responses outside callback
                while finished_ids:
                    rid = finished_ids.pop(0)

                    req_data = req_by_id.get(
                        rid,
                        {
                            "ts": now_iso(),
                            "url": None,
                            "method": None,
                            "resourceType": None,
                            "headers": {},
                            "postData": None,
                        },
                    )
                    rsp_data = resp_by_id.get(
                        rid,
                        {
                            "ts": now_iso(),
                            "url": req_data.get("url"),
                            "status": None,
                            "ok": False,
                            "headers": {},
                        },
                    )

                    body_error = None
                    raw = b""
                    try:
                        rb = client.send_cmd_wait("Network.getResponseBody", {"requestId": rid}, dispatch, timeout_sec=2.0)
                        if "result" in rb:
                            b = rb["result"].get("body", "")
                            if rb["result"].get("base64Encoded"):
                                raw = base64.b64decode(b)
                            else:
                                raw = str(b).encode("utf-8", errors="ignore")
                        else:
                            body_error = str(rb.get("error", {}).get("message", "getResponseBody error"))
                    except Exception as exc:
                        body_error = str(exc)

                    b64, trunc, size = body_to_b64_limited(raw, args.body_limit)
                    entries.append(
                        {
                            "request": req_data,
                            "response": {
                                **rsp_data,
                                "bodyBase64": b64,
                                "bodyTruncated": trunc,
                                "bodySize": size,
                                "bodyError": body_error,
                            },
                        }
                    )
                    dirty = True

                    if len(entries) % 25 == 0:
                        print(f"Capturadas {len(entries)} entradas...")

                # process failed requests
                if failed_ids:
                    for rid, err in list(failed_ids.items()):
                        req_data = req_by_id.get(
                            rid,
                            {
                                "ts": now_iso(),
                                "url": None,
                                "method": None,
                                "resourceType": None,
                                "headers": {},
                                "postData": None,
                            },
                        )
                        entries.append(
                            {
                                "request": req_data,
                                "response": {
                                    "ts": now_iso(),
                                    "url": req_data.get("url"),
                                    "status": None,
                                    "ok": False,
                                    "headers": {},
                                    "bodyBase64": "",
                                    "bodyTruncated": False,
                                    "bodySize": 0,
                                    "bodyError": f"loadingFailed: {err}",
                                },
                            }
                        )
                        dirty = True
                        failed_ids.pop(rid, None)

                now = time.time()
                if args.autosave_seconds > 0 and dirty and (now - last_save) >= args.autosave_seconds:
                    save_json(out_path, args, started_at, entries, target)
                    last_save = now
                    dirty = False
                    print(f"Autosave: {len(entries)} entradas -> {out_path}")

                if args.duration > 0 and (now - start) >= args.duration:
                    break

                key = read_key_nonblocking()
                if key in {"q", "Q", "\x1b"}:
                    break
    finally:
        save_json(out_path, args, started_at, entries, target)
        print(f"Guardado final: {len(entries)} entradas -> {out_path}")
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
