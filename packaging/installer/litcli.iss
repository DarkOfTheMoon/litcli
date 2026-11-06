; Inno Setup script for the Windows installer.
;
; Compile with:  iscc /DVersion=0.4.0 /DStaging=build\stage\litcli packaging\installer\litcli.iss
; The staging directory is the PyInstaller onedir output (litcli.exe plus its dependencies).
;
; Install scope is per-user (no admin): %LOCALAPPDATA%\Programs\litcli, added to the user PATH.
; Chromium is not bundled — the "browser" task downloads it afterwards via the binary itself.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Staging
  #define Staging "..\..\build\stage\litcli"
#endif

[Setup]
AppId={{8F2C31B4-6A1E-4C39-9E0D-5B7A4C1D9F02}
AppName=litcli
AppVersion={#Version}
AppPublisher=ZhangHuiJie
AppPublisherURL=https://gitee.com/simult/litcli
AppSupportURL=https://gitee.com/simult/litcli/issues
DefaultDirName={localappdata}\Programs\litcli
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist\bin
OutputBaseFilename=litcli-{#Version}-windows-x86_64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName=litcli {#Version}

[Languages]
; Default.isl is Inno's English; a Chinese message file comes with the language pack, which is
; not guaranteed on a bare install, so the copy stays bilingual instead of switching files.
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "browser"; Description: "下载 Chromium（知网 / 豆瓣后端需要，约 150 MB） / Download Chromium (needed by the CNKI / Douban backends, ~150 MB)"; GroupDescription: "可选组件 / Optional"; Flags: unchecked

[Files]
Source: "{#Staging}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Run]
; The binary downloads the browser itself, so the version fetched always matches the driver.
Filename: "{app}\litcli.exe"; Parameters: "backends install-browser"; StatusMsg: "正在下载 Chromium … / Downloading Chromium …"; Flags: runhidden waituntilterminated; Tasks: browser
Filename: "{app}\litcli.exe"; Parameters: "backends"; Description: "列出已配置后端 / List configured backends"; Flags: postinstall runhidden nowait shellexec

[Registry]
; Per-user PATH entry (Administrative installs would use HKLM + PrivilegesRequired=admin).
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Check: NeedsAddPath('{app}')

[UninstallDelete]
; Browser binaries live in %LOCALAPPDATA%\ms-playwright and survive an uninstall on purpose.
Type: dirifempty; Name: "{app}"

[Code]
// Not a built-in: tell running shells that the user environment changed.
procedure SendMessageTimeout(hWnd, Msg, wParam: Longint; lParam: String;
  fuFlags, uTimeout: Longint; var lpdwResult: Longint);
  external 'SendMessageTimeoutW@user32.dll stdcall';

function NeedsAddPath(Param: string): Boolean;
var
  Paths: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemoveFromPath(Param: string);
var
  Paths, Entry, Result_: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    exit;
  Entry := ';' + Uppercase(Param) + ';';
  Paths := ';' + Paths + ';';
  P := Pos(Entry, Uppercase(Paths));
  if P > 0 then
    Delete(Paths, P, Length(Entry));
  while Copy(Paths, 1, 1) = ';' do
    Delete(Paths, 1, 1);
  while (Length(Paths) > 0) and (Paths[Length(Paths)] = ';') do
    Delete(Paths, Length(Paths), 1);
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths);
end;

var Result_: Longint;

procedure BroadcastEnvironment;
begin
  SendMessageTimeout(HWND_BROADCAST, $001A, 0, 'Environment', $0002, 5000, Result_);  // WM_SETTINGCHANGE, SMTO_ABORTIFHUNG
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    BroadcastEnvironment;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromPath(ExpandConstant('{app}'));
    BroadcastEnvironment;
  end;
end;
