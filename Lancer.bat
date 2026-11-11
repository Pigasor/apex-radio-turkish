@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Apex Radio
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo.
  echo  Python n'est pas installe / Python is not installed.
  echo  https://www.python.org/downloads/  ^(coche / tick "Add python.exe to PATH"^)
  echo.
  pause
  exit /b
)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
)
if not exist ".venv\installe-v4.ok" (
  echo  Installation / Installing / Bilesenler kuruluyor, 2-5 min...
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt && echo ok> ".venv\installe-v4.ok"
)
rem Raccourci "Apex Radio" sur le bureau (une seule fois) / desktop shortcut (once)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $l=Join-Path $d 'Apex Radio.lnk'; if(-not (Test-Path $l)){ $s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='app.py'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0apex.ico'; $s.Description='Apex Radio - LMU race engineer'; $s.Save(); Write-Host '  Apex Radio shortcut created on the desktop.' }"
".venv\Scripts\python.exe" app.py %*
pause
