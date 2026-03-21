📰 Descargador de Ediciones (Milenio / El Universal)

Aplicación en Python + Streamlit que permite convertir urls de imagenes a pdf y comprimirlas en un zip

✨ Características
📅 Selector de fecha
🗞️ Selector de periódico:
Milenio (todas sus ediciones)
El Universal
🔎 Búsqueda automática de páginas
🧠 Detección inteligente de fin:
Se detiene después de N fallos consecutivos
📄 Generación de PDF con tamaño real de cada página
📦 Descarga opcional en ZIP
🖼️ Vista previa de páginas encontradas
⚙️ Configuración de:
intentos máximos
fallos consecutivos
delay entre requests
🧠 Cómo funciona

La app construye dinámicamente las URLs de las páginas según:

Milenio
https://api-epaper.milenio.com/file/{REGION}/{CODIGO}/{YYYYMMDD}/images/{N}.jpg
El Universal
https://edicionimpresa.eluniversal.com.mx/archive/eu{DDMMYY}/files/pages/tablet/{N}.jpg

Luego:

Intenta descargar la primera página
Incrementa el número
Si falla:
cuenta como fallo
Si hay N fallos consecutivos → se detiene
Con las imágenes válidas:
genera PDF
genera ZIP (opcional)
🚀 Instalación
1. Clonar repositorio
git clone https://github.com/tu-usuario/descargador-periodicos.git
cd descargador-periodicos
2. Crear entorno (opcional pero recomendado)
python -m venv venv
venv\Scripts\activate   # Windows
source venv/bin/activate # Linux / Mac
3. Instalar dependencias
pip install -r requirements.txt
▶️ Uso
streamlit run streamlit_periodicos.py

Luego abre en el navegador:

http://localhost:8501
⚙️ Configuración recomendada
Parámetro	Valor recomendado
Intentos máximos	80–120
Fallos consecutivos	2
Delay	0 – 0.5
⚠️ Notas importantes
🔴 Sobre CORS

Esta app NO usa navegador para descargar, por lo tanto:

✔️ Funciona con El Universal
✔️ No necesita proxy
✔️ No depende de CORS

🟡 Variación de páginas
Milenio: ~32–40 páginas (empieza en 0)
El Universal: ~40–60 páginas (empieza en 1)
🔒 Posibles bloqueos

Algunos errores pueden deberse a:

Fecha inexistente
Cambios en la estructura del sitio
Bloqueo por parte del servidor remoto
🛠️ Dependencias
streamlit
requests
Pillow
reportlab
📁 Estructura del proyecto
.
├── streamlit_periodicos.py
├── requirements.txt
└── README.md
💡 Ideas futuras
Guardado automático en carpeta local
Soporte para más periódicos
Paralelización de descargas
Reintentos inteligentes por página
Exportación por rangos de páginas
⚖️ Aviso legal

Este proyecto es únicamente para fines educativos y personales.

Las imágenes descargadas pertenecen a sus respectivos propietarios.
Respeta los términos de uso de cada medio.

👨‍💻 Autor

ChatGPT 🚀

Si quieres, puedo hacerte:

versión que guarde automáticamente en disco
versión con multithreading (más rápida)
versión ejecutable .exe
versión con interfaz más pro (tipo app desktop)
