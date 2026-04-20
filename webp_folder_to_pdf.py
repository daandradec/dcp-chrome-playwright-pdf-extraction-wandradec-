#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convierte WEBP a JPG y arma PDF en orden alfabético.")
    p.add_argument(
        "--input-dir",
        default="/Users/daandradec/pages_best_quality",
        help="Carpeta con imágenes WEBP.",
    )
    p.add_argument(
        "--jpg-dir-name",
        default="jpg",
        help="Nombre de subcarpeta interna para JPG.",
    )
    p.add_argument(
        "--output-pdf",
        default="/Users/daandradec/pages_best_quality/output.pdf",
        help="Ruta del PDF final.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Directorio no válido: {input_dir}")
        return 1

    jpg_dir = input_dir / args.jpg_dir_name
    jpg_dir.mkdir(parents=True, exist_ok=True)

    webp_files = sorted([p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() == ".webp"])
    if not webp_files:
        print("No se encontraron archivos .webp en la carpeta de entrada.")
        return 2

    jpg_files: list[Path] = []
    for webp in webp_files:
        out_jpg = jpg_dir / f"{webp.stem}.jpg"
        with Image.open(webp) as img:
            rgb = img.convert("RGB")
            # Máxima calidad posible en JPG (aun así JPEG es compresión con pérdida).
            rgb.save(out_jpg, "JPEG", quality=100, subsampling=0, optimize=False)
        jpg_files.append(out_jpg)

    ordered_jpg = sorted(jpg_files, key=lambda p: p.name)
    images: list[Image.Image] = []
    for p in ordered_jpg:
        images.append(Image.open(p).convert("RGB"))

    output_pdf = Path(args.output_pdf).expanduser().resolve()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(output_pdf, save_all=True, append_images=images[1:])

    for im in images:
        im.close()

    print(f"JPG generados en: {jpg_dir}")
    print(f"PDF generado en: {output_pdf}")
    print(f"Total de páginas: {len(images)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
