; Установщик службы FilePost. Собирается Inno Setup 6 (ISCC.exe).
;
;   1. pyinstaller filepost-server.spec  -> dist\filepost-server.exe
;   2. положить nssm.exe рядом с этим файлом
;   3. ISCC.exe installer.iss            -> Output\FilePost-Server-Setup-1.0.0.exe
;
; Устанавливает службу целиком: exe, config.toml под выбранные пути, NSSM,
; правило брандмауэра, исключение Defender. Python на сервере не нужен.

#define AppName        "FilePost Server"
#define AppVersion     "1.0.2"
#define ServiceName    "FilePost"
#define AppExe         "filepost-server.exe"
#define DefaultPort    "50067"

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
Name: "firewall"; Description: "Открыть порт в брандмауэре Windows"; \
    GroupDescription: "Настройка сервера:"
Name: "defender"; Description: "Исключить хранилище из проверки Microsoft Defender"; \
    GroupDescription: "Настройка сервера:"
Name: "service";  Description: "Зарегистрировать и запустить службу Windows"; \
    GroupDescription: "Настройка сервера:"

[Run]
; Правило брандмауэра. profile=any не перестраховка: сеть без домена Windows
; обычно относит к «Общедоступной», а правило по привычке создают для «Частной».
Filename: "netsh"; \
    Parameters: "advfirewall firewall add rule name=""FilePost HTTP"" dir=in action=allow protocol=TCP localport={code:GetPort} profile=any"; \
    StatusMsg: "Настройка брандмауэра..."; Flags: runhidden; Tasks: firewall

; Резервирование порта. Порты выше 49152 система раздаёт исходящим соединениям,
; и служба иногда не стартует после перезагрузки, потому что порт уже занят.
; Отказ команды не критичен: диапазон мог быть зарезервирован ранее.
Filename: "netsh"; \
    Parameters: "int ipv4 add excludedportrange protocol=tcp startport={code:GetPort} numberofports=1"; \
    StatusMsg: "Резервирование порта..."; Flags: runhidden; Tasks: firewall

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
  PortPage: TInputQueryWizardPage;
  SecurityPage: TOutputMsgMemoWizardPage;

function GetPort(Param: String): String;
begin
  { GetPort вызывается и из [Run] через {code:GetPort}, и из кода мастера.
    В тихой установке страницы порта нет, поэтому проверяем на nil. }
  Result := '';
  if PortPage <> nil then
    Result := Trim(PortPage.Values[0]);
  if Result = '' then
    Result := '{#DefaultPort}';
end;

{ Обнаружение средств защиты, которые перехватывают трафик и файловые операции
  раньше штатных механизмов Windows. Молча «настроить» их установщик не может:
  и Kaspersky, и ViPNet управляются своими консолями, а в корпоративной сборке
  ещё и политикой. Поэтому честнее показать администратору точный список того,
  что придётся сделать руками. }
function ServiceExists(const Name: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C sc query "' + Name + '" | find "SERVICE_NAME"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function KasperskyPresent(): Boolean;
begin
  Result := ServiceExists('AVP') or ServiceExists('AVP.KES') or
            ServiceExists('klnagent') or
            DirExists(ExpandConstant('{commonpf}\Kaspersky Lab')) or
            DirExists(ExpandConstant('{commonpf32}\Kaspersky Lab'));
end;

function ViPNetPresent(): Boolean;
begin
  Result := ServiceExists('ITCSSVC') or ServiceExists('ViPNet Client') or
            DirExists(ExpandConstant('{commonpf}\InfoTeCS')) or
            DirExists(ExpandConstant('{commonpf32}\InfoTeCS'));
end;

function SecurityNotes(): String;
var
  Port: String;
begin
  Port := GetPort('');
  Result := '';

  if KasperskyPresent() then
    Result := Result +
      'ОБНАРУЖЕН KASPERSKY' + #13#10 +
      'Исключение Microsoft Defender, которое ставит этот установщик, не поможет:' + #13#10 +
      'при установленном Касперском Defender отключён. Заведите в консоли' + #13#10 +
      'Касперского два исключения, иначе скорость записи упадёт в разы:' + #13#10 +
      '  1. Доверенная папка:   ' + ExpandConstant('{app}') + #13#10 +
      '  2. Доверенный процесс: ' + ExpandConstant('{app}\{#AppExe}') + #13#10 +
      'Проверьте также «Защиту от сетевых атак»: нестандартный порт она может' + #13#10 +
      'посчитать подозрительным при массовой передаче.' + #13#10 + #13#10;

  if ViPNetPresent() then
    Result := Result +
      'ОБНАРУЖЕН VIPNET' + #13#10 +
      'У ViPNet собственный сетевой экран, и он фильтрует трафик РАНЬШЕ' + #13#10 +
      'брандмауэра Windows. Правила, созданного этим установщиком,' + #13#10 +
      'недостаточно — станции не увидят сервер.' + #13#10 +
      'В консоли ViPNet разрешите входящий TCP на порт ' + Port + '.' + #13#10 +
      'Если станции работают через защищённую сеть ViPNet, адресом сервера' + #13#10 +
      'для них будет его адрес В СЕТИ VIPNET, а не обычный IP.' + #13#10 + #13#10;

  if Result = '' then
    Result :=
      'Сторонних средств защиты не обнаружено.' + #13#10 + #13#10 +
      'Если Kaspersky или ViPNet будут установлены позже, потребуется' + #13#10 +
      'завести исключения и разрешить порт ' + Port + ' в их консолях.';
end;

{ config.toml генерирует сам сервер: `init --root` подставляет фактический
  каталог установки и порт. Собирать TOML здесь нельзя — формат жил бы в двух
  местах, в Python и в Pascal, и расходился при первой правке. Так и вышло
  в 1.0.0: пути записались с одинарными обратными слэшами, а в TOML это
  экранирование, и служба не стартовала с ошибкой разбора.

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
       ' init --root "' + ExpandConstant('{app}') + '"' +
       ' --port ' + GetPort(''),
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if LoadStringsFromFile(ExpandConstant('{app}\logs\enrollment-code.txt'), Found) then
    if GetArrayLength(Found) > 0 then
    begin
      Result :=
        'Код регистрации первой станции:' + #13#10 + #13#10 +
        '        ' + Trim(Found[0]) + #13#10 + #13#10 +
        'Адрес сервера для станций:  <IP сервера>:' + GetPort('') + #13#10 + #13#10 +
        'Код действует 24 часа и только один раз.' + #13#10 +
        'Станция, зарегистрированная по нему, получит права администратора:' + #13#10 +
        'с неё выдаются коды остальным станциям.';
      Exit;
    end;

  Result :=
    'База данных уже инициализирована, новый код не создавался.' + #13#10 + #13#10 +
    'Чтобы добавить станцию, выполните на сервере:' + #13#10 +
    '    filepost-server.exe --config config.toml station enroll';
end;

procedure InitializeWizard();
begin
  PortPage := CreateInputQueryPage(
    wpSelectDir,
    'Порт службы',
    'На каком порту сервер будет принимать соединения',
    'Порт должен быть свободен и разрешён во всех сетевых экранах.' + #13#10 +
    'Значение по умолчанию выбрано непопулярным намеренно: на 8080 обычно' + #13#10 +
    'уже что-то работает.');
  PortPage.Add('Порт:', False);
  PortPage.Values[0] := '{#DefaultPort}';

  SecurityPage := CreateOutputMsgMemoPage(
    wpReady,
    'Средства защиты',
    'Что придётся настроить вручную',
    'Установщик не может настроить сторонние средства защиты за вас.',
    '');

  CodePage := CreateOutputMsgMemoPage(
    wpInstalling,
    'Код регистрации первой станции',
    'Запишите его: он показывается один раз',
    'Без этого кода не зарегистрировать ни одного клиента.',
    '');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
begin
  Result := True;
  if CurPageID = PortPage.ID then
  begin
    Port := StrToIntDef(Trim(PortPage.Values[0]), 0);
    if (Port < 1024) or (Port > 65535) then
    begin
      MsgBox('Введите порт от 1024 до 65535.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    { Предупреждаем, но не запрещаем: резервирование ниже снимает проблему. }
    if Port >= 49152 then
      MsgBox('Порт ' + IntToStr(Port) + ' лежит в динамическом диапазоне Windows,' + #13#10 +
             'откуда система раздаёт порты исходящим соединениям.' + #13#10 + #13#10 +
             'Установщик зарезервирует его, иначе служба иногда не стартовала бы' + #13#10 +
             'после перезагрузки. Это нормально, продолжайте.',
             mbInformation, MB_OK);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = SecurityPage.ID then
    SecurityPage.RichEditViewer.Text := SecurityNotes();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    CodePage.RichEditViewer.Text := RunInit();
end;
