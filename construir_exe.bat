@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Extractor de Documentos Digitales - construccion del .exe
echo ============================================================
echo.

if not exist ".venv-build\Scripts\python.exe" (
    echo [1/5] Creando entorno virtual de build...
    python -m venv .venv-build
    if errorlevel 1 (
        echo.
        echo ERROR: no se encontro Python. Instala Python 3.11+ desde
        echo https://www.python.org/downloads/ y marca la opcion
        echo "Add python.exe to PATH" durante la instalacion.
        pause
        exit /b 1
    )
) else (
    echo [1/5] Entorno virtual ya existe, se reutiliza.
)

call ".venv-build\Scripts\activate.bat"

echo [2/5] Actualizando pip...
python -m pip install --upgrade pip >nul 2>&1

echo [3/5] Instalando dependencias de la app (Flask, PyMuPDF, OCR, Excel)...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: fallo la instalacion de dependencias de la app. Revisa el
    echo detalle de arriba antes de continuar.
    pause
    exit /b 1
)

echo [4/5] Instalando dependencias de empaquetado (PyInstaller + pywebview)...
pip install -r requirements-desktop.txt
if errorlevel 1 (
    echo.
    echo ERROR: fallo la instalacion de PyInstaller/pywebview.
    pause
    exit /b 1
)

echo [5/5] Construyendo ExtractorDocumentosDigitales.exe...
pyinstaller extractor_documentos.spec --clean --noconfirm

echo.
echo ============================================================
if exist "dist\ExtractorDocumentosDigitales.exe" (
    echo Listo. El ejecutable esta en:
    echo   dist\ExtractorDocumentosDigitales.exe
    echo.
    echo IMPORTANTE - Tesseract-OCR:
    echo   El .exe NO trae Tesseract-OCR incluido (es un programa aparte,
    echo   no una libreria de Python). En CUALQUIER computador donde se
    echo   vaya a usar el .exe, hay que instalar Tesseract-OCR una sola
    echo   vez, con cualquiera de estas opciones:
    echo     winget install --id UB-Mannheim.TesseractOCR -e
    echo   o descargandolo de:
    echo     https://github.com/UB-Mannheim/tesseract/wiki
    echo   Sin esto, el bot no podra leer cedulas/tarjetas escaneadas
    echo   como imagen ^(los PDF con texto digital si funcionan igual^).
) else (
    echo ALGO SALIO MAL: no se genero el .exe. Revisa los mensajes de
    echo PyInstaller mas arriba para ver el detalle del error.
)
echo ============================================================
pause
endlocal
