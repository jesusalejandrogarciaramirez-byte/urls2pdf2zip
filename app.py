import io
import re
import time
import zipfile
from datetime import date
from typing import Dict, List, Tuple

import requests
import streamlit as st
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


st.set_page_config(page_title="Descargador de periódicos", layout="wide")


EDITIONS: Dict[str, Dict[str, object]] = {
    "Milenio Nacional": {
        "type": "milenio",
        "folder": "Nacional",
        "code": "NAC",
        "start_page": 0,
        "default_attempts": 80,
    },
    "Milenio León": {
        "type": "milenio",
        "folder": "Leon",
        "code": "LEO",
        "start_page": 0,
        "default_attempts": 80,
    },
    "Milenio Tamaulipas": {
        "type": "milenio",
        "folder": "Tamaulipas",
        "code": "TAM",
        "start_page": 0,
        "default_attempts": 80,
    },
    "Milenio Jalisco": {
        "type": "milenio",
        "folder": "Jalisco",
        "code": "JAL",
        "start_page": 0,
        "default_attempts": 80,
    },
    "Milenio Puebla": {
        "type": "milenio",
        "folder": "Puebla",
        "code": "PUE",
        "start_page": 0,
        "default_attempts": 80,
    },
    "Milenio Monterrey": {
        "type": "milenio",
        "folder": "Monterrey",
        "code": "MON",
        "start_page": 0,
        "default_attempts": 80,
    },
    "Milenio Edomex": {
        "type": "milenio",
        "folder": "Edomex",
        "code": "MEX",
        "start_page": 0,
        "default_attempts": 80,
    },
    "El Universal": {
        "type": "el_universal",
        "start_page": 1,
        "default_attempts": 100,
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
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
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
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }


def fetch_image(url: str, timeout: int = 30) -> Tuple[bytes, str]:
    headers = build_headers(url)

    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
        verify=True,
    )

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "image" not in content_type:
        raise RuntimeError(f"Contenido no es imagen: {content_type or 'desconocido'}")

    if not response.content:
        raise RuntimeError("Respuesta vacía")

    return response.content, content_type


def generate_pdf(images_data: List[Dict[str, object]]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)

    for item in images_data:
        image_bytes = item["bytes"]
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size

        pdf.setPageSize((width, height))
        pdf.drawImage(ImageReader(io.BytesIO(image_bytes)), 0, 0, width=width, height=height)
        pdf.showPage()

    pdf.save()
    buffer.seek(0)
    return buffer.read()


def generate_zip(images_data: List[Dict[str, object]]) -> bytes:
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for item in images_data:
            zf.writestr(item["filename"], item["bytes"])

    buffer.seek(0)
    return buffer.read()


def main() -> None:
    st.title("Descargador de ediciones")
    st.write(
        "Genera URLs por fecha y periódico, intenta descargar páginas en secuencia, "
        "tolera fallos consecutivos, crea PDF respetando el tamaño original de cada imagen "
        "y opcionalmente también un ZIP."
    )

    col1, col2 = st.columns(2)

    with col1:
        edition_name = st.selectbox("Periódico / edición", list(EDITIONS.keys()))
        selected_date = st.date_input("Fecha", value=date.today())

    edition = EDITIONS[edition_name]

    with col2:
        max_attempts = st.number_input(
            "Intentos máximos de páginas",
            min_value=10,
            max_value=300,
            value=int(edition["default_attempts"]),
            step=1,
        )
        max_consecutive_failures = st.number_input(
            "Fallos consecutivos para detenerse",
            min_value=1,
            max_value=10,
            value=2,
            step=1,
        )

    download_zip = st.checkbox("Descargar también imágenes en ZIP", value=True)
    delay_seconds = st.number_input(
        "Pausa entre intentos (segundos)",
        min_value=0.0,
        max_value=5.0,
        value=0.0,
        step=0.1,
    )

    if st.button("Buscar y generar archivos", type="primary"):
        start_page = int(edition["start_page"])
        images_data: List[Dict[str, object]] = []
        logs: List[str] = []
        consecutive_failures = 0
        attempts = 0
        page = start_page

        progress_bar = st.progress(0)
        log_box = st.empty()
        preview_box = st.container()

        base_name = f"{sanitize_name(edition_name)}_{selected_date.isoformat()}"

        while attempts < int(max_attempts):
            url = build_url(edition_name, selected_date, page)
            attempts += 1
            logs.append(f"Intentando página {page}: {url}")

            try:
                image_bytes, content_type = fetch_image(url)
                filename = f"{base_name}_pagina_{str(page).zfill(2)}.jpg"

                images_data.append(
                    {
                        "page_number": page,
                        "url": url,
                        "bytes": image_bytes,
                        "filename": filename,
                        "content_type": content_type,
                    }
                )

                logs.append(f"✓ Página {page} descargada correctamente.")
                consecutive_failures = 0

            except Exception as exc:
                consecutive_failures += 1
                logs.append(f"✗ Página {page} falló. Motivo: {exc}")
                logs.append(
                    f"Fallos consecutivos: {consecutive_failures}/{int(max_consecutive_failures)}"
                )

                if consecutive_failures >= int(max_consecutive_failures):
                    logs.append("Se alcanzó el límite de fallos consecutivos. Se detiene la búsqueda.")
                    progress_bar.progress(min(attempts / int(max_attempts), 1.0))
                    log_box.text("\n".join(logs))
                    break

            progress_bar.progress(min(attempts / int(max_attempts), 1.0))
            log_box.text("\n".join(logs))

            page += 1

            if delay_seconds > 0:
                time.sleep(float(delay_seconds))

        if not images_data:
            st.error("No se encontró ninguna imagen válida.")
            st.info(
                "Si El Universal falla, normalmente ya no es CORS porque aquí corre en Python. "
                "Suele ser bloqueo remoto, fecha inexistente o respuesta distinta a imagen."
            )
            return

        logs.append("")
        logs.append(f"Total de páginas encontradas: {len(images_data)}")
        logs.append(f"Primera página detectada: {images_data[0]['page_number']}")
        logs.append(f"Última página detectada: {images_data[-1]['page_number']}")
        log_box.text("\n".join(logs))

        st.success(f"Se encontraron {len(images_data)} páginas.")

        st.subheader("Vista previa")
        preview_cols = st.columns(4)

        for idx, item in enumerate(images_data[:20]):
            with preview_cols[idx % 4]:
                st.image(item["bytes"], caption=f"Página {item['page_number']}", use_container_width=True)

        st.subheader("Descargas")

        with st.spinner("Generando PDF..."):
            pdf_bytes = generate_pdf(images_data)

        st.download_button(
            label="Descargar PDF",
            data=pdf_bytes,
            file_name=f"{base_name}.pdf",
            mime="application/pdf",
        )

        if download_zip:
            with st.spinner("Generando ZIP..."):
                zip_bytes = generate_zip(images_data)

            st.download_button(
                label="Descargar ZIP",
                data=zip_bytes,
                file_name=f"{base_name}.zip",
                mime="application/zip",
            )

        st.subheader("URLs encontradas")
        for item in images_data:
            st.code(item["url"], language=None)


if __name__ == "__main__":
    main()