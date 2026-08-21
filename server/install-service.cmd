@echo off
rem Установка FilePost как службы Windows через NSSM. Раздел 4 архитектуры.
rem Запускать от имени администратора.

setlocal
set ROOT=%~dp0
set NSSM=%ROOT%nssm.exe
set PYTHON=%ROOT%.venv\Scripts\python.exe
set SERVICE=FilePost

if not exist "%NSSM%" (
    echo Не найден nssm.exe рядом со скриптом. Скачайте NSSM и положите его сюда.
    exit /b 1
)
if not exist "%PYTHON%" (
    echo Не найден %PYTHON%. Сначала создайте venv и поставьте зависимости.
    exit /b 1
)

echo === Установка службы %SERVICE% ===
"%NSSM%" install %SERVICE% "%PYTHON%" "-m" "filepost.cli" "--config" "%ROOT%config.toml" "serve"
"%NSSM%" set %SERVICE% AppDirectory "%ROOT%"
"%NSSM%" set %SERVICE% DisplayName "FilePost — обмен файлами"
"%NSSM%" set %SERVICE% Description "Служба обмена файлами в закрытой сети"
"%NSSM%" set %SERVICE% Start SERVICE_AUTO_START

rem Перезапуск при падении, но не бесконечным циклом.
"%NSSM%" set %SERVICE% AppExit Default Restart
"%NSSM%" set %SERVICE% AppRestartDelay 5000
"%NSSM%" set %SERVICE% AppThrottle 10000

rem Ротация stdout/stderr силами NSSM: журнал приложения пишется отдельно.
"%NSSM%" set %SERVICE% AppStdout "%ROOT%logs\service.log"
"%NSSM%" set %SERVICE% AppStderr "%ROOT%logs\service.log"
"%NSSM%" set %SERVICE% AppRotateFiles 1
"%NSSM%" set %SERVICE% AppRotateBytes 10485760

echo === Правило брандмауэра ===
rem profile=any не перестраховка: сеть без домена Windows обычно относит
rem к «Общедоступной», а правило по привычке создают для «Частной».
netsh advfirewall firewall delete rule name="FilePost HTTP" >nul 2>&1
netsh advfirewall firewall add rule name="FilePost HTTP" dir=in action=allow ^
    protocol=TCP localport=8080 profile=any

echo.
echo Служба установлена. Запуск:  net start %SERVICE%
echo Остановка:                   net stop %SERVICE%
echo Удаление:                    "%NSSM%" remove %SERVICE% confirm
echo.
echo НЕ ЗАБЫТЬ:
echo  - исключение антивируса для каталога хранилища (режет скорость в разы)
echo  - путь [backup] в config.toml на ДРУГОМ физическом диске
endlocal
