#!/usr/bin/env python3
"""
Descarga un PDF desde una página protegida por login usando Playwright.

Flujo recomendado (sin cerrar tu Chrome activo):
  python download_pdf_from_active_tab.py \
    --url "https://www.your-url-here
    --output regimen-simple.pdf \
    --timeout 60 \
    --auto-launch-chrome \
    --use-main-profile \
    --profile-directory "Default"

Notas:
- Si CDP no está activo, el script intenta abrir una instancia con depuración remota.
- Con --use-main-profile prioriza tu perfil real de Chrome.
- Si ese perfil está bloqueado por la instancia activa, intenta un clon mínimo en /tmp
  para reutilizar cookies de sesión sin matar tu Chrome principal.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from playwright.sync_api import Browser, Page, Response, sync_playwright

DEFAULT_TARGET_URL = "https://www.your-url-here
DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_CHROME_APP_NAME = "Google Chrome"
DEFAULT_USER_DATA_DIR = str(Path.home() / "Library/Application Support/Google/Chrome")
DEFAULT_PROFILE_DIRECTORY = "Default"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga un PDF desde una web protegida usando sesión de Chrome y Playwright."
    )
    parser.add_argument("--cdp", default="http://localhost:9222", help="Endpoint CDP.")
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="URL objetivo.")
    parser.add_argument("--output", default="descarga.pdf", help="Ruta de salida PDF.")
    parser.add_argument("--match", default=None, help="Regex para filtrar URL del PDF.")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout en segundos.")
    parser.add_argument(
        "--debug-json",
        default=None,
        help="Si fallan todas las estrategias, guarda trafico HTTP/HTTPS en este JSON.",
    )
    parser.add_argument(
        "--debug-body-limit",
        type=int,
        default=200000,
        help="Max bytes de body por response en debug JSON (base64).",
    )

    parser.add_argument(
        "--auto-launch-chrome",
        action="store_true",
        help="Si CDP no está activo, intenta levantar Chrome con remote debugging.",
    )
    parser.add_argument("--chrome-path", default=DEFAULT_CHROME_PATH, help="Binario Chrome.")
    parser.add_argument(
        "--chrome-app-name",
        default=DEFAULT_CHROME_APP_NAME,
        help='Nombre app para "open -na" en macOS.',
    )

    parser.add_argument(
        "--use-main-profile",
        action="store_true",
        help="Intenta usar tu perfil principal para conservar sesión.",
    )
    parser.add_argument("--user-data-dir", default=DEFAULT_USER_DATA_DIR, help="User data dir Chrome.")
    parser.add_argument(
        "--profile-directory",
        default=DEFAULT_PROFILE_DIRECTORY,
        help='Perfil ("Default", "Profile 1", etc).',
    )
    parser.add_argument(
        "--clone-main-profile",
        action="store_true",
        default=True,
        help="Si el perfil real está bloqueado, intenta clonar el perfil en /tmp.",
    )
    parser.add_argument(
        "--allow-unauth-fallback",
        action="store_true",
        help="Permite fallback a perfil limpio (sin sesión) si lo anterior falla.",
    )
    return parser.parse_args()


def cdp_is_available(cdp_url: str, timeout_sec: float = 1.5) -> bool:
    try:
        with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=timeout_sec) as resp:
            return resp.status == 200
    except (URLError, OSError, ValueError):
        return False


def cdp_port_from_url(cdp_url: str) -> int:
    parsed = urlparse(cdp_url)
    if parsed.port:
        return parsed.port
    if parsed.scheme in ("http", "ws"):
        return 80
    if parsed.scheme in ("https", "wss"):
        return 443
    return 9222


def create_profile_clone(user_data_dir: str, profile_directory: str, port: int) -> Optional[str]:
    src_root = Path(user_data_dir)
    src_profile = src_root / profile_directory
    if not src_profile.exists():
        return None

    dst_root = Path(f"/tmp/chrome-cdp-profile-clone-{port}")
    dst_profile = dst_root / profile_directory

    try:
        if dst_root.exists():
            shutil.rmtree(dst_root)
        dst_profile.mkdir(parents=True, exist_ok=True)

        # Archivo global requerido para decrypt de cookies y estado base.
        local_state = src_root / "Local State"
        if local_state.exists():
            shutil.copy2(local_state, dst_root / "Local State")

        # Clona el perfil completo para preservar sesión/cookies/storage del sitio.
        # Se excluyen caches y locks para evitar corrupción y reducir tamaño.
        ignore_names = {
            "Cache",
            "Code Cache",
            "GPUCache",
            "ShaderCache",
            "GrShaderCache",
            "DawnCache",
            "Crashpad",
            "Safe Browsing",
            "BrowserMetrics",
        }

        def _ignore(_dir: str, names: list[str]) -> set[str]:
            ignored = set()
            for name in names:
                if name in ignore_names:
                    ignored.add(name)
                if name.startswith("Singleton"):
                    ignored.add(name)
                if name in {"LOCK", ".org.chromium.Chromium"}:
                    ignored.add(name)
            return ignored

        shutil.copytree(src_profile, dst_profile, dirs_exist_ok=True, ignore=_ignore)

        return str(dst_root)
    except Exception:
        return None


def try_launch_and_wait(cmd: list[str], cdp_url: str, wait_sec: float = 12) -> bool:
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if cdp_is_available(cdp_url):
            return True
        time.sleep(0.4)
    return False


def launch_chrome_for_cdp(args: argparse.Namespace) -> bool:
    port = cdp_port_from_url(args.cdp)
    commands: list[list[str]] = []

    # Prioridad: conservar sesión real.
    if args.use_main_profile:
        commands.append(
            [
                args.chrome_path,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={args.user_data_dir}",
                f"--profile-directory={args.profile_directory}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )

        if args.clone_main_profile:
            cloned = create_profile_clone(args.user_data_dir, args.profile_directory, port)
            if cloned:
                print(
                    "Usando clon de perfil para preservar sesión:",
                    f"{cloned}/{args.profile_directory}",
                )
                commands.append(
                    [
                        args.chrome_path,
                        f"--remote-debugging-port={port}",
                        f"--user-data-dir={cloned}",
                        f"--profile-directory={args.profile_directory}",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ]
                )

    # Fallback opcional a perfil no autenticado.
    if args.allow_unauth_fallback or not args.use_main_profile:
        commands.extend(
            [
                [
                    "/usr/bin/open",
                    "-na",
                    args.chrome_app_name,
                    "--args",
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                [
                    args.chrome_path,
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                [
                    args.chrome_path,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir=/tmp/chrome-cdp-profile-{port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            ]
        )

    for cmd in commands:
        if try_launch_and_wait(cmd, args.cdp):
            return True
    return False


def is_pdf_response(response: Response) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    content_disposition = (response.headers.get("content-disposition") or "").lower()
    url = response.url.lower()
    return (
        "application/pdf" in content_type
        or ".pdf" in content_disposition
        or url.endswith(".pdf")
        or ".pdf?" in url
    )


def looks_like_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def extract_pdf_urls_from_text(text: str, base_url: str) -> list[str]:
    candidates: list[str] = []

    # Links directos en HTML/JS.
    for raw in re.findall(r"""https?://[^"'\\s>]+\\.pdf(?:\\?[^"'\\s>]*)?""", text, flags=re.IGNORECASE):
        candidates.append(raw)
    for raw in re.findall(r"""(?:src|href|file)\\s*[:=]\\s*["']([^"']+\\.pdf(?:\\?[^"']*)?)["']""", text, flags=re.IGNORECASE):
        candidates.append(urljoin(base_url, raw))

    # URL encoded (pdf.js style: file=https%3A%2F%2F...pdf).
    decoded = unquote(text)
    for raw in re.findall(r"""https?://[^"'\\s>]+\\.pdf(?:\\?[^"'\\s>]*)?""", decoded, flags=re.IGNORECASE):
        candidates.append(raw)

    return list(dict.fromkeys(candidates))


def extract_pdf_urls_from_url(url: str, base_url: str) -> list[str]:
    out: list[str] = []
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("file", "pdf", "url", "source"):
        for value in query.get(key, []):
            candidate = unquote(value)
            if candidate.lower().startswith("http") and ".pdf" in candidate.lower():
                out.append(candidate)
            elif ".pdf" in candidate.lower():
                out.append(urljoin(base_url, candidate))
    return list(dict.fromkeys(out))


def extract_candidate_urls_from_text(text: str, base_url: str) -> list[str]:
    candidates: list[str] = []
    keywords = ("pdf", "visor", "download", "document", "archivo", "wp-content", "uploads")

    for raw in re.findall(r"""https?://[^"'\\s<>]+""", text, flags=re.IGNORECASE):
        if any(k in raw.lower() for k in keywords):
            candidates.append(raw)

    for raw in re.findall(r"""["'](/[^"'\\s<>]+)["']""", text):
        if any(k in raw.lower() for k in keywords):
            candidates.append(urljoin(base_url, raw))

    decoded = unquote(text)
    for raw in re.findall(r"""https?://[^"'\\s<>]+""", decoded, flags=re.IGNORECASE):
        if any(k in raw.lower() for k in keywords):
            candidates.append(raw)

    return list(dict.fromkeys(candidates))


def secondary_pdf_strategy(
    page: Page,
    url_pattern: Optional[re.Pattern[str]],
    network_samples: list[tuple[str, str, bytes]],
) -> Optional[tuple[str, bytes]]:
    urls_to_try: list[str] = []

    # Semillas de URL desde DOM actual.
    selectors = ["iframe[src]", "embed[src]", "object[data]", "a[href]"]
    for sel in selectors:
        attr = "src" if "iframe" in sel or "embed" in sel else "data" if "object" in sel else "href"
        els = page.locator(sel)
        for i in range(els.count()):
            value = els.nth(i).get_attribute(attr)
            if not value:
                continue
            resolved = page.evaluate("([base, rel]) => new URL(rel, base).toString()", [page.url, value])
            urls_to_try.append(resolved)

    # Semillas desde respuestas de red capturadas.
    for sample_url, content_type, body in network_samples:
        urls_to_try.append(sample_url)
        urls_to_try.extend(extract_pdf_urls_from_url(sample_url, page.url))

        if body and ("text" in content_type or "json" in content_type or "javascript" in content_type):
            text = body.decode("utf-8", errors="ignore")
            urls_to_try.extend(extract_pdf_urls_from_text(text, sample_url))
            urls_to_try.extend(extract_candidate_urls_from_text(text, sample_url))

    # Intento directo de cada candidato.
    for candidate in dict.fromkeys(urls_to_try):
        if url_pattern and not url_pattern.search(candidate):
            continue
        try:
            resp = page.context.request.get(candidate)
        except Exception:
            continue
        if not resp.ok:
            continue

        body = resp.body()
        if body and looks_like_pdf(body):
            return candidate, body

        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/pdf" in ctype:
            return candidate, body

        if "text" in ctype or "json" in ctype or "javascript" in ctype:
            text = resp.text()
            nested_urls = extract_pdf_urls_from_text(text, candidate)
            nested_urls.extend(extract_candidate_urls_from_text(text, candidate))
            for nested in dict.fromkeys(nested_urls):
                if url_pattern and not url_pattern.search(nested):
                    continue
                try:
                    nested_resp = page.context.request.get(nested)
                except Exception:
                    continue
                if not nested_resp.ok:
                    continue
                nested_body = nested_resp.body()
                if nested_body and looks_like_pdf(nested_body):
                    return nested, nested_body

    return None


def tertiary_iframe_strategy(
    page: Page,
    url_pattern: Optional[re.Pattern[str]],
    max_urls: int = 220,
) -> Optional[tuple[str, bytes]]:
    seeds: list[str] = []

    # Semillas desde iframe src/data-src.
    for attr in ("src", "data-src"):
        els = page.locator(f"iframe[{attr}]")
        for i in range(els.count()):
            value = els.nth(i).get_attribute(attr)
            if value:
                seeds.append(urljoin(page.url, value))

    # Semillas desde frames ya cargados.
    for frame in page.frames:
        if frame.url and frame.url != "about:blank":
            seeds.append(frame.url)
        try:
            resources = frame.evaluate("() => performance.getEntriesByType('resource').map(e => e.name)")
            if isinstance(resources, list):
                seeds.extend([str(r) for r in resources if isinstance(r, str)])
        except Exception:
            pass

    queue = list(dict.fromkeys(seeds))
    print(f"Estrategia iframe profunda: {len(queue)} semillas iniciales.")
    visited: set[str] = set()

    while queue and len(visited) < max_urls:
        candidate = queue.pop(0)
        if candidate in visited:
            continue
        visited.add(candidate)

        if url_pattern and not url_pattern.search(candidate):
            pass

        try:
            resp = page.context.request.get(candidate)
        except Exception:
            continue
        if not resp.ok:
            continue

        body = resp.body()
        if body and looks_like_pdf(body):
            return candidate, body

        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/pdf" in ctype:
            return candidate, body

        if "text" in ctype or "json" in ctype or "javascript" in ctype or "html" in ctype:
            text = resp.text()
            discovered: list[str] = []
            discovered.extend(extract_pdf_urls_from_text(text, candidate))
            discovered.extend(extract_candidate_urls_from_text(text, candidate))
            discovered.extend(extract_pdf_urls_from_url(candidate, candidate))

            # script/src/href extractions (aunque no tengan "pdf" en la URL).
            for raw in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", text, flags=re.IGNORECASE):
                discovered.append(urljoin(candidate, raw))

            for nxt in dict.fromkeys(discovered):
                if nxt not in visited:
                    queue.append(nxt)

    print(f"Estrategia iframe profunda: exploradas {len(visited)} URLs sin PDF.")
    return None


def get_iframe_urls(page: Page) -> list[str]:
    urls: list[str] = []
    for attr in ("src", "data-src"):
        els = page.locator(f"iframe[{attr}]")
        for i in range(els.count()):
            value = els.nth(i).get_attribute(attr)
            if value:
                urls.append(urljoin(page.url, value))
    for frame in page.frames:
        if frame.url and frame.url != "about:blank":
            urls.append(frame.url)
    return list(dict.fromkeys(urls))


def request_bytes_with_cookie_header(page: Page, target_url: str, referer: str) -> Optional[bytes]:
    # fallback explícito: request HTTP con header Cookie armado desde el contexto autenticado.
    try:
        cookies = page.context.cookies([target_url])
    except Exception:
        cookies = []
    cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")])
    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        user_agent = "Mozilla/5.0"

    headers = {
        "User-Agent": str(user_agent),
        "Referer": referer,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        req = UrlRequest(target_url, headers=headers)
        with urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception:
        return None


def authenticated_iframe_fetch_strategy(
    page: Page,
    url_pattern: Optional[re.Pattern[str]],
) -> Optional[tuple[str, bytes]]:
    iframe_urls = get_iframe_urls(page)
    print(f"Estrategia cookies+iframe: {len(iframe_urls)} iframes/frames detectados.")

    candidates: list[str] = []
    for iframe_url in iframe_urls:
        candidates.append(iframe_url)
        candidates.extend(extract_pdf_urls_from_url(iframe_url, page.url))
        try:
            resp = page.context.request.get(iframe_url)
            if not resp.ok:
                continue
            body = resp.body()
            if body and looks_like_pdf(body):
                return iframe_url, body
            content_type = (resp.headers.get("content-type") or "").lower()
            if "text" in content_type or "json" in content_type or "javascript" in content_type or "html" in content_type:
                text = resp.text()
                candidates.extend(extract_pdf_urls_from_text(text, iframe_url))
                candidates.extend(extract_candidate_urls_from_text(text, iframe_url))
        except Exception:
            continue

    for candidate in dict.fromkeys(candidates):
        if url_pattern and not url_pattern.search(candidate):
            continue
        try:
            resp = page.context.request.get(candidate)
            if resp.ok:
                body = resp.body()
                if body and looks_like_pdf(body):
                    return candidate, body
                if "application/pdf" in (resp.headers.get("content-type") or "").lower():
                    return candidate, body
        except Exception:
            pass

        body2 = request_bytes_with_cookie_header(page, candidate, referer=page.url)
        if body2 and looks_like_pdf(body2):
            return candidate, body2

    return None


def expect_download_strategy(page: Page, output_path: Path, timeout_sec: int = 20) -> bool:
    timeout_ms = max(1, timeout_sec) * 1000
    click_selectors = [
        "a[download]",
        "a[href*='.pdf']",
        "a[href*='download']",
        "button:has-text('Descargar')",
        "button:has-text('Download')",
    ]

    for sel in click_selectors:
        loc = page.locator(sel)
        if loc.count() < 1:
            continue
        try:
            with page.expect_download(timeout=timeout_ms) as dl_info:
                loc.first.click()
            dl = dl_info.value
            dl.save_as(str(output_path))
            return True
        except Exception:
            continue

    # intenta en cada frame
    for frame in page.frames:
        for sel in click_selectors:
            try:
                loc = frame.locator(sel)
                if loc.count() < 1:
                    continue
                with page.expect_download(timeout=timeout_ms) as dl_info:
                    loc.first.click()
                dl = dl_info.value
                dl.save_as(str(output_path))
                return True
            except Exception:
                continue

    # último intento: navegar directo a iframe en una pestaña temporal
    for iframe_url in get_iframe_urls(page):
        temp_page: Optional[Page] = None
        try:
            temp_page = page.context.new_page()
            with temp_page.expect_download(timeout=timeout_ms) as dl_info:
                temp_page.goto(iframe_url, wait_until="domcontentloaded")
            dl = dl_info.value
            dl.save_as(str(output_path))
            return True
        except Exception:
            continue
        finally:
            if temp_page:
                try:
                    temp_page.close()
                except Exception:
                    pass

    return False


def print_to_pdf_fallback(page: Page, output_path: Path) -> bool:
    try:
        page.emulate_media(media="screen")
    except Exception:
        pass

    # primer intento: página actual
    try:
        page.pdf(path=str(output_path), print_background=True, prefer_css_page_size=True)
        return True
    except Exception:
        pass

    # segundo intento: imprimir iframe principal en pestaña temporal
    for iframe_url in get_iframe_urls(page):
        temp_page: Optional[Page] = None
        try:
            temp_page = page.context.new_page()
            temp_page.goto(iframe_url, wait_until="domcontentloaded", timeout=20000)
            temp_page.pdf(path=str(output_path), print_background=True, prefer_css_page_size=True)
            return True
        except Exception:
            continue
        finally:
            if temp_page:
                try:
                    temp_page.close()
                except Exception:
                    pass

    return False


def page_is_active(page: Page) -> bool:
    try:
        return bool(page.evaluate("() => document.visibilityState === 'visible'"))
    except Exception:
        return False


def get_active_page(browser: Browser) -> Optional[Page]:
    for context in browser.contexts:
        for page in context.pages:
            if page_is_active(page):
                return page
    for context in browser.contexts:
        if context.pages:
            return context.pages[0]
    return None


def get_live_page(browser: Browser, current: Optional[Page]) -> Optional[Page]:
    if current is not None:
        try:
            if not current.is_closed():
                return current
        except Exception:
            pass
    return get_active_page(browser)


def save_bytes(data: bytes, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        low = key.lower()
        if low in {"authorization", "cookie", "set-cookie"}:
            out[key] = "[redacted]"
        else:
            out[key] = value
    return out


def body_to_limited_b64(data: bytes, limit: int) -> tuple[str, bool, int]:
    capped = data[: max(0, limit)]
    encoded = base64.b64encode(capped).decode("ascii")
    return encoded, len(data) > len(capped), len(data)


def write_debug_json(debug_path: str, args: argparse.Namespace, output_path: Path, entries: list[dict]) -> None:
    payload = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "Playwright captura trafico HTTP/HTTPS del navegador controlado. "
            "TCP crudo no esta disponible desde este script."
        ),
        "args": {
            "cdp": args.cdp,
            "url": args.url,
            "output": str(output_path),
            "timeout": args.timeout,
            "match": args.match,
            "profileDirectory": args.profile_directory,
        },
        "trafficCount": len(entries),
        "traffic": entries,
    }
    abs_path = Path(debug_path).expanduser().resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Debug HTTP/HTTPS guardado en: {abs_path}")


def try_direct_pdf_sources(page: Page, url_pattern: Optional[re.Pattern[str]]) -> Optional[bytes]:
    selectors = [
        "iframe[src]",
        "embed[src]",
        "object[data]",
        "a[href$='.pdf']",
        "a[href*='.pdf?']",
    ]

    candidates: list[str] = []
    for sel in selectors:
        attr = "src" if "iframe" in sel or "embed" in sel else "data" if "object" in sel else "href"
        els = page.locator(sel)
        for i in range(els.count()):
            value = els.nth(i).get_attribute(attr)
            if not value:
                continue
            url = page.url if value.startswith("javascript:") else value
            resolved = page.evaluate("([base, rel]) => new URL(rel, base).toString()", [page.url, url])
            candidates.append(resolved)

    unique_candidates = list(dict.fromkeys(candidates))
    for url in unique_candidates:
        if url_pattern and not url_pattern.search(url):
            continue
        # Caso pdf.js: URL del visor con ?file=<pdf_url>.
        for nested in extract_pdf_urls_from_url(url, page.url):
            if url_pattern and not url_pattern.search(nested):
                continue
            nested_resp = page.context.request.get(nested)
            nested_body = nested_resp.body() if nested_resp.ok else b""
            if nested_body and looks_like_pdf(nested_body):
                return nested_body

        resp = page.context.request.get(url)
        if not resp.ok:
            continue

        body = resp.body()
        if body and looks_like_pdf(body):
            return body

        content_type = (resp.headers.get("content-type", "") or "").lower()
        if "text/html" in content_type or "javascript" in content_type:
            text = resp.text()
            for extracted in extract_pdf_urls_from_text(text, url):
                if url_pattern and not url_pattern.search(extracted):
                    continue
                extracted_resp = page.context.request.get(extracted)
                if not extracted_resp.ok:
                    continue
                extracted_body = extracted_resp.body()
                if extracted_body and looks_like_pdf(extracted_body):
                    return extracted_body
    return None


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    pattern = re.compile(args.match) if args.match else None

    with sync_playwright() as pw:
        if not cdp_is_available(args.cdp):
            if args.auto_launch_chrome:
                print("CDP no estaba activo. Intentando iniciarlo...")
                launched = launch_chrome_for_cdp(args)
                if not launched:
                    print(
                        "No fue posible activar CDP con tu configuración actual.",
                        file=sys.stderr,
                    )
            else:
                print("CDP no disponible y --auto-launch-chrome no fue especificado.", file=sys.stderr)

        try:
            browser = pw.chromium.connect_over_cdp(args.cdp)
        except Exception as exc:
            print(
                "No se pudo conectar a CDP. Si tu Chrome principal está abierto sin "
                "--remote-debugging-port, esa instancia no se puede adjuntar en caliente.\n"
                f"Detalle: {exc}",
                file=sys.stderr,
            )
            return 1

        page = get_active_page(browser)
        if not page:
            print("No se encontró ninguna pestaña en la sesión de Chrome conectada.", file=sys.stderr)
            browser.close()
            return 1

        context = page.context
        debug_entries: list[dict] = []
        request_meta: dict[int, dict] = {}

        def finalize(code: int) -> int:
            if args.debug_json:
                write_debug_json(args.debug_json, args, output_path, debug_entries)
            try:
                context.remove_listener("request", on_request_debug)
                context.remove_listener("response", on_response_debug)
            except Exception:
                pass
            browser.close()
            return code

        def on_request_debug(req) -> None:
            if not args.debug_json:
                return
            try:
                request_meta[id(req)] = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "url": req.url,
                    "method": req.method,
                    "resourceType": req.resource_type,
                    "headers": sanitize_headers(req.all_headers()),
                    "postData": req.post_data,
                }
            except Exception:
                return

        def on_response_debug(resp: Response) -> None:
            if not args.debug_json:
                return
            try:
                req = resp.request
                req_data = request_meta.get(
                    id(req),
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "url": req.url,
                        "method": req.method,
                        "resourceType": req.resource_type,
                        "headers": {},
                        "postData": req.post_data,
                    },
                )
                body = resp.body()
                body_b64, body_truncated, body_size = body_to_limited_b64(body, args.debug_body_limit)
                debug_entries.append(
                    {
                        "request": req_data,
                        "response": {
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "url": resp.url,
                            "status": resp.status,
                            "ok": resp.ok,
                            "headers": sanitize_headers(resp.headers),
                            "bodyBase64": body_b64,
                            "bodyTruncated": body_truncated,
                            "bodySize": body_size,
                        },
                    }
                )
            except Exception:
                return

        context.on("request", on_request_debug)
        context.on("response", on_response_debug)

        print(f"Pestaña inicial: {page.url}")
        if args.url:
            print(f"Navegando a: {args.url}")
            page.goto(args.url, wait_until="domcontentloaded")
            print(f"URL cargada: {page.url}")

        data = try_direct_pdf_sources(page, pattern)
        if data:
            save_bytes(data, output_path)
            print(f"PDF guardado en: {output_path}")
            return finalize(0)

        captured: dict[str, bytes] = {}
        network_samples: list[tuple[str, str, bytes]] = []

        def on_response(resp: Response) -> None:
            try:
                body = resp.body()
                if not body or not resp.ok:
                    return

                content_type = (resp.headers.get("content-type") or "").lower()
                lower_url = resp.url.lower()
                interesting = (
                    "text/" in content_type
                    or "json" in content_type
                    or "javascript" in content_type
                    or "pdf" in content_type
                    or "octet-stream" in content_type
                    or any(k in lower_url for k in ("visor", "regimen", "pdf", "download", "file", "document"))
                )
                if interesting and len(network_samples) < 250:
                    network_samples.append((resp.url, content_type, body[:600000]))

                # Acepta PDF por encabezados o por firma binaria.
                if (not pattern or pattern.search(resp.url)) and (is_pdf_response(resp) or looks_like_pdf(body)):
                    captured[resp.url] = body
            except Exception:
                return

        page.on("response", on_response)
        try:
            page.reload(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass

        deadline = time.time() + args.timeout
        while time.time() < deadline and not captured:
            live_page = get_live_page(browser, page)
            if not live_page:
                print("La pestaña se cerró y no hay otra pestaña activa para continuar.")
                break
            if live_page is not page:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
                page = live_page
                page.on("response", on_response)
                print(f"Cambiando a pestaña activa: {page.url}")

            try:
                page.wait_for_timeout(300)
            except Exception:
                live_page = get_live_page(browser, page)
                if not live_page:
                    print("La página/contexto se cerró durante la espera.")
                    break
                if live_page is not page:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass
                    page = live_page
                    page.on("response", on_response)
                else:
                    break

        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

        if not captured:
            print("No hubo PDF directo. Ejecutando estrategia secundaria...")
            fallback = secondary_pdf_strategy(page, pattern, network_samples)
            if fallback:
                fallback_url, fallback_pdf = fallback
                save_bytes(fallback_pdf, output_path)
                print(f"PDF encontrado con estrategia secundaria: {fallback_url}")
                print(f"PDF guardado en: {output_path}")
                return finalize(0)

            print("Estrategia secundaria sin éxito. Ejecutando estrategia iframe profunda...")
            fallback2 = tertiary_iframe_strategy(page, pattern)
            if fallback2:
                fallback2_url, fallback2_pdf = fallback2
                save_bytes(fallback2_pdf, output_path)
                print(f"PDF encontrado en estrategia iframe profunda: {fallback2_url}")
                print(f"PDF guardado en: {output_path}")
                return finalize(0)

            print("Estrategia iframe profunda sin éxito. Ejecutando cookies+iframe request...")
            fallback3 = authenticated_iframe_fetch_strategy(page, pattern)
            if fallback3:
                fallback3_url, fallback3_pdf = fallback3
                save_bytes(fallback3_pdf, output_path)
                print(f"PDF encontrado por cookies+iframe: {fallback3_url}")
                print(f"PDF guardado en: {output_path}")
                return finalize(0)

            print("Cookies+iframe sin éxito. Ejecutando expect_download/content-disposition...")
            if expect_download_strategy(page, output_path, timeout_sec=max(20, args.timeout // 2)):
                print(f"Archivo descargado por expect_download en: {output_path}")
                return finalize(0)

            print("expect_download sin éxito. Ejecutando print-to-PDF como último recurso...")
            if print_to_pdf_fallback(page, output_path):
                print(f"PDF generado por impresión de página en: {output_path}")
                return finalize(0)

            print(
                "No se capturó ningún PDF dentro del tiempo límite. "
                "También fallaron cookies+iframe, expect_download y print-to-PDF.",
                file=sys.stderr,
            )
            return finalize(2)

        url, pdf_bytes = next(iter(captured.items()))
        save_bytes(pdf_bytes, output_path)
        print(f"PDF capturado desde: {url}")
        print(f"PDF guardado en: {output_path}")
        return finalize(0)


if __name__ == "__main__":
    raise SystemExit(main())
