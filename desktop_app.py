# -*- coding: utf-8 -*-
"""
desktop_app.py
==============
Punto de entrada para la versión de escritorio (.exe) del Extractor de
Documentos Digitales. No es una versión distinta del bot -- es el MISMO
app.py de siempre, solo que en vez de abrirlo en una pestaña de un
navegador cualquiera, se abre en su propia ventana nativa (sin barra de
direcciones, sin pestañas) usando pywebview. Se ve y se siente como una
aplicación de escritorio normal, aunque por dentro sigue siendo el mismo
Flask de siempre corriendo en segundo plano.

Importante: Flask se levanta SOLO en 127.0.0.1 (nunca en la red) -- esta
app maneja usuario y clave reales contra el sistema Esculapio del
cliente, así que no debe quedar expuesta a nadie más en la misma red.
"""
import sys
import threading
import time
from pathlib import Path

# Para que los imports (app, auth, clasificador, etc.) se encuentren
# igual en modo script que empaquetado con PyInstaller.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import app  # la app Flask de siempre, sin ningún cambio de lógica

import webview

PUERTO = 5057
URL_LOCAL = f"http://127.0.0.1:{PUERTO}"


def _iniciar_flask():
    app.run(host="127.0.0.1", port=PUERTO, debug=False, use_reloader=False, threaded=True)


def _servidor_listo(intentos=40, espera=0.25):
    """Espera a que Flask ya esté aceptando conexiones antes de abrir la
    ventana -- si se abre la ventana demasiado pronto, se ve una página
    de error de "no se puede conectar" por una fracción de segundo."""
    import urllib.request
    for _ in range(intentos):
        try:
            urllib.request.urlopen(URL_LOCAL, timeout=1)
            return True
        except Exception:
            time.sleep(espera)
    return False


if __name__ == "__main__":
    hilo = threading.Thread(target=_iniciar_flask, daemon=True)
    hilo.start()
    _servidor_listo()

    webview.create_window(
        "Extractor de Documentos Digitales",
        URL_LOCAL,
        width=1360,
        height=880,
        min_size=(1100, 700),
    )
    webview.start()
