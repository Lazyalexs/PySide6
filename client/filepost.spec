# PyInstaller: сборка клиента в один .exe. Раздел 4 архитектуры.
# Сборка:  pyinstaller filepost.spec
# Результат: dist\FilePost.exe

block_cipher = None

a = Analysis(
    # Не filepost_client/main.py: PyInstaller запускает скрипт как __main__ без
    # пакетного контекста, и относительные импорты внутри него падают.
    ["run_client.py"],
    pathex=["."],
    binaries=[],
    # Звук уведомлений распаковывается в sys._MEIPASS/resources — см. sound.resource_path.
    datas=[("filepost_client/resources/notify.wav", "resources")],
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
