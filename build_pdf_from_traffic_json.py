#!/usr/bin/env python3
"""
Reconstruye un PDF desde un traffic debug JSON generado por Playwright.

Toma entradas de .traffic[] donde:
- exista response.url con patron /imagenes_cartilla/pagina_XXX.ext
- exista response.bodyBase64

Decodifica bodyBase64 a imagen, ordena por numero de pagina y genera PDF.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


PAGE_RE = re.compile(r"/imagenes_cartilla/pagina_(\d+)\.(webp|jpg|jpeg|png|avif)$", re.IGNORECASE)


@dataclass
class PageImage:
    page_num: int
    ext: str
    image_bytes: bytes
    source_url: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Genera PDF desde traffic-debug JSON (imagenes_cartilla).")
    p.add_argument("--input", required=True, help="Ruta a traffic-debug-python.json")
    p.add_argument("--output", required=True, help="Ruta del PDF de salida")
    p.add_argument(
        "--workdir",
        default="./pages_from_json",
        help="Carpeta donde guardar imágenes extraídas",
    )
    p.add_argument(
        "--keep-images",
        action="store_true",
        help="Conservar imágenes en workdir luego de generar PDF",
    )
    return p.parse_args()


def load_entries(input_path: Path) -> list[dict]:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    traffic = data.get("traffic")
    if not isinstance(traffic, list):
        raise ValueError("JSON no contiene arreglo 'traffic'.")
    return traffic


def collect_best_pages(entries: list[dict]) -> dict[int, PageImage]:
    by_page: dict[int, PageImage] = {}

    for entry in entries:
        response = entry.get("response") or {}
        url = str(response.get("url") or "")
        m = PAGE_RE.search(url)
        if not m:
            continue

        b64 = response.get("bodyBase64")
        if not isinstance(b64, str) or not b64:
            continue

        truncated = bool(response.get("bodyTruncated"))
        if truncated:
            # No sirve para reconstruir fielmente esa página.
            continue

        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue

        page_num = int(m.group(1))
        ext = m.group(2).lower()

        current = by_page.get(page_num)
        # Si hay duplicados de la misma pagina, conserva el de mayor tamaño.
        if current is None or len(raw) > len(current.image_bytes):
            by_page[page_num] = PageImage(
                page_num=page_num,
                ext=ext,
                image_bytes=raw,
                source_url=url,
            )

    return by_page


def write_images(by_page: dict[int, PageImage], workdir: Path) -> list[Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for page_num in sorted(by_page):
        item = by_page[page_num]
        out = workdir / f"pagina_{page_num:03d}.{item.ext}"
        out.write_bytes(item.image_bytes)
        written.append(out)

    return written


def build_pdf(image_paths: list[Path], output_pdf: Path) -> None:
    images: list[Image.Image] = []
    for p in image_paths:
        img = Image.open(p).convert("RGB")
        images.append(img)

    if not images:
        raise ValueError("No se pudo abrir ninguna imagen para generar PDF.")

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(output_pdf, save_all=True, append_images=images[1:])

    for im in images:
        im.close()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_pdf = Path(args.output).expanduser().resolve()
    workdir = Path(args.workdir).expanduser().resolve()

    if not input_path.exists():
        print(f"No existe input: {input_path}")
        return 1

    try:
        entries = load_entries(input_path)
    except Exception as exc:
        print(f"Error leyendo JSON: {exc}")
        return 1

    pages = collect_best_pages(entries)
    if not pages:
        print("No se encontraron páginas válidas en imagenes_cartilla.")
        return 2

    image_paths = write_images(pages, workdir)
    print(f"Páginas extraídas: {len(image_paths)}")

    try:
        build_pdf(image_paths, output_pdf)
    except Exception as exc:
        print(f"Error generando PDF: {exc}")
        return 1

    print(f"PDF generado: {output_pdf}")

    if not args.keep_images:
        for p in image_paths:
            p.unlink(missing_ok=True)
        try:
            if not any(workdir.iterdir()):
                workdir.rmdir()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
