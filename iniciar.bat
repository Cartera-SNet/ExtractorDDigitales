@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Extractor de Documentos Digitales - v1 (pruebas locales)
echo ============================================================
echo.
echo Version de Python detectada:
python --version 2>nul
echo (Recomendado: Python 3.11, 3.12 o 3.13. Si tienes una version muy
echo  nueva recien salida, algunas librerias pueden no tener instalador
echo  precompilado todavia.)
echo.

REM ── Crear entorno virtual si no existe ──────────────────────
if not exist "venv\Scripts\activate.bat" (
    echo [1/3] Creando entorno virtual, primera vez puede tardar un poco...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo ERROR: no se encontro Python. Instala Python 3.11+ desde
        echo https://www.python.org/downloads/  y marca la opcion
        echo "Add python.exe to PATH" durante la instalacion.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Entorno virtual ya existe, se reutiliza.
)

call venv\Scripts\activate.bat

echo [2/3] Verificando/instalando dependencias...
python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ============================================================
    echo ERROR: fallo la instalacion de dependencias ^(ver detalle arriba^).
    echo No se puede continuar hasta resolver esto.
    echo.
    echo Causa mas comun: version de Python muy nueva o muy vieja para
    echo la que todavia no hay instalador precompilado de alguna libreria.
    echo Recomendado: usar Python 3.11, 3.12 o 3.13 ^(evitar la ultima
    echo version recien salida^). Descarga: https://www.python.org/downloads/
    echo.
    echo Si quieres reintentar con otra version de Python:
    echo   1^) Borra la carpeta "venv" de esta misma carpeta.
    echo   2^) Instala Python 3.12 marcando "Add python.exe to PATH".
    echo   3^) Vuelve a correr iniciar.bat.
    echo ============================================================
    pause
    exit /b 1
)

echo.
echo [3/3] Verificando motor de OCR (necesario para leer cedulas/tarjetas
echo        escaneadas como imagen)...
set "TESSERACT_OK="
set "TESSERACT_OFICIAL="
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    set "TESSERACT_OK=1"
    set "TESSERACT_OFICIAL=1"
)
if not defined TESSERACT_OK if exist "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe" (
    set "TESSERACT_OK=1"
    set "TESSERACT_OFICIAL=1"
)
if not defined TESSERACT_OK (
    where tesseract >nul 2>&1
    if not errorlevel 1 set "TESSERACT_OK=1"
)

if not defined TESSERACT_OK (
    echo.
    echo AVISO: no se encontro Tesseract-OCR instalado en el sistema.
    echo Se va a instalar EasyOCR como respaldo automatico ^(no necesita
    echo instalador aparte de Windows, pero es mas lento que Tesseract y
    echo la descarga inicial pesa varios cientos de MB^).
    echo.
    echo Si en algun momento quieres el motor rapido, se instala con:
    echo   winget install --id UB-Mannheim.TesseractOCR -e
    echo o descargandolo de https://github.com/UB-Mannheim/tesseract/wiki
    echo ^(esto NO se instala solo; hazlo tu cuando quieras^).
    echo.
    pip install easyocr
) else (
    if defined TESSERACT_OFICIAL (
        echo Tesseract-OCR oficial encontrado en su carpeta de instalacion,
        echo se usara como motor principal ^(prioridad sobre cualquier otra
        echo copia de Tesseract que traiga otro programa instalado, como
        echo PDF24 o similares^).
    ) else (
        echo Tesseract-OCR encontrado en el PATH del sistema ^(no es la
        echo instalacion oficial dedicada; si la lectura de documentos se
        echo ve imprecisa, instala la version oficial desde
        echo https://github.com/UB-Mannheim/tesseract/wiki para mejor
        echo calidad^), se usara como motor principal.
    )
)

echo.
echo Iniciando el servidor...
echo Se va a abrir el navegador solo en unos segundos.
echo Si no se abre, entra a mano a: http://localhost:5057
echo (Deja esta ventana abierta mientras uses el bot. Para cerrar, Ctrl+C)
echo.
python app.py

pause
