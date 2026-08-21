@echo off
rem Сборка FilePost на Windows. Запускать на той машине, где будут собираться
rem дистрибутивы: PyInstaller не умеет кросс-компиляцию, .exe для Windows
rem собирается только на Windows.
rem
rem Требуется: Python 3.11+ и (для установщика клиента) Inno Setup 6.

setlocal
set ROOT=%~dp0
set FAILED=0

echo ============================================================
echo   FilePost — сборка дистрибутивов
echo ============================================================
echo.

rem ---------------------------------------------------------------- сервер
echo [1/4] Сервер: окружение
cd /d "%ROOT%server"
if not exist .venv (
    python -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -r requirements.txt || goto :error
.venv\Scripts\pip install --quiet pyinstaller || goto :error

echo [2/4] Сервер: тесты и сборка
.venv\Scripts\python -m pytest tests -q || goto :error
.venv\Scripts\pyinstaller --clean --noconfirm filepost-server.spec || goto :error

rem ---------------------------------------------------------------- клиент
echo [3/4] Клиент: окружение и тесты
cd /d "%ROOT%client"
if not exist .venv (
    python -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -r requirements.txt || goto :error
.venv\Scripts\python -m pytest tests -q || goto :error

echo [4/4] Клиент: сборка exe и установщика
.venv\Scripts\pyinstaller --clean --noconfirm filepost.spec || goto :error

rem Inno Setup ставится не всегда; без него остаётся голый .exe.
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    %ISCC% installer.iss || goto :error
    echo     Установщик: client\Output\FilePost-Setup-1.0.3.exe
) else (
    echo     ВНИМАНИЕ: Inno Setup 6 не найден, установщик не собран.
    echo     Скачайте https://jrsoftware.org/isdl.php — без него остаётся
    echo     только dist\FilePost.exe, который придётся копировать руками.
)

echo.
echo ============================================================
echo   Готово
echo ============================================================
echo   Сервер:  server\dist\filepost-server.exe
echo   Клиент:  client\dist\FilePost.exe
echo.
echo   На сервер копируются: filepost-server.exe, config.toml,
echo   install-service.cmd и nssm.exe — Python там не нужен.
goto :end

:error
echo.
echo *** СБОРКА ПРЕРВАНА, код %ERRORLEVEL% ***
echo Тесты и сборка останавливаются на первой ошибке намеренно:
echo выкладывать дистрибутив с падающими тестами нельзя.
set FAILED=1

:end
cd /d "%ROOT%"
endlocal & exit /b %FAILED%
