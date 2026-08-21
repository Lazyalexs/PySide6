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

{ config.toml генерируется под фактический каталог установки. Иначе пришлось бы
  просить администратора править пути руками, а это первая же ошибка развёртывания. }
procedure WriteConfig();
var
  Path, Content, Backup: String;
begin
  Path := ExpandConstant('{app}\config.toml');
  if FileExists(Path) then
    Exit;  { не затираем настройки при обновлении }

  { Бэкап по умолчанию — на другой диск. Копия рядом с оригиналом защищает
    ровно от одного сценария и бесполезна в том, ради чего делается. }
  Backup := 'E:\\Backup\\FilePost';

  Content :=
    '[server]' + #13#10 +
    'host = "0.0.0.0"' + #13#10 +
    'port = 8080' + #13#10 +
    'token_ttl_hours = 12' + #13#10 +
    'presence_timeout_sec = 45' + #13#10 +
    'min_client_version = "1.0.0"' + #13#10 +
    'discovery_enabled = false' + #13#10 +
    'discovery_port = 8081' + #13#10 + #13#10 +
    '[storage]' + #13#10 +
    'path = "' + ExpandConstant('{app}\storage') + '"' + #13#10 +
    'tmp_path = "' + ExpandConstant('{app}\tmp') + '"' + #13#10 +
    'chunk_size_mb = 16' + #13#10 +
    'min_free_space_gb = 50' + #13#10 +
    'max_file_size_gb = 20' + #13#10 + #13#10 +
    '[retention]' + #13#10 +
    'enabled = false' + #13#10 +
    'delete_after_download_days = 7' + #13#10 +
    'delete_never_downloaded_days = 30' + #13#10 +
    'notify_sender_before_days = 2' + #13#10 +
    'delete_orphaned = false' + #13#10 + #13#10 +
    '[cleanup]' + #13#10 +
    'abandoned_uploads_hours = 48' + #13#10 +
    'reservation_idle_hours = 2' + #13#10 +
    'events_retention_days = 30' + #13#10 + #13#10 +
    '[backup]' + #13#10 +
    '# ВАЖНО: путь должен указывать на ДРУГОЙ физический диск.' + #13#10 +
    '# Копия рядом с оригиналом защищает только от случайного удаления' + #13#10 +
    '# и бесполезна в том, ради чего бэкап делается. Если такого диска' + #13#10 +
    '# на сервере нет, поправьте путь — иначе в журнале будет ошибка' + #13#10 +
    '# db.backup_failed при каждом прогоне уборки.' + #13#10 +
    'enabled = true' + #13#10 +
    'path = "' + Backup + '"' + #13#10 +
    'time = "03:00"' + #13#10 +
    'keep_copies = 14' + #13#10 + #13#10 +
    '[limits]' + #13#10 +
    'max_parallel_uploads_per_user = 2' + #13#10 +
    'max_parallel_downloads_per_user = 2' + #13#10 +
    'max_recipients_per_message = 20' + #13#10 +
    'max_attachments_per_message = 50' + #13#10 +
    'max_subject_length = 200' + #13#10 +
    'max_body_length = 10000' + #13#10;

  { Именно UTF-8: SaveStringToFile пишет в ANSI, и русские
    комментарии превратились бы в байты cp1251, на которых
    tomllib падает — служба не запустилась бы после установки. }
  SaveStringToUTF8File(Path, Content, False);
end;

{ Код регистрации первой станции показывается один раз. Пропустить его нельзя:
  без него не зарегистрировать ни одного клиента, а второй раз init не сработает.

  Код читаем из logs\enrollment-code.txt, а не из вывода консоли: там русский
  текст в кодировке консоли, который в мастере превратится в кракозябры.
  Сам код — чистый ASCII, поэтому проблем с кодировкой нет. }
function RunInit(): String;
var
  ResultCode: Integer;
  Lines: TArrayOfString;
begin
  Exec(ExpandConstant('{app}\{#AppExe}'),
       '--config "' + ExpandConstant('{app}\config.toml') + '" init',
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if LoadStringsFromFile(ExpandConstant('{app}\logs\enrollment-code.txt'), Lines) then
    if GetArrayLength(Lines) > 0 then
    begin
      Result :=
        'Код регистрации первой станции:' + #13#10 + #13#10 +
        '        ' + Trim(Lines[0]) + #13#10 + #13#10 +
        'Действует 24 часа и только один раз.' + #13#10 +
        'Станция, зарегистрированная по нему, получит права администратора —' + #13#10 +
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
    'Запишите его — он показывается один раз',
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
