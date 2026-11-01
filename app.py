"""Descargador de ediciones: páginas validadas, resultados persistentes y entrega por bloques."""
from __future__ import annotations
import gc
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict
from zoneinfo import ZoneInfo
import uuid
import zipfile

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_PART_IMAGE_BYTES = 128 * 1024 * 1024
MAX_RETRY_WAIT = 15
PAGE_DOWNLOAD_SECONDS = 60
MAX_WORKER_RSS = 512 * 1024 * 1024
WORKER_STALL_SECONDS = 300
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
SESSION_SCHEMA = 1
LOGGER = logging.getLogger("urls2pdf2zip")

EDITIONS: Dict[str, Dict[str, object]] = {
    "Milenio Nacional": {
        "type": "milenio",
        "folder": "Nacional",
        "code": "NAC",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio León": {
        "type": "milenio",
        "folder": "Leon",
        "code": "LEO",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio Tamaulipas": {
        "type": "milenio",
        "folder": "Tamaulipas",
        "code": "TAM",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio Jalisco": {
        "type": "milenio",
        "folder": "Jalisco",
        "code": "JAL",
        "start_page": 0,
        "default_attempts": 86,
    },
    "Milenio Puebla": {
        "type": "milenio",
        "folder": "Puebla",
        "code": "PUE",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio Monterrey": {
        "type": "milenio",
        "folder": "Monterrey",
        "code": "MON",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio Edomex": {
        "type": "milenio",
        "folder": "Edomex",
        "code": "MEX",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio Hidalgo": {
        "type": "milenio",
        "folder": "Hidalgo",
        "code": "HID",
        "start_page": 0,
        "default_attempts": 60,
    },
    "Milenio Laguna": {
        "type": "milenio",
        "folder": "Laguna",
        "code": "LAG",
        "start_page": 0,
        "default_attempts": 60,
    },    
    "El Universal": {
        "type": "el_universal",
        "start_page": 1,
        "default_attempts": 80,
    },
    "La Afición": {
        "type": "milenio",
        "folder": "Nacional",
        "code": "LFNE",
        "start_page": 0,
        "default_attempts": 60,
    },
}

def sanitize_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^\w.-]", "", name, flags=re.UNICODE)
    return name

def format_yyyymmdd(dt: date) -> str:
    return dt.strftime("%Y%m%d")

def format_ddmmyy(dt: date) -> str:
    return dt.strftime("%d%m%y")

def build_url(edition_name: str, dt: date, page: int) -> str:
    edition = EDITIONS[edition_name]
    edition_type = edition["type"]

    if edition_type == "milenio":
        folder = edition["folder"]
        code = edition["code"]
        return (
            f"https://api-epaper.milenio.com/file/"
            f"{folder}/{code}/{format_yyyymmdd(dt)}/images/{page}.jpg"
        )

    if edition_type == "el_universal":
        return (
            f"https://edicionimpresa.eluniversal.com.mx/archive/"
            f"eu{format_ddmmyy(dt)}/files/pages/tablet/{page}.jpg"
        )

    raise ValueError("Edición no soportada")

def build_headers(url: str) -> Dict[str, str]:
    if "eluniversal.com.mx" in url:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "image/jpeg,image/png,image/webp;q=0.9",
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
            "Referer": "https://edicionimpresa.eluniversal.com.mx/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/jpeg,image/png,image/webp;q=0.9",
    }

class DownloadError(Exception):
    def __init__(self, message: str, kind: str = "source", status_code=None, retry_after=None):
        self.kind, self.status_code, self.retry_after = kind, status_code, retry_after
        super().__init__(message)


class PageMissing(DownloadError):
    pass


class ConversionError(Exception):
    pass


def _retry_delay(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _normalize_image(raw_path: Path, destination: Path) -> dict:
    from PIL import Image, ImageOps, UnidentifiedImageError

    normalized = destination.with_suffix(destination.suffix + ".part")
    try:
        with Image.open(raw_path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise DownloadError("El servidor no devolvió una imagen JPEG, PNG o WebP válida.", "image")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise DownloadError("La imagen excede el tamaño de página que puede procesar este servidor.", "size")
            source_format = image.format
            # open() sólo lee la cabecera: load() descubre archivos truncados.
            image.load()
            orientation = image.getexif().get(274, 1)
            original_jpeg = source_format == "JPEG" and image.mode in {"RGB", "L"} and orientation in {None, 1}
            if original_jpeg:
                width, height = image.size
            else:
                oriented = ImageOps.exif_transpose(image)
                try:
                    if "A" in oriented.getbands() or "transparency" in oriented.info:
                        with oriented.convert("RGBA") as rgba:
                            rgb = Image.new("RGB", rgba.size, "white")
                            with rgba.getchannel("A") as alpha:
                                rgb.paste(rgba, mask=alpha)
                    else:
                        rgb = oriented.convert("RGB")
                    try:
                        width, height = rgb.size
                        rgb.save(normalized, format="JPEG", quality=95, subsampling=0)
                    finally:
                        rgb.close()
                finally:
                    oriented.close()
        if original_jpeg:
            raw_path.replace(destination)
        else:
            normalized.replace(destination)
        return {"path": str(destination), "width": width, "height": height,
                "size_bytes": destination.stat().st_size, "source_format": source_format,
                "normalized": not original_jpeg}
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        # Errores de disco no deben confundirse con una página ausente.
        if isinstance(exc, OSError) and exc.errno is not None:
            raise
        raise DownloadError("La respuesta contiene una imagen dañada o incompleta.", "image") from exc
    finally:
        normalized.unlink(missing_ok=True)


def fetch_image(url: str, destination: Path, *, session, timeout=(8, 20), retries=2) -> dict:
    """Descarga y valida una página en disco; reintenta la misma URL si procede."""
    import requests

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw_path = destination.with_suffix(".download.part")
    for attempt in range(retries + 1):
        wait = 0.5 * (2 ** attempt)
        try:
            started = time.monotonic()
            with session.get(url, headers=build_headers(url), timeout=timeout,
                             allow_redirects=True, verify=True, stream=True) as response:
                code = response.status_code
                if code in (404, 410):
                    raise PageMissing(f"Página no disponible (HTTP {code}).", "missing", code)
                if code in (401, 403):
                    raise DownloadError(f"El servidor del periódico rechazó el acceso (HTTP {code}).", "access", code)
                if code in (408, 425, 429) or 500 <= code < 600:
                    retry_after = _retry_delay(response.headers.get("Retry-After"))
                    raise DownloadError(f"El servidor respondió HTTP {code}.", "transient", code, retry_after)
                if code != 200:
                    raise DownloadError(f"Respuesta inesperada del servidor: HTTP {code}.", "http", code)
                content_type = (response.headers.get("Content-Type") or "").lower().split(";", 1)[0]
                if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                    raise DownloadError(f"El servidor devolvió {content_type}, no una imagen.", "image")
                declared = response.headers.get("Content-Length", "")
                declared_size = int(declared) if declared.isdigit() else None
                if declared_size is not None and declared_size > MAX_IMAGE_BYTES:
                    raise DownloadError("La página supera el tamaño admitido para una imagen individual.", "size")
                received = 0
                with raw_path.open("wb") as target:
                    for chunk in response.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > MAX_IMAGE_BYTES:
                            raise DownloadError("La página supera el tamaño admitido para una imagen individual.", "size")
                        if time.monotonic() - started > PAGE_DOWNLOAD_SECONDS:
                            raise DownloadError("La descarga de la página tardó demasiado.", "transient")
                        target.write(chunk)
                if not received:
                    raise DownloadError("El servidor devolvió una respuesta vacía.", "transient")
                if (declared_size is not None and not response.headers.get("Content-Encoding")
                        and received != declared_size):
                    raise DownloadError("La descarga de la imagen quedó incompleta.", "transient")
            metadata = _normalize_image(raw_path, destination)
            metadata.update(url=url, content_type="image/jpeg", download_attempts=attempt + 1)
            return metadata
        except PageMissing:
            raise
        except requests.exceptions.SSLError as exc:
            raise DownloadError("No se pudo verificar el certificado HTTPS del periódico.", "tls") from exc
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            error = DownloadError("Se interrumpió la conexión al descargar esta página.", "transient")
            error.__cause__ = exc
        except requests.RequestException as exc:
            raise DownloadError("No se pudo completar la solicitud al periódico.", "network") from exc
        except DownloadError as exc:
            if exc.kind != "transient":
                raise
            error = exc
        finally:
            raw_path.unlink(missing_ok=True)
        if error.retry_after is not None:
            if error.retry_after > MAX_RETRY_WAIT:
                raise DownloadError(
                    f"El servidor pide esperar {math.ceil(error.retry_after)} segundos antes de reintentar. Vuelve a buscar más tarde.",
                    "rate_limit", error.status_code)
            wait = max(wait, error.retry_after)
        if attempt >= retries:
            raise DownloadError(f"{error} Se agotaron {retries + 1} intentos en la misma página.",
                                error.kind, error.status_code) from error
        LOGGER.warning("Reintento %s/%s de %s: %s", attempt + 1, retries, url, error)
        time.sleep(wait)
    raise DownloadError("No se pudo descargar la página.")


def generate_pdf(images_data: list[dict], output_path: Path) -> Path:
    from reportlab import rl_config
    from reportlab.pdfgen import canvas

    if not images_data:
        raise ConversionError("No hay páginas válidas para generar el PDF.")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".pdf.part")
    old_ascii85 = rl_config.useA85
    try:
        rl_config.useA85 = False
        pdf = canvas.Canvas(str(temporary), pageCompression=1)
        pdf.setTitle(output_path.stem)
        for item in images_data:
            width, height = item["width"], item["height"]
            pdf.setPageSize((width, height))
            # Pasar la ruta incorpora el JPEG comprimido sin un ImageReader RGB adicional.
            pdf.drawImage(item["path"], 0, 0, width=width, height=height)
            pdf.showPage()
        pdf.save()
        with temporary.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise ConversionError("La generación no produjo un PDF válido.")
        temporary.replace(output_path)
        return output_path
    finally:
        rl_config.useA85 = old_ascii85
        temporary.unlink(missing_ok=True)


def generate_zip(images_data: list[dict], output_path: Path) -> Path:
    if not images_data:
        raise ConversionError("No hay páginas válidas para generar el ZIP.")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for item in images_data:
                archive.write(item["path"], arcname=item["filename"])
        temporary.replace(output_path)
        return output_path
    finally:
        temporary.unlink(missing_ok=True)


def _page_groups(pages, limit=MAX_PART_IMAGE_BYTES):
    group, total = [], 0
    for page in pages:
        if group and total + page["size_bytes"] > limit:
            yield group
            group, total = [], 0
        group.append(page)
        total += page["size_bytes"]
    if group:
        yield group


def download_edition(edition_name, selected_date, output_dir, *, max_attempts=60,
                     max_consecutive_missing=2, delay_seconds=0.0, include_zip=True,
                     progress=None, session=None) -> dict:
    import requests

    if edition_name not in EDITIONS:
        raise ValueError("Edición no soportada")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 300:
        raise ValueError("El máximo debe estar entre 1 y 300 páginas.")
    if type(max_consecutive_missing) is not int or not 1 <= max_consecutive_missing <= 10:
        raise ValueError("El límite de páginas ausentes debe estar entre 1 y 10.")
    if not isinstance(selected_date, date) or not 0 <= delay_seconds <= 5:
        raise ValueError("Fecha o pausa no válida.")
    output_dir = Path(output_dir)
    images_dir = output_dir / "paginas"
    images_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{sanitize_name(edition_name)}_{selected_date.isoformat()}"
    result = {"edition_name": edition_name, "date": selected_date.isoformat(), "pages": [],
              "files": [], "missing_pages": [], "failed_pages": [], "logs": [], "errors": [],
              "stop_reason": "attempt_limit", "include_zip": bool(include_zip)}
    pages, logs = result["pages"], result["logs"]
    missing, consecutive = [], 0
    own_session = session is None
    session = session or requests.Session()

    def update(message, stage="download", page=None, attempts=0):
        if progress:
            progress({"message": message, "stage": stage, "page": page, "attempts": attempts,
                      "max_attempts": max_attempts, "valid": len(pages), "logs": logs[-12:]})

    try:
        first = int(EDITIONS[edition_name]["start_page"])
        for attempt, page in enumerate(range(first, first + max_attempts), 1):
            url = build_url(edition_name, selected_date, page)
            update(f"Descargando página {page}…", page=page, attempts=attempt)
            filename = f"{base_name}_pagina_{page:03d}.jpg"
            try:
                image = fetch_image(url, images_dir / filename, session=session)
                image.update(page_number=page, filename=filename)
                pages.append(image)
                consecutive = 0
                logs.append(f"Página {page}: imagen válida descargada.")
            except PageMissing as exc:
                consecutive += 1
                missing.append(page)
                logs.append(f"Página {page}: {exc} Ausentes consecutivas: {consecutive}/{max_consecutive_missing}.")
                if consecutive >= max_consecutive_missing:
                    result["stop_reason"] = "not_found"
                    update("Se alcanzó el límite de páginas ausentes.", page=page, attempts=attempt)
                    break
            except (DownloadError, OSError) as exc:
                kind = exc.kind if isinstance(exc, DownloadError) else "disk"
                message = str(exc) if isinstance(exc, DownloadError) else "No se pudo guardar la página en el servidor."
                result["failed_pages"].append({"page_number": page, "url": url, "kind": kind, "error": message})
                logs.append(f"Página {page}: {message} La búsqueda se detuvo con resultados parciales.")
                LOGGER.warning("Error en %s página %s: %s", edition_name, page, exc)
                result["stop_reason"] = "source_error"
                break
            update(f"Páginas válidas: {len(pages)}", page=page, attempts=attempt)
            if delay_seconds and attempt < max_attempts:
                time.sleep(float(delay_seconds))
    finally:
        if own_session:
            session.close()
    result["attempts"] = attempt
    if not pages:
        result["status"] = "error"
        result["errors"].append("No se encontró ninguna página válida para generar archivos.")
        return result
    result["missing_pages"] = [page for page in missing if page < pages[-1]["page_number"]]
    partial = result["stop_reason"] != "not_found" or bool(result["missing_pages"])
    result["status"] = "partial" if partial else "complete"
    groups = list(_page_groups(pages))
    suffix = "_parcial" if partial else ""
    for number, group in enumerate(groups, 1):
        group_suffix = f"_parte_{number:03d}" if len(groups) > 1 else ""
        for extension, generator, mime in [("pdf", generate_pdf, "application/pdf"),
                                            *([("zip", generate_zip, "application/zip")] if include_zip else [])]:
            filename = f"{base_name}{suffix}{group_suffix}.{extension}"
            output_path = output_dir / filename
            update(f"Generando {filename}…", stage="generate", attempts=result["attempts"])
            try:
                generator(group, output_path)
                result["files"].append({"id": uuid.uuid4().hex, "path": str(output_path),
                    "file_name": filename, "mime": mime, "size_bytes": output_path.stat().st_size,
                    "pages": [p["page_number"] for p in group]})
            except Exception as exc:
                LOGGER.exception("No se pudo generar %s", filename)
                output_path.unlink(missing_ok=True)
                result["errors"].append(f"No se pudo generar {filename}: {type(exc).__name__}. Los demás resultados válidos se conservan.")
                result["status"] = "partial"
            finally:
                gc.collect()
    if not result["files"]:
        result["status"] = "error"
    logs.append(f"Generación finalizada: {len(pages)} páginas válidas y {len(result['files'])} archivos disponibles.")
    return result


def _write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _worker_main(config_path):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    try:
        result = download_edition(config["edition_name"], date.fromisoformat(config["selected_date"]),
            config["output_dir"], max_attempts=config["max_attempts"],
            max_consecutive_missing=config["max_consecutive_missing"], delay_seconds=config["delay_seconds"],
            include_zip=config["include_zip"], progress=lambda event: _write_json(config["progress_path"], event))
        _write_json(config["result_path"], {"ok": True, "result": result})
        return 0
    except Exception as exc:
        LOGGER.exception("Falló la búsqueda/generación de la edición")
        _write_json(config["result_path"], {"ok": False, "error": f"No se pudo completar el proceso ({type(exc).__name__}). Consulta el registro del servidor."})
        return 1


def _worker_rss(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _stop_worker(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def run_download_job(options, job_dir, progress=None, heartbeat=None):
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir = job_dir / "resultados"
    config = dict(options, output_dir=str(output_dir), progress_path=str(job_dir / "progress.json"),
                  result_path=str(job_dir / "result.json"))
    _write_json(job_dir / "config.json", config)
    process, completed = None, False
    try:
        with (job_dir / "worker.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(job_dir / "config.json")],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                env=dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1"))
            last_event, last_progress = None, time.monotonic()
            while process.poll() is None:
                if heartbeat:
                    heartbeat()
                if _worker_rss(process.pid) > MAX_WORKER_RSS:
                    raise ConversionError("El proceso superó la memoria disponible para esta búsqueda. Prueba con menos páginas por búsqueda.")
                if time.monotonic() - last_progress > WORKER_STALL_SECONDS:
                    raise ConversionError("El proceso estuvo más de cinco minutos sin avanzar. Vuelve a intentarlo más tarde.")
                try:
                    event = json.loads((job_dir / "progress.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    event = None
                if event is not None and event != last_event:
                    last_event, last_progress = event, time.monotonic()
                    if progress:
                        progress(event)
                time.sleep(0.1)
            try:
                payload = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            log.seek(0)
            worker_log = log.read(20000)
            if process.returncode != 0 or not payload or not payload.get("ok"):
                LOGGER.error("Trabajador finalizó con código %s: %s", process.returncode, worker_log)
                raise ConversionError((payload or {}).get("error", "El proceso terminó inesperadamente. Vuelve a buscar la edición."))
            result = payload["result"]
            if result["status"] != "complete":
                LOGGER.warning("Resultado %s (%s): %s", result["status"], result["stop_reason"], worker_log)
            completed = True
            return result
    finally:
        _stop_worker(process)
        if not completed:
            shutil.rmtree(job_dir, ignore_errors=True)



def construir_mensaje_descarga(parts, batch: str, request=None, enabled: bool = True) -> bytes:
    """Entrega como máximo un bloque del archivo, identificado por sesión/lote/archivo.

    El cliente sólo puede pedir IDs publicados en este lote, nunca una ruta. Cada
    petición confirma el bloque anterior; no se conserva el archivo completo en RAM.
    """
    metadata = {"protocol": 3, "batch": batch, "enabled": bool(enabled),
                "files": [{"id": p["id"], "name": p["file_name"], "size": p["size_bytes"], "mime": p["mime"]} for p in parts],
                "response": None, "error": None}
    payload = b""
    if enabled and isinstance(request, dict) and request.get("batch") == batch:
        file_id, token, offset = request.get("id"), request.get("token"), request.get("offset")
        try:
            if (not isinstance(file_id, str) or not isinstance(token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token)
                    or type(offset) is not int or offset < 0 or offset % DOWNLOAD_CHUNK_BYTES):
                raise ConversionError("Solicitud de descarga no válida. Pulsa Reintentar descarga.")
            part = next((p for p in parts if p["id"] == file_id), None)
            if part is None:
                raise ConversionError("El archivo ya no pertenece a este lote. Vuelve a procesarlo.")
            path, total = Path(part["path"]), part["size_bytes"]
            if offset >= total or path.stat().st_size != total:
                raise ConversionError("El archivo cambió o quedó incompleto. Vuelve a generar esta edición.")
            with path.open("rb") as source:
                if offset == 0:
                    magic = source.read(5)
                    if part["mime"] == "application/zip":
                        if magic[:4] != b"PK\x03\x04" or not zipfile.is_zipfile(source):
                            raise ConversionError("El archivo no es válido. Vuelve a generar la edición.")
                    elif part["mime"] == "application/pdf":
                        source.seek(max(0, total - 1024))
                        if magic != b"%PDF-" or b"%%EOF" not in source.read():
                            raise ConversionError("El PDF no está completo. Vuelve a generar la edición.")
                    else:
                        raise ConversionError("Tipo de archivo no permitido.")
                source.seek(offset)
                payload = source.read(min(DOWNLOAD_CHUNK_BYTES, total - offset))
            if len(payload) != min(DOWNLOAD_CHUNK_BYTES, total - offset):
                raise ConversionError("No se pudo leer el archivo completo. Vuelve a generar esta edición.")
            metadata["response"] = {"id": file_id, "token": token, "offset": offset,
                                    "size": len(payload), "total": total,
                                    "last": offset + len(payload) == total}
        except (ConversionError, OSError) as exc:
            payload = b""
            metadata["error"] = {"id": file_id, "token": token,
                                 "message": str(exc) if isinstance(exc, ConversionError) else
                                 "El archivo temporal no está disponible. Vuelve a generar esta edición."}
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(header).to_bytes(4, "big") + header + payload


DOWNLOAD_CSS = r"""

.downloads {font: inherit; color: inherit;}
.summary {font-size: .9rem; margin: .5rem 0 1rem;}
.file {border: 1px solid #8885; border-radius: .6rem; padding: .8rem; margin: .7rem 0;}
.name {font-weight: 600; overflow-wrap: anywhere;}
.status {font-size: .85rem; opacity: .8; margin: .35rem 0 .6rem;}
.action {display: inline-flex; font: inherit; font-size: .95rem; padding: .4rem .7rem;
  border: 1px solid #8888; border-radius: .45rem; background: transparent; color: inherit;
  text-decoration: none; cursor: pointer; overflow-wrap: anywhere;}
.action:hover {border-color: #f36; color: #f36;}
.action:focus-visible {outline: 2px solid #f36; outline-offset: 2px;}
[hidden] {display: none !important;}
progress {width: 100%; height: .5rem; accent-color: #f36;}

"""

DOWNLOAD_JS = r"""

const CACHE_BYTES = 256 * 1024 * 1024;
const MAX_CHUNK = 4 * 1024 * 1024;
const WAIT_MS = 60000;

function decode(data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (bytes.byteLength < 4) throw new Error('Mensaje de descarga incompleto.');
  const length = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0, false);
  if (length < 2 || length > bytes.byteLength - 4) throw new Error('Cabecera de descarga incompleta.');
  const meta = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length)));
  if (meta.protocol !== 3 || typeof meta.batch !== 'string' || !Array.isArray(meta.files))
    throw new Error('Formato de descarga no compatible. Recarga la página.');
  return {meta, chunk: bytes.subarray(4 + length)};
}

function createState(root, batch) {
  root.querySelector('[data-pdfzip-error]')?.remove();
  const area = document.createElement('section');
  area.className = 'downloads';
  area.setAttribute('aria-label', 'Descargas');
  const summary = document.createElement('div');
  summary.className = 'summary';
  summary.setAttribute('role', 'status');
  summary.setAttribute('aria-live', 'polite');
  area.appendChild(summary);
  root.appendChild(area);
  const s = {root, batch, area, summary, records: new Map(), order: [], enabled: false,
    active: null, inflight: null, timer: null, counter: 0, disposed: false,
    emit: null, lastRequest: null};

  function sentBefore(id) {
    try { return sessionStorage.getItem('editions3:' + batch + ':' + id) === 'sent'; }
    catch (_) { return false; }
  }
  function remember(rec) {
    rec.sent = true;
    try { sessionStorage.setItem('editions3:' + batch + ':' + rec.id, 'sent'); } catch (_) {}
  }
  function release(rec) {
    if (rec.url) {
      const url = rec.url;
      // Una descarga recién solicitada necesita conservar su URL al iniciarse.
      if (Date.now() - rec.lastUsed < 2000) setTimeout(() => URL.revokeObjectURL(url), 2000);
      else URL.revokeObjectURL(url);
      rec.url = null;
    }
    rec.chunks = [];
  }
  function clearWait() { clearTimeout(s.timer); s.timer = null; }
  function emit(value) { s.lastRequest = value; s.emit(value); }
  function render(rec) {
    const ready = rec.phase === 'ready';
    rec.link.hidden = !ready;
    if (ready) rec.link.href = rec.url;
    else rec.link.removeAttribute('href');
    rec.retry.hidden = !['error', 'evicted'].includes(rec.phase);
    rec.retry.textContent = rec.phase === 'error' ? 'Reintentar descarga' : 'Descargar ' + rec.name;
    rec.bar.hidden = rec.phase !== 'loading';
    rec.bar.max = rec.size;
    rec.bar.value = rec.offset;
    let text;
    if (rec.phase === 'error') text = rec.error;
    else if (rec.phase === 'evicted') text = 'Pulsa Descargar para volver a preparar este archivo.';
    else if (ready) text = rec.sent ? 'Descarga solicitada al navegador. Puedes descargarlo de nuevo.' : 'Archivo listo para descargar.';
    else if (!s.enabled) text = 'Se preparará al terminar la generación de la edición.';
    else if (rec.phase === 'loading') text = 'Preparando descarga: ' + Math.floor(100 * rec.offset / rec.size) + '%';
    else text = 'En espera para preparar la descarga.';
    rec.status.textContent = (rec.size / 1048576).toFixed(2) + ' MiB · ' + text;
  }
  function renderSummary() {
    const records = [...s.records.values()];
    s.area.hidden = records.length === 0;
    const failed = records.filter(rec => rec.phase === 'error').length;
    const sent = records.filter(rec => rec.sent).length;
    s.summary.textContent = !s.enabled ? 'Los archivos se prepararán al terminar la generación de la edición.'
      : failed ? 'No se pudo preparar ' + failed + ' archivo(s). Puedes reintentar su descarga.'
      : sent < records.length ? 'Preparando las descargas automáticas: ' + sent + ' de ' + records.length
      : 'Descargas solicitadas al navegador: ' + sent + '. Los archivos siguen disponibles debajo.';
  }
  function fail(rec, message) {
    clearWait();
    release(rec);
    rec.phase = 'error'; rec.error = message; rec.offset = 0;
    s.inflight = null; s.active = null;
    render(rec); renderSummary();
  }
  function reserve(size) {
    let cached = [...s.records.values()].filter(rec => rec.phase === 'ready');
    let total = cached.reduce((sum, rec) => sum + rec.size, 0);
    cached.sort((a, b) => a.lastUsed - b.lastUsed);
    for (const rec of cached) {
      if (total + size <= CACHE_BYTES) break;
      total -= rec.size;
      release(rec); rec.phase = 'evicted'; rec.offset = 0;
      render(rec);
    }
  }
  s.pump = () => {
    if (s.disposed || !s.enabled || s.inflight) return;
    let rec = s.active ? s.records.get(s.active) : null;
    if (!rec) rec = s.order.map(id => s.records.get(id)).find(item => item.phase === 'waiting');
    if (!rec) {
      if (s.lastRequest !== null) emit(null); // Libera también el último bloque del mensaje de Streamlit.
      renderSummary();
      return;
    }
    if (rec.phase === 'waiting') {
      reserve(rec.size);
      rec.chunks = []; rec.offset = 0; rec.phase = 'loading';
    }
    s.active = rec.id;
    const request = {batch, id: rec.id, offset: rec.offset, token: Date.now() + '-' + (++s.counter)};
    s.inflight = request;
    render(rec); renderSummary();
    emit(request);
    s.timer = setTimeout(() => {
      if (s.disposed || s.inflight?.token !== request.token) return;
      fail(rec, 'La transferencia no respondió. Comprueba la conexión y pulsa Reintentar descarga.');
      s.pump();
    }, WAIT_MS);
  };
  s.update = meta => {
    const ids = new Set(meta.files.map(file => file.id));
    for (const [id, rec] of s.records) {
      if (ids.has(id)) continue;
      if (s.active === id) { clearWait(); s.inflight = null; s.active = null; }
      release(rec); rec.row.remove(); s.records.delete(id);
    }
    s.order = meta.files.map(file => file.id);
    s.enabled = !!meta.enabled;
    if (!s.enabled) { clearWait(); s.inflight = null; }
    for (const file of meta.files) {
      if (!s.records.has(file.id)) {
        const row = document.createElement('div'); row.className = 'file';
        const name = document.createElement('div'); name.className = 'name'; name.textContent = file.name;
        const status = document.createElement('div'); status.className = 'status';
        const bar = document.createElement('progress'); bar.setAttribute('aria-label', 'Preparación de ' + file.name);
        const link = document.createElement('a'); link.className = 'action';
        link.download = file.name; link.textContent = 'Descargar ' + file.name;
        const retry = document.createElement('button'); retry.className = 'action'; retry.type = 'button';
        row.append(name, status, bar, link, retry); area.appendChild(row);
        const rec = {id: file.id, name: file.name, mime: file.mime, size: file.size, row, status, bar, link, retry,
          phase: 'waiting', chunks: [], offset: 0, url: null, lastUsed: 0,
          sent: sentBefore(file.id), manual: false, error: ''};
        link.addEventListener('click', () => { rec.lastUsed = Date.now(); remember(rec); render(rec); renderSummary(); });
        retry.addEventListener('click', () => {
          rec.phase = 'waiting'; rec.error = ''; rec.manual = true;
          render(rec); s.pump();
        });
        s.records.set(file.id, rec);
      }
      render(s.records.get(file.id));
    }
    renderSummary();
  };
  s.consume = (meta, chunk) => {
    if (!s.enabled || !s.inflight) return;
    const answer = meta.error || meta.response;
    const request = s.inflight;
    if (!answer || answer.id !== request.id || answer.token !== request.token) return;
    const rec = s.records.get(request.id);
    if (!rec) return;
    if (meta.error) { fail(rec, meta.error.message); return; }
    if (answer.offset !== request.offset || answer.offset !== rec.offset) {
      fail(rec, 'Se recibió un bloque fuera de orden. Pulsa Reintentar descarga.'); return;
    }
    if (answer.total !== rec.size || answer.size !== chunk.byteLength || chunk.byteLength === 0
        || chunk.byteLength > MAX_CHUNK || rec.offset + chunk.byteLength > rec.size
        || chunk.byteLength !== Math.min(MAX_CHUNK, rec.size - rec.offset)
        || answer.last !== (rec.offset + chunk.byteLength === rec.size)) {
      fail(rec, 'Se recibió una descarga incompleta. Pulsa Reintentar descarga.'); return;
    }
    const magic = rec.mime === 'application/pdf' ? [37, 80, 68, 70, 45] : [80, 75, 3, 4];
    if (rec.offset === 0 && !magic.every((value, index) => chunk[index] === value)) {
      fail(rec, 'El resultado recibido no es un archivo válido. Vuelve a generar la edición.'); return;
    }
    clearWait(); s.inflight = null;
    rec.chunks.push(chunk.slice()); // Evita retener el buffer Protobuf completo.
    rec.offset += chunk.byteLength;
    if (rec.offset === rec.size) {
      try {
        const blob = new Blob(rec.chunks, {type: rec.mime});
        rec.chunks = [];
        rec.url = URL.createObjectURL(blob); rec.phase = 'ready'; rec.lastUsed = Date.now();
        s.active = null;
        render(rec);
        if (!rec.sent || rec.manual) { rec.manual = false; rec.link.click(); }
      } catch (_) { fail(rec, 'El navegador no pudo preparar el archivo. Pulsa Reintentar descarga.'); }
    }
    render(rec); renderSummary();
  };
  s.dispose = () => {
    if (s.disposed) return;
    s.disposed = true; clearWait();
    for (const rec of s.records.values()) release(rec);
    s.records.clear();
    s.area.remove();
    window.removeEventListener('pagehide', s.pagehide);
  };
  s.pagehide = event => { if (!event.persisted) s.dispose(); };
  window.addEventListener('pagehide', s.pagehide);
  return s;
}

export default function ({data, parentElement, setStateValue}) {
  let decoded;
  try { decoded = decode(data); }
  catch (error) {
    parentElement.__pdfzipDelivery?.dispose();
    parentElement.querySelector('[data-pdfzip-error]')?.remove();
    const message = document.createElement('p');
    message.dataset.pdfzipError = 'true'; message.setAttribute('role', 'alert');
    message.textContent = error.message;
    parentElement.appendChild(message);
    return;
  }
  const {meta, chunk} = decoded;
  const retired = parentElement.__pdfzipRetiredBatches ||= new Set();
  if (retired.has(meta.batch)) return;
  let state = parentElement.__pdfzipDelivery;
  if (!state || state.disposed || state.batch !== meta.batch) {
    if (state && state.batch !== meta.batch) retired.add(state.batch);
    state?.dispose();
    state = createState(parentElement, meta.batch);
    parentElement.__pdfzipDelivery = state;
  }
  state.emit = value => setStateValue('request', value);
  state.update(meta);
  state.consume(meta, chunk);
  state.pump();
  return () => {
    // Los datos cambian con cada bloque. Sólo limpiar al desmontar realmente,
    // sin revocar los enlaces listos durante una actualización de la pantalla.
    setTimeout(() => {
      if (parentElement.__pdfzipDelivery === state && !parentElement.isConnected) state.dispose();
    }, 0);
  };
}

"""

def emitir_descargas(st, parts, enabled=True):
    import streamlit.components.v2 as components

    component = components.component("editions_delivery_v1", html="", css=DOWNLOAD_CSS, js=DOWNLOAD_JS)
    value = st.session_state.get("edition_delivery")
    request = value.get("request") if value is not None else None
    message = construir_mensaje_descarga(parts, st.session_state.edition_delivery_batch, request, enabled)
    component(data=message, key="edition_delivery", on_request_change=lambda: None)


def _reset_session(st):
    old_temp = st.session_state.get("edition_temp")
    if old_temp is not None:
        old_temp.cleanup()
    st.session_state.edition_temp = tempfile.TemporaryDirectory(prefix="ediciones_")
    st.session_state.edition_delivery_batch = uuid.uuid4().hex
    st.session_state.edition_result = None
    st.session_state.edition_pending = None
    st.session_state.edition_schema = SESSION_SCHEMA


def _render_result(st, result):
    st.subheader("Resultado de la búsqueda")
    st.caption(f"{result['edition_name']} · {result['date']}")
    count = len(result.get("pages", []))
    if result["status"] == "error":
        st.error("No se pudieron generar archivos para esta búsqueda.")
    elif result["status"] == "partial":
        st.warning(f"Resultado parcial: se recuperaron {count} páginas. Revisa los avisos antes de usar los archivos.")
    else:
        st.success(f"Archivos generados: {count} páginas válidas encontradas.")
    if result.get("stop_reason") == "attempt_limit":
        st.warning("Se alcanzó el máximo de páginas a buscar. Puede haber más páginas; aumenta el máximo y vuelve a buscar si lo necesitas.")
    elif result.get("stop_reason") == "not_found":
        st.caption("La búsqueda terminó al encontrar varias páginas consecutivas no disponibles (HTTP 404/410). Esto no confirma el total oficial de la edición.")
    if result.get("missing_pages"):
        st.warning("Páginas ausentes antes de la última recuperada: " + ", ".join(map(str, result["missing_pages"])))
    for failure in result.get("failed_pages", []):
        st.error(f"Página {failure['page_number']}: {failure['error']}")
        st.link_button("Abrir dirección de la página", failure["url"])
    for error in result.get("errors", []):
        st.error(error)
    parts = result.get("files", [])
    pdf_parts = sum(part["mime"] == "application/pdf" for part in parts)
    zip_parts = sum(part["mime"] == "application/zip" for part in parts)
    if parts:
        st.caption(f"Disponibles: {pdf_parts} PDF y {zip_parts} ZIP. Los botones de descarga se conservan mientras mantengas esta búsqueda en la sesión.")
    if pdf_parts > 1 or zip_parts > 1:
        st.info("La edición se dividió en partes para reducir la memoria necesaria. Cada PDF y ZIP se abre por separado y contiene páginas completas.")
    with st.expander("Detalle de la búsqueda"):
        st.text("\n".join(result.get("logs", [])) or "Sin mensajes adicionales.")


def main():
    import streamlit as st

    st.set_page_config(page_title="Descargador de periódicos", layout="centered")
    st.title("Descargador de ediciones")
    st.caption("Busca las páginas de una edición y prepara su PDF y sus imágenes en ZIP.")
    if st.session_state.get("edition_schema") != SESSION_SCHEMA:
        _reset_session(st)

    @st.cache_resource
    def download_slot():
        return threading.BoundedSemaphore(1)

    busy = st.session_state.edition_pending is not None
    edition_name = st.selectbox("Periódico / edición", list(EDITIONS), disabled=busy, key="edition_choice")
    today = datetime.now(ZoneInfo("America/Mexico_City")).date()
    selected_date = st.date_input("Fecha", value=today, disabled=busy, key="edition_date")
    edition = EDITIONS[edition_name]
    col1, col2, col3 = st.columns(3)
    with col1:
        max_attempts = st.number_input("Máximo de páginas a buscar", min_value=10, max_value=300,
            value=int(edition["default_attempts"]), step=1, disabled=busy, key="attempts_" + sanitize_name(edition_name))
    with col2:
        max_missing = st.number_input("Páginas ausentes para detenerse", min_value=1, max_value=10,
            value=2, step=1, disabled=busy, key="missing_limit",
            help="Sólo cuenta páginas con HTTP 404/410. Los errores de conexión se reintentan y se muestran por separado.")
    with col3:
        delay_seconds = st.number_input("Pausa entre páginas", min_value=0.0, max_value=5.0,
            value=0.0, step=0.1, disabled=busy, key="request_delay")
    include_zip = st.checkbox("Generar también ZIP", value=True, disabled=busy, key="include_zip",
                              help="Se aplica a la próxima búsqueda. Los archivos ya generados se conservan.")
    actions = st.columns(2)
    if actions[0].button("Buscar y generar", type="primary", disabled=busy, width="stretch"):
        _reset_session(st)
        st.session_state.edition_pending = {"edition_name": edition_name,
            "selected_date": selected_date.isoformat(), "max_attempts": int(max_attempts),
            "max_consecutive_missing": int(max_missing), "delay_seconds": float(delay_seconds),
            "include_zip": bool(include_zip)}
        st.rerun()
    if actions[1].button("Cancelar búsqueda" if busy else "Vaciar resultados", width="stretch",
                          disabled=not busy and st.session_state.edition_result is None):
        _reset_session(st)
        st.rerun()

    # Este bloque permanece montado. Los reruns no destruyen los Blob listos.
    delivery_area = st.container(key="edition_download_area")
    result = st.session_state.edition_result
    if busy:
        with delivery_area:
            emitir_descargas(st, [], enabled=False)
        options = dict(st.session_state.edition_pending)
        status = st.empty()
        details = st.empty()
        bar = st.progress(0)
        slot = download_slot()
        acquired = False
        job_dir = Path(st.session_state.edition_temp.name) / uuid.uuid4().hex

        def heartbeat():
            # Atiende cancelación/reinicio sin ejecutar decodificación en el hilo UI.
            status.caption("Buscando y generando la edición…")

        def progress(event):
            status.caption(event["message"])
            details.text("\n".join(event.get("logs", [])))
            bar.progress(min(event["attempts"] / max(1, event["max_attempts"]), 1.0),
                         text=f"Páginas válidas: {event['valid']} · Intentos: {event['attempts']}/{event['max_attempts']}")

        try:
            while not slot.acquire(timeout=0.2):
                status.caption("Esperando turno de procesamiento. Tu búsqueda sigue en la cola.")
            acquired = True
            result = run_download_job(options, job_dir, progress=progress, heartbeat=heartbeat)
        except Exception as exc:
            LOGGER.exception("No se pudo procesar la búsqueda")
            result = {"status": "error", "edition_name": options["edition_name"],
                      "date": options["selected_date"], "pages": [], "files": [], "logs": [],
                      "errors": [str(exc) if isinstance(exc, ConversionError) else
                                 "Ocurrió un error al procesar la edición. Consulta el log del servidor."]}
        finally:
            if acquired:
                slot.release()
        st.session_state.edition_result = result
        st.session_state.edition_pending = None
        st.rerun()

    if result is not None:
        _render_result(st, result)
        with delivery_area:
            emitir_descargas(st, result.get("files", []))
        if result.get("files"):
            st.caption("La app solicita la descarga automáticamente. Si el navegador pide permiso para varias descargas, permite las de este sitio o utiliza los botones Descargar.")
    else:
        with delivery_area:
            emitir_descargas(st, [], enabled=False)
        st.info("Selecciona una edición y fecha; después pulsa Buscar y generar.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker_main(sys.argv[2]))
    main()
