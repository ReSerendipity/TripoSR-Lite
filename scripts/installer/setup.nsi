; TripoSR-Lite 桌面版 NSIS 当前用户安装器（引导式：数据分卷随 Setup 同目录分发）
; 编译：makensis /DAPP_VERSION=... /DSRC_DIR=... /DOUT_DIR=... /DSRC_DATA=... /DSRC_SHELL=... /DSRC_7ZA=... /DAPP_ICON=... /DDATA_VOLUMES=... /DEST_KB=... setup.nsi
; 必须用 UTF-8 BOM 保存（含中文 DetailPrint / MessageBox）。
;
; 为什么数据卷不内嵌：NSIS 单文件安装包上限约 2GB（32 位偏移），本包外部推理运行时
; （torch cu128）约 5.1GB，无法塞进一个 Setup。Setup 只内嵌壳 exe 与独立 7za，
; TripoSR-Lite-Data.7z.001..NNN 必须与 Setup 同目录（或 data\ 子目录）。
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "StrFunc.nsh"
; CLI 全局命令：PATH 幂等增删。安装段与卸载段分别 Using（不能混在同一个宏）。
${Using:StrFunc} StrStr
${Using:StrFunc} UnStrStr
${Using:StrFunc} UnStrRep

!define APP_NAME "TripoSR-Lite"
!define APP_DISPLAY_NAME "TripoSR-Lite 单图重建工作台"
!define APP_EXE "TripoSR-Lite.exe"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\TripoSR-Lite"
!define APP_REG_KEY "Software\TripoSR-Lite"
!define USER_DATA_DIR "$LOCALAPPDATA\TripoSR-Lite"
!define DATA_PREFIX "TripoSR-Lite-Data.7z"

!ifndef APP_VERSION
  !define APP_VERSION "0.1.0"
!endif
!ifndef OUT_DIR
  !define OUT_DIR "."
!endif
!ifndef SRC_DIR
  !define SRC_DIR "..\..\dist\desktop-staging"
!endif
!ifndef SRC_DATA
  !error "缺少 -DSRC_DATA=<TripoSR-Lite-Data.7z.001 路径>"
!endif
!ifndef SRC_SHELL
  !error "缺少 -DSRC_SHELL=<TripoSR-Lite.exe 路径>"
!endif
!ifndef SRC_7ZA
  !define SRC_7ZA "7za.exe"
!endif
!ifndef APP_ICON
  !define APP_ICON "..\..\desktop\src-tauri\icons\icon.ico"
!endif
!ifndef DATA_VOLUMES
  !define DATA_VOLUMES 3
!endif
!ifndef EST_KB
  !define EST_KB 5500000
!endif

Name "${APP_DISPLAY_NAME}"
OutFile "${OUT_DIR}\${APP_NAME}-Setup-v${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${APP_REG_KEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor lzma
CRCCheck on
BrandingText "${APP_NAME}"

Var TOOLSDIR
Var DATADIR
Var KEEP_DATA

; ---------- CLI 全局命令：把 $INSTDIR 精确追加到当前用户 PATH（幂等）----------
; 用 ";...;" 包裹后做整段匹配，避免 C:\Foo 命中 C:\FooBar。
Function AddToUserPath
  Push $0
  Push $1
  Push $2
  Push $3
  ReadRegStr $1 HKCU "Environment" "Path"
  StrCpy $2 ";$1;"
  ${StrStr} $3 "$2" ";$INSTDIR;"
  ${If} $3 == ""
    ${If} $1 == ""
      StrCpy $1 "$INSTDIR"
    ${Else}
      StrCpy $0 "$1" 1 -1
      ${If} $0 == ";"
        StrCpy $1 "$1$INSTDIR"
      ${Else}
        StrCpy $1 "$1;$INSTDIR"
      ${EndIf}
    ${EndIf}
    WriteRegExpandStr HKCU "Environment" "Path" "$1"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
    DetailPrint "已将 CLI 命令目录加入当前用户 PATH: $INSTDIR"
  ${Else}
    DetailPrint "CLI 命令目录已在当前用户 PATH 中，跳过"
  ${EndIf}
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; ---------- 卸载：从当前用户 PATH 精确移除 $INSTDIR 条目 ----------
Function un.RemoveFromUserPath
  Push $0
  Push $1
  Push $2
  ReadRegStr $1 HKCU "Environment" "Path"
  StrCpy $2 ";$1;"
  ${UnStrStr} $0 "$2" ";$INSTDIR;"
  ${IfNot} $0 == ""
    ${UnStrRep} $2 "$2" ";$INSTDIR;" ";"
    ${UnStrRep} $2 "$2" ";$INSTDIR;" ";"
    ${UnStrRep} $2 "$2" ";;" ";"
    ${UnStrRep} $2 "$2" ";;" ";"
    StrCpy $0 "$2" 1
    ${If} $0 == ";"
      StrCpy $2 "$2" "" 1
    ${EndIf}
    StrCpy $0 "$2" 1 -1
    ${If} $0 == ";"
      StrCpy $2 "$2" -1
    ${EndIf}
    WriteRegExpandStr HKCU "Environment" "Path" "$2"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
    DetailPrint "已从当前用户 PATH 移除 CLI 命令目录: $INSTDIR"
  ${EndIf}
  Pop $2
  Pop $1
  Pop $0
FunctionEnd


; ---------- 终止运行中的壳 ----------
!macro KillRunning
  DetailPrint "检测到 ${APP_NAME} 可能正在运行，正在终止..."
  nsExec::ExecToLog 'taskkill /f /im ${APP_EXE} /T'
  Pop $0
  Sleep 1500
!macroend

; ---------- 数据卷定位与完整性（全部 MessageBox 走 IfSilent 守卫）----------
!macro RequireVolume N
  ${IfNot} ${FileExists} "$DATADIR\${DATA_PREFIX}.00${N}"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "缺少数据分卷 ${DATA_PREFIX}.00${N}。$\r$\n请把 ${APP_NAME}-Data.7z.001..00${DATA_VOLUMES} 与安装器放在同一目录，并用 SHA256SUMS 校验完整性。"
    ${EndIf}
    Abort
  ${EndIf}
!macroend

; ---------- MUI 页面 ----------
!define MUI_ABORTWARNING
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SRC_DIR}\app\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_DISPLAY_NAME}"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; ---------- 安装 ----------
Section "${APP_DISPLAY_NAME}" SEC_APP
  StrCpy $TOOLSDIR "$TEMP\triposr-lite-installer"
  RMDir /r "$TOOLSDIR"
  CreateDirectory "$TOOLSDIR"
  StrCpy $DATADIR "$EXEDIR"

  !insertmacro KillRunning

  ; 数据分卷：优先与 Setup 同目录，其次 data\ 子目录
  ${IfNot} ${FileExists} "$DATADIR\${DATA_PREFIX}.001"
    StrCpy $DATADIR "$EXEDIR\data"
    ${IfNot} ${FileExists} "$DATADIR\${DATA_PREFIX}.001"
      ${IfNot} ${Silent}
        MessageBox MB_ICONSTOP "未找到数据分卷 ${DATA_PREFIX}.001。$\r$\n本安装器为引导式：请将 ${APP_NAME}-Setup-v${APP_VERSION}.exe 与 ${APP_NAME}-Data.7z.001..NNN 放在同一目录后重新运行。"
      ${EndIf}
      Abort
    ${EndIf}
  ${EndIf}

  !insertmacro RequireVolume 1
  !if ${DATA_VOLUMES} > 1
    !insertmacro RequireVolume 2
  !endif
  !if ${DATA_VOLUMES} > 2
    !insertmacro RequireVolume 3
  !endif
  !if ${DATA_VOLUMES} > 3
    !insertmacro RequireVolume 4
  !endif
  !if ${DATA_VOLUMES} > 4
    !insertmacro RequireVolume 5
  !endif

  ; 独立 7za（7z.exe 依赖 7z.dll，内嵌后运行会静默失败）
  SetOutPath "$TOOLSDIR"
  File /oname=7za.exe "${SRC_7ZA}"

  CreateDirectory "$INSTDIR"
  DetailPrint "正在解压应用与推理运行时（约 5.2GB，视磁盘速度需 3-8 分钟，请勿关闭窗口）..."
  ExecWait '"$TOOLSDIR\7za.exe" x "$DATADIR\${DATA_PREFIX}.001" -o"$INSTDIR" -y -bso0 -bsp0' $0
  Delete "$TOOLSDIR\7za.exe"
  ${If} $0 != 0
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "数据解压失败（7z 错误码 $0）。$\r$\n请检查磁盘空间（安装需约 6GB）与数据分卷完整性（SHA256SUMS）。"
    ${EndIf}
    Abort
  ${EndIf}

  ; 壳 exe（数据卷只含 app/，不含壳）
  SetOutPath "$INSTDIR"
  File /oname=${APP_EXE} "${SRC_SHELL}"
  ${IfNot} ${FileExists} "$INSTDIR\${APP_EXE}"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "安装异常：未找到 ${APP_EXE}。"
    ${EndIf}
    Abort
  ${EndIf}
  ${IfNot} ${FileExists} "$INSTDIR\app\scripts\desktop_launch.py"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "安装异常：应用代码未解压完整（缺 app\scripts\desktop_launch.py）。"
    ${EndIf}
    Abort
  ${EndIf}

  ; CLI 全局命令：shim 放在安装根目录（PATH 加入的是这一层）
  SetOutPath "$INSTDIR"
  File "triposr.cmd"
  Call AddToUserPath

  ; 用户数据目录（模型/结果/日志）安装期创建，覆盖安装保留
  CreateDirectory "${USER_DATA_DIR}"
  CreateDirectory "${USER_DATA_DIR}\models"
  CreateDirectory "${USER_DATA_DIR}\models\triposr"
  CreateDirectory "${USER_DATA_DIR}\results"
  CreateDirectory "${USER_DATA_DIR}\logs"

  ; 快捷方式（桌面 + 开始菜单固定目录）
  CreateDirectory "$SMPROGRAMS\${APP_DISPLAY_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_DISPLAY_NAME}\${APP_DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortcut "$DESKTOP\${APP_DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}"
  ; CLI 快捷方式：打开命令行窗口显示帮助
  CreateShortcut "$SMPROGRAMS\${APP_DISPLAY_NAME}\TripoSR-Lite CLI.lnk" "$SYSDIR\cmd.exe" '/k "$INSTDIR\triposr.cmd" --help'

  ; HKCU 注册
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "ReSerendipity"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" ${EST_KB}

  WriteUninstaller "$INSTDIR\uninstall.exe"
  RMDir /r "$TOOLSDIR"
SectionEnd

; ---------- 版本信息 ----------
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_DISPLAY_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "FileDescription" "${APP_DISPLAY_NAME} 安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "ReSerendipity"
VIAddVersionKey "LegalCopyright" "Copyright (C) 2026 ReSerendipity"

; ---------- 卸载 ----------
Section "Uninstall"
  !insertmacro KillRunning

  Delete "$DESKTOP\${APP_DISPLAY_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_DISPLAY_NAME}\${APP_DISPLAY_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_DISPLAY_NAME}\TripoSR-Lite CLI.lnk"
  RMDir "$SMPROGRAMS\${APP_DISPLAY_NAME}"

  ; CLI 全局命令：删注册表键前先从当前用户 PATH 精确移除安装目录
  Call un.RemoveFromUserPath
  DeleteRegKey HKCU "${APP_REG_KEY}"
  DeleteRegKey HKCU "${UNINST_KEY}"

  ; 保留用户数据：静默卸载默认保留（/SD IDYES），交互卸载默认答「是」
  StrCpy $KEEP_DATA "1"
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON1 \
    "是否保留用户数据（模型权重、推理结果与日志）？$\r$\n选择「是」保留，选择「否」一并删除 ${USER_DATA_DIR}。" \
    /SD IDYES IDYES triposr_keep_data
  StrCpy $KEEP_DATA "0"
  triposr_keep_data:

  ; 只删程序：app/ 代码与两套 runtime、壳 exe、卸载器
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\triposr.cmd"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r "$INSTDIR"
  StrCpy $R0 0
  ${DoWhile} ${FileExists} "$INSTDIR\${APP_EXE}"
    IntOp $R0 $R0 + 1
    ${If} $R0 >= 15
      ${Break}
    ${EndIf}
    Sleep 1000
    Delete "$INSTDIR\${APP_EXE}"
  ${Loop}

  ${If} $KEEP_DATA == "0"
    DetailPrint "按用户选择删除用户数据：${USER_DATA_DIR}"
    RMDir /r "${USER_DATA_DIR}"
  ${Else}
    DetailPrint "已保留用户数据：${USER_DATA_DIR}"
  ${EndIf}

  ; 兜底：卸载器退出后延迟清理仍被句柄占用的安装目录
  nsExec::Exec 'cmd /c ping -n 4 127.0.0.1 >nul & rd /s /q "$INSTDIR"'
  Pop $0
SectionEnd
