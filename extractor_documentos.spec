# -*- mode: python ; coding: utf-8 -*-
# Construir con: pyinstaller extractor_documentos.spec --clean --noconfirm
# (o simplemente corriendo construir_exe.bat, que hace todo el proceso)

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),   # HTML de la interfaz
        ('static', 'static'),         # logo, favicon
        ('config', 'config'),         # mapeo_ips.xlsx (referencia NIT -> nombre IPS)
    ],
    hiddenimports=[
        # PyMuPDF, numpy y openpyxl a veces necesitan un empujón para que
        # PyInstaller encuentre todos sus sub-módulos internos
        'fitz',
        'numpy',
        'openpyxl',
        'openpyxl.cell._writer',
        'PIL._tkinter_finder',
    ],
    hookspath=[],
    excludes=[],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ExtractorDocumentosDigitales',
    console=False,                              # sin ventana negra de consola
    onefile=True,                               # todo en un solo .exe
    icon='resources/icons/app.ico',              # mismo logo que ya usa la app (favicon)
)
