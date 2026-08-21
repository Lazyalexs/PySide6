# PyInstaller: сборка клиента в один .exe. Раздел 4 архитектуры.
# Сборка:  pyinstaller filepost.spec
# Результат: dist\FilePost.exe

block_cipher = None

a = Analysis(
    ["filepost_client/main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["filepost_client.ui.main_window", "filepost_client.ui.setup"],
    hookspath=[],
    runtime_hooks=[],
    # Qt тянет за собой много лишнего; без этого .exe раздувается втрое.
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "tkinter",
        "matplotlib",
        "numpy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="FilePost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # без консольного окна
    icon=None,
    version=None,
)
