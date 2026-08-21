; Установщик службы FilePost. Собирается Inno Setup 6 (ISCC.exe).
;
;   1. pyinstaller filepost-server.spec  -> dist\filepost-server.exe
;   2. положить nssm.exe рядом с этим файлом
;   3. ISCC.exe installer.iss            -> Output\FilePost-Server-Setup-1.0.0.exe
;
; Устанавливает службу целиком: exe, config.toml под выбранные пути, NSSM,
; правило брандмауэра, исключение Defender. Python на сервере не нужен.

#define AppName        "FilePost Server"
#define AppVersion     "1.0.1"
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

{ config.toml генерирует сам сервер: `init --root` подставляет фактический
  каталог установки. Собирать TOML здесь нельзя — формат жил бы в двух местах,
  в Python и в Pascal, и расходился при первой правке. Так и вышло в 1.0.0:
  пути записались с одинарными обратными слэшами, а в TOML это экранирование,
  и служба не стартовала с ошибкой разбора.

  Код регистрации читаем из logs\enrollment-code.txt, а не из вывода консоли:
  там русский текст в кодировке консоли, который в мастере стал бы кракозябрами.
  Сам код — чистый ASCII. }
function RunInit(): String;
var
  ResultCode: Integer;
  Found: TArrayOfString;
begin
  Exec(ExpandConstant('{app}\{#AppExe}'),
       '--config "' + ExpandConstant('{app}\config.toml') + '"' +
       ' init --root "' + ExpandConstant('{app}') + '"',
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
    CodePage.RichEditViewer.Text := RunInit();
end;
