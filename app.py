import base64
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


st.set_page_config(page_title="Descargador de periódicos", layout="centered")


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
        pdf.drawImage(
            ImageReader(io.BytesIO(image_bytes)),
            0,
            0,
            width=width,
            height=height,
        )
        pdf.showPage()

    pdf.save()
    buffer.seek(0)
    return buffer.read()


def generate_zip(images_data: List[Dict[str, object]]) -> bytes:
    buffer = io.BytesIO()

    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as zf:
        for item in images_data:
            zf.writestr(item["filename"], item["bytes"])

    buffer.seek(0)
    return buffer.read()


def auto_download_file(file_bytes: bytes, filename: str, mime_type: str, key: str) -> None:
    b64 = base64.b64encode(file_bytes).decode()
    href = f"data:{mime_type};base64,{b64}"

    st.components.v1.html(
        f"""
        <script>
          (function() {{
            const already = sessionStorage.getItem("auto_download_{key}");
            if (!already) {{
              const a = document.createElement("a");
              a.href = "{href}";
              a.download = "{filename}";
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              sessionStorage.setItem("auto_download_{key}", "1");
            }}
          }})();
        </script>
        """,
        height=0,
    )


def main() -> None:
    st.title("Descargador de ediciones")

    edition_name = st.selectbox("Periódico / edición", list(EDITIONS.keys()))
    selected_date = st.date_input("Fecha", value=date.today())

    edition = EDITIONS[edition_name]

    col1, col2, col3 = st.columns(3)

    with col1:
        max_attempts = st.number_input(
            "Intentos máximos",
            min_value=10,
            max_value=300,
            value=int(edition["default_attempts"]),
            step=1,
        )

    with col2:
        max_consecutive_failures = st.number_input(
            "Fallos consecutivos para detenerse",
            min_value=1,
            max_value=10,
            value=2,
            step=1,
        )

    with col3:
        delay_seconds = st.number_input(
            "Pausa entre intentos",
            min_value=0.0,
            max_value=5.0,
            value=0.0,
            step=0.1,
        )

    download_zip = st.checkbox("Generar también ZIP", value=True)

    if st.button("Buscar y generar", type="primary"):
        start_page = int(edition["start_page"])
        images_data: List[Dict[str, object]] = []
        logs: List[str] = []
        consecutive_failures = 0
        attempts = 0
        page = start_page

        progress_bar = st.progress(0)
        status_box = st.empty()
        info_box = st.empty()

        base_name = f"{sanitize_name(edition_name)}_{selected_date.isoformat()}"

        while attempts < int(max_attempts):
            url = build_url(edition_name, selected_date, page)
            attempts += 1

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

                consecutive_failures = 0
                logs.append(f"✓ Página {page} descargada")
                status_box.text("\n".join(logs[-12:]))

            except Exception as exc:
                consecutive_failures += 1
                logs.append(
                    f"✗ Página {page} falló ({consecutive_failures}/{int(max_consecutive_failures)}): {exc}"
                )
                status_box.text("\n".join(logs[-12:]))

                if consecutive_failures >= int(max_consecutive_failures):
                    logs.append("Se alcanzó el límite de fallos consecutivos. Fin del proceso.")
                    status_box.text("\n".join(logs[-12:]))
                    progress_bar.progress(min(attempts / int(max_attempts), 1.0))
                    break

            progress_bar.progress(min(attempts / int(max_attempts), 1.0))
            info_box.info(
                f"Páginas válidas: {len(images_data)} | "
                f"Intento actual: {attempts}/{int(max_attempts)} | "
                f"Página actual: {page}"
            )

            page += 1

            if delay_seconds > 0:
                time.sleep(float(delay_seconds))

        if not images_data:
            st.error("No se encontró ninguna imagen válida.")
            return

        st.success(f"Proceso terminado. Páginas descargadas: {len(images_data)}")

        with st.spinner("Generando PDF..."):
            pdf_bytes = generate_pdf(images_data)

        auto_download_file(
            file_bytes=pdf_bytes,
            filename=f"{base_name}.pdf",
            mime_type="application/pdf",
            key=f"{base_name}_pdf",
        )

        st.download_button(
            label="Descargar PDF manualmente",
            data=pdf_bytes,
            file_name=f"{base_name}.pdf",
            mime="application/pdf",
        )

        if download_zip:
            with st.spinner("Generando ZIP..."):
                zip_bytes = generate_zip(images_data)

            auto_download_file(
                file_bytes=zip_bytes,
                filename=f"{base_name}.zip",
                mime_type="application/zip",
                key=f"{base_name}_zip",
            )

            st.download_button(
                label="Descargar ZIP manualmente",
                data=zip_bytes,
                file_name=f"{base_name}.zip",
                mime="application/zip",
            )


if __name__ == "__main__":
    main()