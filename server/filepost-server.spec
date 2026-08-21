# PyInstaller: сборка службы в один .exe. Раздел 4 архитектуры.
# Сборка:  pyinstaller filepost-server.spec
# Результат: dist\filepost-server.exe
#
# Собранный exe избавляет от установки Python на сервер: там остаётся один файл
# и config.toml рядом. Это и имелось в виду под «embeddable-сборкой» в разделе 4.

block_cipher = None

a = Analysis(
    ["run_server.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    # Uvicorn и FastAPI грузят реализации по строковым именам, поэтому статический
    # анализ их не находит. Без этого списка собранный exe падает при старте
    # с «Unknown loop/protocol» — классическая ловушка упаковки uvicorn.
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "anyio._backends._asyncio",
        "argon2",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Служба консольная: ничего графического ей не нужно.
        "PySide6",
        "PyQt5",
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
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
    name="filepost-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,   # служба запускается NSSM, вывод уходит в журнал
    icon=None,
    version=None,
)
