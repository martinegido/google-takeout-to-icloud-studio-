#!/bin/bash
cd "$(dirname "$0")"
PORT=8765

# Si ya está corriendo en el puerto 8765, solo abrimos el navegador
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "El panel ya está activo en http://localhost:$PORT"
    open "http://localhost:$PORT"
    exit 0
fi

echo "Iniciando Sincronizador Google Fotos -> iCloud en http://localhost:$PORT ..."
(sleep 1.2 && open "http://localhost:$PORT") &
python3 app.py
