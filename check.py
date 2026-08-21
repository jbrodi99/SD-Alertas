#!/usr/bin/env python3
"""
Recorre pages.json, compara cada página contra su snapshot anterior
(guardado en snapshots/) y notifica a Discord si hubo cambios.

Además trackea disponibilidad: si una página deja de responder o vuelve
a responder, notifica el cambio de estado (no cada corrida mientras
sigue caída).
"""

import json
import os
import sys
import time
import difflib
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

SNAP_DIR = Path("snapshots")
STATUS_PATH = SNAP_DIR / "_status.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DIFF_CHAR_LIMIT = 800  # Discord: 2000 chars por mensaje, dejamos margen

MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 5


def slugify(url: str) -> str:
    path = urlparse(url).path.strip("/")
    slug = path.replace("/", "_")
    return slug or "index"


def normalize(html: str) -> str:
    """Texto visible del body, sin scripts/estilos, líneas colapsadas."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = soup.get_text("\n").split("\n")
    return "\n".join(line.strip() for line in lines if line.strip())


def fetch(url: str) -> tuple[str | None, str | None]:
    """Intenta bajar la URL con reintentos. Devuelve (html, error).

    Un timeout aislado no significa que el sitio esté caído (falacia
    "la red es confiable") — por eso reintentamos antes de declarar
    la página como caída.
    """
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            return resp.text, None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
    return None, last_error


def load_status() -> dict:
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    return {}


def save_status(status: dict) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def notify_discord(message: str) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL no está seteado, salteo notificación.", file=sys.stderr)
        return
    resp = requests.post(WEBHOOK_URL, json={"content": message[:1990]}, timeout=15)
    if not resp.ok:
        print(f"Error notificando a Discord: {resp.status_code} {resp.text}", file=sys.stderr)


def check_page(url: str, status: dict) -> list[dict]:
    """Chequea una página. Devuelve 0, 1 o 2 eventos (disponibilidad y/o cambio)."""
    events = []
    slug = slugify(url)
    was_failing = status.get(slug, {}).get("failing", False)

    html, error = fetch(url)

    if html is None:
        status[slug] = {"failing": True, "last_error": error}
        if was_failing:
            print(f"[sigue caído] {url}: {error}")
        else:
            print(f"[CAIDO] {url}: {error}")
            events.append({"kind": "down", "url": url, "detail": error})
        return events  # sin contenido, no hay nada más para chequear

    status[slug] = {"failing": False, "last_error": None}
    if was_failing:
        print(f"[RECUPERADO] {url}")
        events.append({"kind": "up", "url": url})

    current = normalize(html)
    snap_path = SNAP_DIR / f"{slug}.txt"
    previous = snap_path.read_text(encoding="utf-8") if snap_path.exists() else None

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(current, encoding="utf-8")

    if previous is None:
        print(f"[baseline] {url}")
        return events

    if previous == current:
        print(f"[sin cambios] {url}")
        return events

    diff_lines = [
        line
        for line in difflib.ndiff(previous.splitlines(), current.splitlines())
        if line.startswith("+ ") or line.startswith("- ")
    ]
    diff = "\n".join(diff_lines)[:DIFF_CHAR_LIMIT]
    print(f"[CAMBIO] {url}")
    events.append({"kind": "change", "url": url, "diff": diff})
    return events


def format_event(event: dict) -> str:
    if event["kind"] == "down":
        return f"🔴 **Dejó de responder**\n{event['url']}\n`{event['detail']}`"
    if event["kind"] == "up":
        return f"🟢 **Volvió a responder**\n{event['url']}"
    return (
        "📢 **Cambio detectado en la web de la cátedra**\n"
        f"{event['url']}\n```diff\n{event['diff']}\n```"
    )


def main() -> None:
    pages = json.loads(Path("pages.json").read_text(encoding="utf-8"))
    status = load_status()
    all_events = []

    try:
        for url in pages:
            all_events.extend(check_page(url, status))
    finally:
        # Guardamos el estado incluso si algo falló a mitad de camino,
        # para no perder el tracking de disponibilidad ya actualizado.
        save_status(status)

    if not all_events:
        print("Sin novedades en esta corrida.")
        return

    for event in all_events:
        notify_discord(format_event(event))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # salvavidas: algo no contemplado explotó
        print(f"Fallo inesperado del watcher: {exc}", file=sys.stderr)
        try:
            notify_discord(
                "⚠️ **El watcher mismo falló** (excepción no manejada).\n"
                f"`{exc}`\nRevisá los logs del run en GitHub Actions."
            )
        except Exception:
            print("Además falló el intento de notificar el crash.", file=sys.stderr)
        sys.exit(1)
