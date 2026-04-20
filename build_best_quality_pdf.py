#!/usr/bin/env python3
"""
Descarga un e-book paginado por imágenes (lazy/visor) buscando la mejor calidad
por página y genera un PDF consolidado.

Requisitos:
  pip install requests pillow

Ejemplo:
  python build_best_quality_pdf.py \
    --base-url "your url site here" \
    --start 1 --end 195 \
    --output "/Users/daandradec/regimen_195_paginas_hd.pdf"
"""

from __future__ import annotations

import argparse
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from PIL import Image


DEFAULT_BASE_URL = "url site here"
DEFAULT_OUT = "ebook_best_quality.pdf"


@dataclass
class Candidate:
    url: str
    content: bytes
    width: int
    height: int
    size_bytes: int
    ext: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reconstruye PDF desde imágenes con mejor calidad disponible.")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="URL base del visor.")
    p.add_argument("--start", type=int, default=1, help="Página inicial.")
    p.add_argument("--end", type=int, default=195, help="Página final.")
    p.add_argument("--output", default=DEFAULT_OUT, help="Ruta PDF salida.")
    p.add_argument("--workdir", default="./pages_best_quality", help="Carpeta temporal de imágenes.")
    p.add_argument("--timeout", type=int, default=25, help="Timeout HTTP por request.")
    p.add_argument("--probe-timeout", type=int, default=8, help="Timeout corto por URL candidata.")
    p.add_argument(
        "--max-probes-per-page",
        type=int,
        default=120,
        help="Máximo de URLs candidatas a probar por página.",
    )
    p.add_argument("--cookie", default=None, help="Header Cookie opcional (si el origen exige sesión).")
    p.add_argument(
        "--cookie-auto",
        action="store_true",
        help="Extrae cookies automáticamente desde Chrome via CDP.",
    )
    p.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="Endpoint CDP para --cookie-auto.",
    )
    p.add_argument("--user-agent", default="Mozilla/5.0", help="User-Agent HTTP.")
    p.add_argument("--max-retries", type=int, default=2, help="Reintentos por URL candidata.")
    p.add_argument("--keep-images", action="store_true", help="Conservar imágenes descargadas.")
    return p.parse_args()


def join_url(base: str, rel: str) -> str:
    if not base.endswith("/"):
        base += "/"
    if rel.startswith("/"):
        rel = rel[1:]
    return base + rel


def build_candidate_paths(page_num: int) -> list[str]:
    num3 = f"{page_num:03d}"
    num4 = f"{page_num:04d}"
    names = [
        f"pagina_{num3}",
        f"pagina-{num3}",
        f"page_{num3}",
        f"page-{num3}",
        f"img_{num3}",
        f"pagina_{num4}",
        f"page_{num4}",
    ]

    suffixes = ["", "@2x", "-hd", "-full", "_hd"]
    exts = ["webp", "jpg", "jpeg", "png", "avif"]

    folders = ["imagenes_cartilla", "imagenes", "images", "img", "assets/imagenes_cartilla", "assets/images"]

    out: list[str] = []
    for folder in folders:
        for n in names:
            for s in suffixes:
                for ext in exts:
                    out.append(f"{folder}/{n}{s}.{ext}")

    return out


def request_content(session: requests.Session, url: str, timeout: int, max_retries: int) -> bytes | None:
    for _ in range(max_retries + 1):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.content:
                return r.content
        except requests.RequestException:
            pass
    return None


def decode_image(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        return None


def score_candidate(c: Candidate) -> tuple[int, int, int]:
    return (c.width * c.height, c.size_bytes, c.width)


def pick_best(candidates: Iterable[Candidate]) -> Candidate | None:
    items = list(candidates)
    if not items:
        return None
    items.sort(key=score_candidate, reverse=True)
    return items[0]


def normalize_image_for_pdf(img: Image.Image) -> Image.Image:
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def cookie_header_from_cdp(cdp_url: str, base_url: str) -> str:
    from playwright.sync_api import sync_playwright  # Import local para mantener dependencia opcional.

    pairs: dict[str, str] = {}
    base_host = urlparse(base_url).hostname or ""

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        for context in browser.contexts:
            try:
                cookies = context.cookies([base_url])
            except Exception:
                cookies = context.cookies()
            for c in cookies:
                domain = (c.get("domain") or "").lstrip(".")
                name = c.get("name")
                value = c.get("value")
                if not name or value is None:
                    continue
                # Preferimos cookies del dominio objetivo.
                if base_host and domain and not (base_host == domain or base_host.endswith(f".{domain}")):
                    continue
                pairs[name] = value

    return "; ".join([f"{k}={v}" for k, v in pairs.items()])


def main() -> int:
    args = parse_args()

    if args.end < args.start:
        raise SystemExit("--end debe ser >= --start")

    workdir = Path(args.workdir).expanduser().resolve()
    out_pdf = Path(args.output).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})
    if args.cookie_auto and not args.cookie:
        try:
            args.cookie = cookie_header_from_cdp(args.cdp_url, args.base_url)
            if args.cookie:
                print(f"[COOKIE] cargadas automáticamente ({len(args.cookie.split('; '))} cookies).")
            else:
                print("[COOKIE] no se encontraron cookies para el dominio objetivo.")
        except Exception as exc:
            print(f"[COOKIE] error extrayendo cookies por CDP: {exc}")

    if args.cookie:
        session.headers.update({"Cookie": args.cookie})

    selected_files: list[Path] = []

    for page_num in range(args.start, args.end + 1):
        rel_candidates = build_candidate_paths(page_num)
        total_candidates = len(rel_candidates)
        found: list[Candidate] = []
        probes = 0

        print(f"[PAGE {page_num:03d}] probando hasta {args.max_probes_per_page} de {total_candidates} variantes...")
        for rel in rel_candidates:
            if probes >= args.max_probes_per_page:
                break
            url = join_url(args.base_url, rel)
            content = request_content(session, url, timeout=args.probe_timeout, max_retries=args.max_retries)
            probes += 1
            if not content:
                continue

            img = decode_image(content)
            if not img:
                continue

            ext_match = re.search(r"\.([a-zA-Z0-9]+)$", rel)
            ext = (ext_match.group(1).lower() if ext_match else "bin")
            found.append(
                Candidate(
                    url=url,
                    content=content,
                    width=img.width,
                    height=img.height,
                    size_bytes=len(content),
                    ext=ext,
                )
            )

            # Si encontramos una imagen realmente grande, no vale la pena seguir.
            if img.width * img.height >= 2_500_000:
                break

        best = pick_best(found)
        if not best:
            print(f"[MISS] pagina {page_num:03d} (probes={probes})")
            continue

        out_img = workdir / f"pagina_{page_num:03d}.{best.ext}"
        out_img.write_bytes(best.content)
        selected_files.append(out_img)
        print(
            f"[OK] pagina {page_num:03d} | {best.width}x{best.height} | "
            f"{best.size_bytes} bytes | probes={probes} | {best.url}"
        )

    if not selected_files:
        print("No se descargó ninguna página.")
        return 2

    pil_images: list[Image.Image] = []
    for f in selected_files:
        img = Image.open(f)
        pil_images.append(normalize_image_for_pdf(img))

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    pil_images[0].save(out_pdf, save_all=True, append_images=pil_images[1:])

    for im in pil_images:
        try:
            im.close()
        except Exception:
            pass

    if not args.keep_images:
        for f in selected_files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            if not any(workdir.iterdir()):
                workdir.rmdir()
        except Exception:
            pass

    print(f"PDF generado: {out_pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
