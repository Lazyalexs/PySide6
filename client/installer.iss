; Установщик клиента FilePost. Собирается Inno Setup 6 (компилятор ISCC.exe).
;
;   1. pyinstaller filepost.spec        -> dist\FilePost.exe
;   2. ISCC.exe installer.iss           -> Output\FilePost-Setup-1.0.0.exe
;
; Ставится в %LOCALAPPDATA% и не требует прав администратора: раскатывать
; на семь станций проще, когда установщик не спрашивает пароль админа.

#define AppName        "FilePost"
#define AppVersion     "1.0.2"
#define AppPublisher   "FilePost"
#define AppExe         "FilePost.exe"

[Setup]
AppId={{8F3A1C42-5D7E-4B29-9E14-6A2C7D1F0B33}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

; Без прав администратора: установка идёт в профиль пользователя.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

; Русский интерфейс: у пользователей русская Windows.
ShowLanguageDialog=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; \
    GroupDescription: "Дополнительно:"
Name: "autostart"; Description: "Запускать вместе с Windows"; \
    GroupDescription: "Дополнительно:"

[Registry]
; Автозапуск свёрнутым в трей. Клиент умеет прописать этот ключ и сам,
; но при массовой раскатке удобнее решить это на этапе установки.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#AppName}"; \
    ValueData: """{app}\{#AppExe}"" --minimized"; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Каталог данных НЕ удаляем: там лежат полученные файлы и очередь передач.
; Пользователь решает сам, что с ними делать.
Type: dirifempty; Name: "{app}"

[Code]
{ Адрес сервера и код регистрации можно передать при тихой установке:

    FilePost-Setup-1.0.0.exe /VERYSILENT /SERVER=filepost-srv:8080 /STATION="Бухгалтерия, окно 2"

  Тогда клиент при первом запуске не спросит адрес — останется только код
  регистрации. Это то, ради чего пункт 3 раздела 4 архитектуры существует:
  пользователю вводить адрес не нужно. }

var
  Lines: TArrayOfString;

{ Вложенные процедуры Pascal Script в Inno Setup не поддерживает,
  поэтому и накопитель строк, и массив живут на верхнем уровне. }
procedure Add(const S: String);
begin
  SetArrayLength(Lines, GetArrayLength(Lines) + 1);
  Lines[GetArrayLength(Lines) - 1] := S;
end;

function GetCmdLineParam(const Key: String): String;
var
  I: Integer;
  Param: String;
begin
  Result := '';
  for I := 1 to ParamCount do
  begin
    Param := ParamStr(I);
    if Pos(Uppercase(Key) + '=', Uppercase(Param)) = 1 then
    begin
      Result := Copy(Param, Length(Key) + 2, MaxInt);
      Exit;
    end;
  end;
end;

procedure WriteDeploymentConfig();
var
  ConfigPath, Server, Station: String;
begin
  Server := GetCmdLineParam('/SERVER');
  Station := GetCmdLineParam('/STATION');
  if (Server = '') and (Station = '') then
    Exit;

  ConfigPath := ExpandConstant('{localappdata}\FilePost\config.ini');
  ForceDirectories(ExpandConstant('{localappdata}\FilePost'));
  if FileExists(ConfigPath) then
    Exit;  { не затираем настройки уже работающей станции }

  SetArrayLength(Lines, 0);

  { Пишем только параметры развёртывания. Ключ станции сюда не попадает:
    он выдаётся сервером при регистрации и появится в файле позже. }
  Add('[server]');
  if Server <> '' then
    Add('url = http://' + Server);
  Add('');
  Add('[station]');
  if Station <> '' then
    Add('display_name = ' + Station);

  { Именно UTF-8: название станции содержит кириллицу, а клиент читает
    config.ini как UTF-8. SaveStringToFile записала бы ANSI, и вместо
    «Бухгалтерия, окно 2» получились бы кракозябры. }
  SaveStringsToUTF8FileWithoutBOM(ConfigPath, Lines, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteDeploymentConfig();
end;
