@echo off
rem Установка FilePost как службы Windows через NSSM. Раздел 4 архитектуры.
rem Запускать от имени администратора.

setlocal
set ROOT=%~dp0
set NSSM=%ROOT%nssm.exe
set EXE=%ROOT%filepost-server.exe
set SERVICE=FilePost

rem Каталог хранилища из config.toml. Если он лежит не на D:, поправьте здесь —
rem скрипт не разбирает TOML, чтобы не тянуть зависимости в установщик.
set STORAGE=D:\FilePost

if not exist "%NSSM%" (
    echo Не найден nssm.exe рядом со скриптом. Скачайте NSSM и положите его сюда.
    exit /b 1
)
if not exist "%EXE%" (
    echo Не найден %EXE%. Соберите его: build.cmd на машине сборки,
    echo затем скопируйте filepost-server.exe сюда. Python на сервере не нужен.
    exit /b 1
)

echo === Установка службы %SERVICE% ===
"%NSSM%" install %SERVICE% "%EXE%" "--config" "%ROOT%config.toml" "serve"
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

echo === Исключение Microsoft Defender ===
rem Realtime-сканирование режет скорость в разы и может перехватить файл
rem в момент сборки. Исключаем каталог хранилища и сам процесс службы.
rem Если стоит сторонний антивирус, Defender обычно отключён — эти команды
rem тогда просто ничего не сделают, и исключение надо завести в его консоли.
powershell -NoProfile -Command ^
    "if (Get-Command Add-MpPreference -ErrorAction SilentlyContinue) {" ^
    "  Add-MpPreference -ExclusionPath '%STORAGE%' -ErrorAction SilentlyContinue;" ^
    "  Add-MpPreference -ExclusionProcess '%EXE%' -ErrorAction SilentlyContinue;" ^
    "  Write-Host 'Исключения Defender добавлены';" ^
    "  Get-MpPreference ^| Select-Object -ExpandProperty ExclusionPath" ^
    "} else { Write-Host 'Microsoft Defender не найден — заведите исключение вручную' }"

echo.
echo Служба установлена. Запуск:  net start %SERVICE%
echo Остановка:                   net stop %SERVICE%
echo Удаление:                    "%NSSM%" remove %SERVICE% confirm
echo.
echo НЕ ЗАБЫТЬ:
echo  - проверить, что исключение антивируса действительно применилось
echo  - путь [backup] в config.toml на ДРУГОМ физическом диске
echo  - зеркальный том под хранилище: файлы не бэкапятся осознанно
endlocal
