# 📸➔☁️ Google Takeout & Folders to iCloud Photos Studio (macOS)

Herramienta visual web para **macOS** que compara tus archivos de **Google Fotos (Google Takeout `.zip`)** y **carpetas de fotos/vídeos sin comprimir** contra tu biblioteca actual de **iCloud Photos**, detectando exactamente qué años, meses y archivos te faltan y subiéndolos por tandas con su fecha original restaurada.

---

## ✨ ¿Qué problema resuelve?

Pasar años de recuerdos desde Google Fotos o discos duros/NAS a iCloud Photos suele ser una pesadilla por cinco motivos que esta herramienta resuelve de forma automática:

1. **No necesitas descomprimir cientos de GB de archivos `.zip`:** Lee la tabla interna de los `.zip` de Google Takeout directamente desde un **NAS** o disco externo y solo extrae las fotos/vídeos que realmente faltan en tu iCloud.
2. **Cero Borrados (100% Seguro):** Trabaja en modo estrictamente **Solo Lectura (`Read-Only`)** sobre tus archivos originales y sobre tu biblioteca de Fotos de macOS.
3. **Compatible con "Optimizar almacenamiento del Mac":** No necesita que las 40.000+ fotos de iCloud estén descargadas en el Mac; lee el catálogo local de nombres, fechas y metadatos.
4. **Restauración exacta de fechas (`JSON` + `EXIF` + `QuickTime`):**
   - Cruza cada foto/vídeo con su `.json` de Google Takeout (`photoTakenTime`).
   - Si Google dividió el Takeout en varios volúmenes `.zip` y el `.json` quedó en otro archivo, extrae automáticamente la fecha real de captura incrustada en el `EXIF` / `QuickTime` mediante **ExifTool**.
5. **Adiós al límite de 1.000 fotos y al bloqueo de vídeos de `icloud.com`:**
   - La web `icloud.com/photos` rechaza vídeos grandes, rechaza archivos `.png`/`.heic` y limita las subidas.
   - Esta herramienta importa por **tandas pausables** directamente a través de la app nativa **Fotos (`Photos.app`)** de macOS y **convierte automáticamente al vuelo con `FFmpeg` los vídeos con códec `VP9` (`vp09`) de Google a `H.264` (`avc1`)** para que Apple Photos nunca dé error de *"Formato desconocido"*.

---

## 🖥️ Características Principales del Panel Web

- **Apartado 1 — Lector iCloud Photos (Mac):** Escaneo vía Automatización oficial de `Photos.app` o modo instantáneo (2 segundos) mediante snapshot de solo lectura de `Photos.sqlite`.
- **Apartado 2A — Archivos `.ZIP` de Google Takeout:** Añade `.zip` individuales o carpetas enteras de `.zip` en tu NAS sin descomprimirlos.
- **Apartado 2B — Carpetas Sueltas Sin Comprimir:** Añade cualquier carpeta de fotos y vídeos descomprimidos; lee su fecha real de cámara (`EXIF`) para no confundirla con la fecha de copia del archivo.
- **Mapa Visual por Años y Meses:** Muestra mes a mes cuántas fotos y vídeos tienes en iCloud, cuántas en tus orígenes y cuántas faltan (`🔴 Mes vacío en iCloud` vs `🟡 Parcial`).
- **Deduplicación Inteligente:**
  - Detecta coincidencias aunque cambie el formato (`.HEIC` en iCloud vs `.JPG` en Google Fotos).
  - Detecta clips cortos de **Live Photos** cuya foto principal ya está en iCloud.
  - Elimina duplicados internos de álbumes dentro del propio Google Takeout.
- **Explorador Visual con Miniaturas Reales (`Thumbnails`):** Previsualiza `.JPG`, `.PNG`, `.HEIC`, `.MOV` y `.MP4` (incluso cuando siguen dentro de un archivo `.zip`).
- **Gestor de Tandas Pausable + Galería Final de Importados:** Elige tandas de 25, 100, 250 o 500 archivos (o por Mes/Año), pausa o reanuda cuando quieras, y al terminar revisa la galería visual con todas las fotos y vídeos recién importados.

---

## 🛠️ Requisitos en macOS

1. **macOS** con la app **Fotos** sincronizada con iCloud.
2. **Python 3.10+** y `Pillow`:
   ```bash
   pip3 install -r requirements.txt
   ```
3. **ExifTool** y **FFmpeg** (para restaurar fechas EXIF/QuickTime y convertir vídeos `VP9` de Google a `H.264` de Apple):
   ```bash
   brew install exiftool ffmpeg
   ```

---

## 🚀 Cómo empezar

### Opción A: Doble clic desde Finder
Haz doble clic en el archivo **`Iniciar_Panel.command`**. Se iniciará el servidor local y se abrirá automáticamente tu navegador en `http://localhost:8765`.

### Opción B: Desde la Terminal
```bash
python3 app.py
```
Y abre en tu navegador: **[http://localhost:8765](http://localhost:8765)**

---

## 📂 Estructura del Proyecto

```text
unirgooglefotosyfotosicloud/
├── app.py                     # Servidor web local y API de orquestación en segundo plano
├── Iniciar_Panel.command      # Lanzador de doble clic para macOS
├── requirements.txt           # Dependencias de Python
├── core/
│   ├── database.py            # Base de datos SQLite persistente (data/sync_state.db)
│   ├── scanner_icloud.py      # Lector 100% Read-Only del catálogo de iCloud Photos en Mac
│   ├── scanner_takeout.py     # Escáner de ZIPs de Takeout (sin descomprimir) y Carpetas sueltas
│   ├── matcher.py             # Motor de cruce, deduplicación y matriz por Años/Meses
│   ├── batch_processor.py     # Motor de tandas pausables, ExifTool, FFmpeg (VP9->H.264) y Photos.app
│   ├── thumbnails.py          # Generador de miniaturas para HEIC, JPG, PNG, MOV y MP4
│   └── utils.py               # Normalización de nombres y utilidades de fechas
└── static/
    ├── index.html             # Interfaz gráfica paso a paso
    ├── styles.css             # Diseño visual oscuro de alto contraste
    └── app.js                 # Lógica interactiva en tiempo real
```

---

## 🔒 Garantía de Seguridad

- El código **jamás ejecuta operaciones de borrado** sobre tus carpetas de origen, tu NAS ni tu biblioteca de Fotos.
- Todos los archivos procesados en una tanda se **copian/extraen** primero a `Lotes_Listos_Para_iCloud/` antes de restaurar sus metadatos e importarse en un álbum identificado (`Google Takeout (YYYY-MM)`) dentro de la app Fotos.
