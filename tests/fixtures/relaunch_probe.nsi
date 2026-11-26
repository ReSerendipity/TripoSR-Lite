; L2 NSIS probe: reuse the exact /RELAUNCH detection + Exec pattern from setup.nsi.
; ASCII-only (no BOM) to avoid NSIS "Bad text encoding".
Unicode true
!include "LogicLib.nsh"
!include "StrFunc.nsh"
${Using:StrFunc} StrStr

Name "TripoSR relaunch probe"
OutFile "${OUT}"
InstallDir "$TEMP\triposr-relaunch-probe"
RequestExecutionLevel user

Section "Probe"
  FileOpen $2 "$TEMP\triposr-relaunch-probe-installed.txt" w
  FileWrite $2 "installed$\r$\n"
  FileClose $2

  ${StrStr} $0 "$CMDLINE" "/RELAUNCH"
  ${If} $0 != ""
    FileOpen $1 "$TEMP\triposr-relaunch-probe.txt" w
    FileWrite $1 "relaunched$\r$\n"
    FileClose $1
  ${EndIf}
SectionEnd

Section "Uninstall"
SectionEnd
