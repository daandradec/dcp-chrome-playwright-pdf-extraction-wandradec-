# Extracción de PDF mediante Playwright y CDP (Chrome DevTools Protocol)

Este repositorio contiene un conjunto de herramientas diseñadas para automatizar la extracción de libros electrónicos, manuales y documentos desde visores web que utilizan técnicas de protección, carga diferida o formatos de imagen modernos (WEBP).

## 🚀 Funcionalidades Principales

- **Captura de Tráfico Real:** Intercepta respuestas de red mediante CDP y WebSockets para extraer imágenes en alta resolución.
- **Reconstrucción desde Tráfico:** Genera PDFs a partir de archivos JSON de tráfico capturado (decodificación Base64).
- **Descarga Directa de Pestañas Activas:** Se conecta a una instancia de Chrome existente para descargar PDFs de páginas protegidas por login.
- **Procesamiento de Imágenes:** Conversión masiva de WEBP a JPG y consolidación en documentos PDF únicos.

## 🛠️ Requisitos

- **Python 3.8+**
  - `pip install playwright requests pillow websocket-client`
- **Node.js** (para scripts JS)
  - `npm install playwright pdf-lib`
- **Google Chrome** (instalado localmente)

## 📖 Uso de las Herramientas

### 1. Iniciar Chrome en Modo Depuración
Para que los scripts puedan interactuar con una sesión activa (manteniendo tus cookies y login), inicia Chrome con el siguiente comando:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins="*" \
  --user-data-dir="$HOME/.chrome-cdp-session"
```

### 2. Extracción Automática de Imágenes
Si el sitio usa un patrón de URLs para las páginas:
```bash
python build_best_quality_pdf.py \
  --base-url "https://ejemplo.com/visor/pagina_" \
  --start 1 --end 100 \
  --output "documento_final.pdf"
```

### 3. Captura Manual de Tráfico
Para sitios complejos que requieren navegación manual, usa el capturador CDP:
```bash
python capture_manual_traffic_cdp.py --port 9222 --output traffic.json
```
*Navega por las páginas del visor y el script guardará automáticamente todas las respuestas de imagen encontradas.*

### 4. Reconstrucción desde JSON
Si ya tienes un volcado de tráfico en JSON:
```bash
python build_pdf_from_traffic_json.py --input traffic.json --output resultado.pdf
```

### 5. Conversión de WEBP a PDF
Si ya descargaste las imágenes pero están en formato WEBP:
```bash
python webp_folder_to_pdf.py --input-dir ./imagenes_webp --output libro.pdf
```

## 📂 Estructura del Proyecto

- `capture_manual_traffic_cdp.py`: Interceptor de tráfico basado en Playwright/CDP.
- `build_best_quality_pdf.py`: Script principal para descarga secuencial y ensamble.
- `download_pdf_from_active_tab.py`: Utilidad para "imprimir a PDF" desde una pestaña activa.
- `webp_to_jpg_3engines_pdf.js`: Procesador avanzado de imágenes usando Node.js para mayor rendimiento.

## ⚠️ Notas Legales
Este proyecto es exclusivamente para fines educativos y de respaldo personal. Asegúrate de cumplir con los términos de servicio de los sitios web que visites.
