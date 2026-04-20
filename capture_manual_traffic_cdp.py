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
from typing import Optional

from playwright.sync_api import Browser, CDPSession, Page, sync_playwright


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_headers(headers: dict) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in {"authorization", "cookie", "set-cookie"}:
            out[str(k)] = "[redacted]"
        else:
            out[str(k)] = str(v)
    return out


def body_to_limited_b64(raw: bytes, limit: int) -> tuple[str, bool, int]:
    capped = raw[:limit] if limit > 0 else raw
    return base64.b64encode(capped).decode("ascii"), len(raw) > len(capped), len(raw)


def get_target_page(browser: Browser, target_url: str) -> Optional[Page]:
    # 1) try exact target match
    for ctx in browser.contexts:
        for p in ctx.pages:
            try:
                if target_url and target_url in p.url:
                    return p
            except Exception:
                pass

    # 2) visible page
    for ctx in browser.contexts:
        for p in ctx.pages:
            try:
                visible = p.evaluate("() => document.visibilityState === 'visible'")
                if visible:
                    return p
            except Exception:
                pass

    # 3) first page fallback
    for ctx in browser.contexts:
        if ctx.pages:
            return ctx.pages[0]
    return None


@dataclass
class RawMode:
    enabled: bool = False
    fd: int = -1
    old_attrs: Optional[list] = None

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


def read_key_nonblocking() -> Optional[str]:
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return None
    ch = sys.stdin.read(1)
    return ch


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Captura trafico de una pestana Chrome via CDP y guarda JSON.")
    p.add_argument("--cdp", default="http://127.0.0.1:9222", help="Endpoint CDP")
    p.add_argument(
        "--target-url",
        default="your url here",
        help="Pestana objetivo",
    )
    p.add_argument("--navigate-if-needed", action="store_true", help="Navegar a target-url si no coincide")
    p.add_argument("--output", default="traffic-debug-python.json", help="Ruta JSON salida")
    p.add_argument("--body-limit", type=int, default=0, help="Max bytes por body en base64 (0 = sin limite)")
    p.add_argument("--autosave-seconds", type=int, default=5, help="Autosave periodico")
    p.add_argument("--duration", type=int, default=0, help="Duracion en segundos (0 = hasta Q/Esc)")
    return p.parse_args()


def save_json(out_path: Path, args: argparse.Namespace, started_at: str, entries: list[dict]) -> None:
    payload = {
        "createdAt": now_iso(),
        "startedAt": started_at,
        "note": "Captura via CDP Network.* (HTTP/HTTPS). TCP crudo no disponible.",
        "cdp": args.cdp,
        "targetUrl": args.target_url,
        "trafficCount": len(entries),
        "traffic": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    out_path = Path(args.output).expanduser().resolve()
    started_at = now_iso()

    entries: list[dict] = []
    req_by_id: dict[str, dict] = {}
    resp_by_id: dict[str, dict] = {}
    finished_ids: list[str] = []
    failed_ids: dict[str, str] = {}

    dirty = False

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp)
        except Exception as exc:
            print(f"No se pudo conectar a CDP {args.cdp}: {exc}", file=sys.stderr)
            return 1

        page = get_target_page(browser, args.target_url)
        if not page:
            print("No se encontro ninguna pestana en la sesion CDP.", file=sys.stderr)
            browser.close()
            return 1

        if args.navigate_if_needed and args.target_url and args.target_url not in page.url:
            try:
                page.goto(args.target_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                print(f"No se pudo navegar a target-url: {exc}", file=sys.stderr)

        print(f"Conectado a CDP: {args.cdp}")
        print(f"Pestana objetivo: {page.url}")

        cdp: CDPSession = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Page.enable")

        def on_request_will_be_sent(ev: dict) -> None:
            nonlocal dirty
            rid = ev.get("requestId")
            req = ev.get("request", {})
            if not rid:
                return
            req_by_id[rid] = {
                "ts": now_iso(),
                "url": req.get("url"),
                "method": req.get("method"),
                "resourceType": ev.get("type"),
                "headers": sanitize_headers(req.get("headers", {})),
                "postData": req.get("postData"),
            }
            dirty = True

        def on_response_received(ev: dict) -> None:
            nonlocal dirty
            rid = ev.get("requestId")
            rsp = ev.get("response", {})
            if not rid:
                return
            resp_by_id[rid] = {
                "ts": now_iso(),
                "url": rsp.get("url"),
                "status": rsp.get("status"),
                "ok": int(rsp.get("status", 0) or 0) < 400,
                "headers": sanitize_headers(rsp.get("headers", {})),
            }
            dirty = True

        def on_loading_finished(ev: dict) -> None:
            rid = ev.get("requestId")
            if rid:
                finished_ids.append(rid)

        def on_loading_failed(ev: dict) -> None:
            rid = ev.get("requestId")
            if rid:
                failed_ids[rid] = str(ev.get("errorText") or "loadingFailed")

        cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
        cdp.on("Network.responseReceived", on_response_received)
        cdp.on("Network.loadingFinished", on_loading_finished)
        cdp.on("Network.loadingFailed", on_loading_failed)

        print("Captura iniciada.")
        print("1) Recarga la pagina del visor")
        print("2) Haz scroll para forzar lazy-load")
        print("Presiona 'q' o 'Esc' para finalizar y guardar.")

        start = time.time()
        last_save = 0.0

        with RawMode():
            try:
                while True:
                    # Process finished responses outside callbacks (mas estable)
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
                        resp_data = resp_by_id.get(
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
                            rb = cdp.send("Network.getResponseBody", {"requestId": rid})
                            if rb.get("base64Encoded"):
                                raw = base64.b64decode(rb.get("body", ""))
                            else:
                                raw = str(rb.get("body", "")).encode("utf-8", errors="ignore")
                        except Exception as exc:
                            body_error = str(exc)

                        b64, trunc, size = body_to_limited_b64(raw, args.body_limit)
                        entries.append(
                            {
                                "request": req_data,
                                "response": {
                                    **resp_data,
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

                    # Process failed requests
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

                    # Autosave
                    now = time.time()
                    if args.autosave_seconds > 0 and dirty and (now - last_save) >= args.autosave_seconds:
                        save_json(out_path, args, started_at, entries)
                        last_save = now
                        dirty = False
                        print(f"Autosave: {len(entries)} entradas -> {out_path}")

                    # duration-based stop
                    if args.duration > 0 and (now - start) >= args.duration:
                        break

                    # key-based stop
                    ch = read_key_nonblocking()
                    if ch in {"q", "Q", "\x1b"}:  # q or ESC
                        break

                    time.sleep(0.1)
            except KeyboardInterrupt:
                # fallback in case user still presses Ctrl+C
                pass

        # Final save
        save_json(out_path, args, started_at, entries)
        print(f"Guardado final: {len(entries)} entradas -> {out_path}")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
