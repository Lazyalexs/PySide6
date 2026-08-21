; Установщик службы FilePost. Собирается Inno Setup 6 (ISCC.exe).
;
;   1. pyinstaller filepost-server.spec  -> dist\filepost-server.exe
;   2. положить nssm.exe рядом с этим файлом
;   3. ISCC.exe installer.iss            -> Output\FilePost-Server-Setup-1.0.0.exe
;
; Устанавливает службу целиком: exe, config.toml под выбранные пути, NSSM,
; правило брандмауэра, исключение Defender. Python на сервере не нужен.

#define AppName        "FilePost Server"
#define AppVersion     "1.0.0"
#define ServiceName    "FilePost"
#define AppExe         "filepost-server.exe"

[Setup]
AppId={{2C7B9E51-4A38-4D62-B0F7-1E9D3A5C8B22}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName=D:\FilePost
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=FilePost-Server-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

; Служба и правило брандмауэра требуют администратора.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
ShowLanguageDialog=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "nssm.exe";       DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";      DestDir: "{app}"; Flags: ignoreversion isreadme

[Dirs]
; storage и tmp обязаны лежать на одном томе: перемещение собранного файла
; должно быть мгновенным rename, а не копированием гигабайтов между дисками.
Name: "{app}\storage"
Name: "{app}\tmp"
Name: "{app}\db"
Name: "{app}\logs"

[Tasks]
Name: "firewall"; Description: "Открыть порт 8080 в брандмауэре"; \
    GroupDescription: "Настройка сервера:"
Name: "defender"; Description: "Исключить хранилище из проверки Microsoft Defender"; \
    GroupDescription: "Настройка сервера:"
Name: "service";  Description: "Зарегистрировать и запустить службу Windows"; \
    GroupDescription: "Настройка сервера:"

[Run]
; Правило брандмауэра. profile=any не перестраховка: сеть без домена Windows
; обычно относит к «Общедоступной», а правило по привычке создают для «Частной».
Filename: "netsh"; \
    Parameters: "advfirewall firewall add rule name=""FilePost HTTP"" dir=in action=allow protocol=TCP localport=8080 profile=any"; \
    StatusMsg: "Настройка брандмауэра..."; Flags: runhidden; Tasks: firewall

; Исключение Defender: realtime-сканирование режет скорость записи в разы
; и может перехватить файл ровно в момент сборки из чанков.
Filename: "powershell"; \
    Parameters: "-NoProfile -Command ""if (Get-Command Add-MpPreference -ErrorAction SilentlyContinue) {{ Add-MpPreference -ExclusionPath '{app}' -ErrorAction SilentlyContinue; Add-MpPreference -ExclusionProcess '{app}\{#AppExe}' -ErrorAction SilentlyContinue }}"""; \
    StatusMsg: "Исключение антивируса..."; Flags: runhidden; Tasks: defender

; Регистрация службы.
Filename: "{app}\nssm.exe"; \
    Parameters: "install {#ServiceName} ""{app}\{#AppExe}"" --config ""{app}\config.toml"" serve"; \
    StatusMsg: "Регистрация службы..."; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppDirectory ""{app}"""; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} DisplayName ""FilePost — обмен файлами"""; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} Start SERVICE_AUTO_START"; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppExit Default Restart"; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppRestartDelay 5000"; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppThrottle 10000"; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppStdout ""{app}\logs\service.log"""; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppStderr ""{app}\logs\service.log"""; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppRotateFiles 1"; \
    Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "start {#ServiceName}"; \
    StatusMsg: "Запуск службы..."; Flags: runhidden; Tasks: service

[UninstallRun]
Filename: "{app}\nssm.exe"; Parameters: "stop {#ServiceName}"; Flags: runhidden; RunOnceId: "StopSvc"
Filename: "{app}\nssm.exe"; Parameters: "remove {#ServiceName} confirm"; Flags: runhidden; RunOnceId: "DelSvc"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FilePost HTTP"""; \
    Flags: runhidden; RunOnceId: "DelRule"

[UninstallDelete]
; Хранилище и база НЕ удаляются: там переписка и файлы пользователей.
Type: filesandordirs; Name: "{app}\logs"

[Code]
var
  CodePage: TOutputMsgMemoWizardPage;
  Lines: TArrayOfString;

{ Копим строки в массиве, а не в одной строке с CRLF: писать нужно
  через SaveStringsToUTF8FileWithoutBOM, а она принимает именно массив.
  SaveStringToFile здесь не годится — она пишет в ANSI, и русские
  комментарии стали бы байтами cp1251, невалидным UTF-8, на котором
  tomllib падает: служба не запустилась бы после установки вовсе. }
procedure Add(const S: String);
begin
  SetArrayLength(Lines, GetArrayLength(Lines) + 1);
  Lines[GetArrayLength(Lines) - 1] := S;
end;

{ config.toml генерируется под фактический каталог установки. Иначе пришлось бы
  просить администратора править пути руками, а это первая же ошибка развёртывания. }
procedure WriteConfig();
var
  Path: String;
begin
  Path := ExpandConstant('{app}\config.toml');
  if FileExists(Path) then
    Exit;  { не затираем настройки при обновлении }

  SetArrayLength(Lines, 0);

  Add('[server]');
  Add('host = "0.0.0.0"');
  Add('port = 8080');
  Add('token_ttl_hours = 12');
  Add('presence_timeout_sec = 45');
  Add('min_client_version = "1.0.0"');
  Add('discovery_enabled = false');
  Add('discovery_port = 8081');
  Add('');

  Add('[storage]');
  { Пути с двойными слэшами: в TOML обратный слэш внутри строки — экранирование. }
  Add('path = "' + ExpandConstant('{app}\storage') + '"');
  Add('tmp_path = "' + ExpandConstant('{app}\tmp') + '"');
  Add('chunk_size_mb = 16');
  Add('min_free_space_gb = 50');
  Add('max_file_size_gb = 20');
  Add('');

  Add('[retention]');
  Add('# ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО: ничего не удаляется само.');
  Add('enabled = false');
  Add('delete_after_download_days = 7');
  Add('delete_never_downloaded_days = 30');
  Add('notify_sender_before_days = 2');
  Add('delete_orphaned = false');
  Add('');

  Add('[cleanup]');
  Add('abandoned_uploads_hours = 48');
  Add('reservation_idle_hours = 2');
  Add('events_retention_days = 30');
  Add('');

  Add('[backup]');
  Add('# ВАЖНО: путь должен указывать на ДРУГОЙ физический диск.');
  Add('# Копия рядом с оригиналом защищает только от случайного удаления');
  Add('# и бесполезна в том, ради чего бэкап делается. Если такого диска');
  Add('# на сервере нет, поправьте путь: иначе в журнале будет ошибка');
  Add('# db.backup_failed при каждом прогоне уборки.');
  Add('enabled = true');
  Add('path = "E:\\Backup\\FilePost"');
  Add('time = "03:00"');
  Add('keep_copies = 14');
  Add('');

  Add('[limits]');
  Add('max_parallel_uploads_per_user = 2');
  Add('max_parallel_downloads_per_user = 2');
  Add('max_recipients_per_message = 20');
  Add('max_attachments_per_message = 50');
  Add('max_subject_length = 200');
  Add('max_body_length = 10000');

  SaveStringsToUTF8FileWithoutBOM(Path, Lines, False);
end;

{ Код регистрации первой станции показывается один раз. Пропустить его нельзя:
  без него не зарегистрировать ни одного клиента, а второй раз init не сработает.

  Код читаем из logs\enrollment-code.txt, а не из вывода консоли: там русский
  текст в кодировке консоли, который в мастере превратится в кракозябры.
  Сам код — чистый ASCII, поэтому проблем с кодировкой нет. }
function RunInit(): String;
var
  ResultCode: Integer;
  Found: TArrayOfString;
begin
  Exec(ExpandConstant('{app}\{#AppExe}'),
       '--config "' + ExpandConstant('{app}\config.toml') + '" init',
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if LoadStringsFromFile(ExpandConstant('{app}\logs\enrollment-code.txt'), Found) then
    if GetArrayLength(Found) > 0 then
    begin
      Result :=
        'Код регистрации первой станции:' + #13#10 + #13#10 +
        '        ' + Trim(Found[0]) + #13#10 + #13#10 +
        'Действует 24 часа и только один раз.' + #13#10 +
        'Станция, зарегистрированная по нему, получит права администратора:' + #13#10 +
        'с неё выдаются коды остальным станциям.';
      Exit;
    end;

  { База уже была инициализирована — это обновление, а не первая установка. }
  Result :=
    'База данных уже инициализирована, новый код не создавался.' + #13#10 + #13#10 +
    'Чтобы добавить станцию, выполните на сервере:' + #13#10 +
    '    filepost-server.exe --config config.toml station enroll';
end;

procedure InitializeWizard();
begin
  CodePage := CreateOutputMsgMemoPage(
    wpInstalling,
    'Код регистрации первой станции',
    'Запишите его: он показывается один раз',
    'Без этого кода не зарегистрировать ни одного клиента.',
    '');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteConfig();
    CodePage.RichEditViewer.Text := RunInit();
  end;
end;
