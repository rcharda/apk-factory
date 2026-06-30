@echo off
setlocal enabledelayedexpansion
title APK Factory Pro - Serveur local

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

if exist "%ROOT%\setenv.bat" call "%ROOT%\setenv.bat"

set "PYTHON_EXE="
if exist "%ROOT%\tools\python\python.exe" (
    set "PYTHON_EXE=%ROOT%\tools\python\python.exe"
    goto found_python
)
where python >nul 2>nul
if %errorlevel% == 0 ( set "PYTHON_EXE=python" & goto found_python )
where python3 >nul 2>nul
if %errorlevel% == 0 ( set "PYTHON_EXE=python3" & goto found_python )

echo [ERREUR] Python introuvable. Lance INSTALL.bat d abord.
pause
exit /b 1

:found_python
echo [OK] Python : %PYTHON_EXE%

if exist "%ROOT%\tools\jdk\bin\java.exe" (
    set "PATH=%ROOT%\tools\jdk\bin;%PATH%"
    set "JAVA_HOME=%ROOT%\tools\jdk"
    echo [OK] Java JDK : tools\jdk\bin\java.exe
) else (
    where java >nul 2>nul
    if %errorlevel% == 0 ( echo [OK] Java systeme detecte. ) else ( echo [AVERT] Java non trouve - generation APK indisponible. )
)

if exist "%ROOT%\tools\apktool.jar" ( echo [OK] apktool.jar : OK ) else ( echo [AVERT] apktool.jar absent - lance INSTALL.bat )

echo.
echo ============================================================
echo   Serveur APK Factory demarre sur :
echo   http://localhost:7842
echo.
echo   Ouvre cette URL dans ton navigateur.
echo   Ferme cette fenetre pour arreter le serveur.
echo ============================================================
echo.

cd /d "%ROOT%"
"%PYTHON_EXE%" server.py

pause
