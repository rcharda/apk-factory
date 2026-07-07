@echo off
setlocal enabledelayedexpansion
title APK Factory Pro - Compilation .exe

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "NODE_DIR=%ROOT%\tools\node"

if exist "%ROOT%\setenv.bat" call "%ROOT%\setenv.bat"
if exist "%NODE_DIR%\node.exe" set "PATH=%NODE_DIR%;%PATH%"

echo.
echo ============================================================
echo   APK Factory Pro - Compilation installateur Windows
echo ============================================================
echo.

where node >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERREUR] Node.js introuvable. Lance INSTALL.bat d abord.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('node -v') do echo [OK] Node.js %%v

if not exist "%ROOT%\node_modules" (
    echo [INFO] node_modules absent - npm install...
    cd /d "%ROOT%"
    call npm install
    if %errorlevel% neq 0 ( echo [ERREUR] npm install a echoue. & pause & exit /b 1 )
)

if not exist "%ROOT%\server.py" (
    echo [ERREUR] server.py introuvable. Ce script doit etre a la racine du projet.
    pause & exit /b 1
)

if not exist "%ROOT%\builder.html" (
    echo [ERREUR] builder.html introuvable a la racine du projet.
    pause & exit /b 1
)

mkdir "%ROOT%\output" 2>nul
mkdir "%ROOT%\workspace" 2>nul

if not exist "%ROOT%\assets\icon.ico" echo [INFO] Pas d icone (assets\icon.ico) - icone par defaut.

echo.
echo [1/2] Compilation en cours (3-5 min au premier lancement)...
echo.

cd /d "%ROOT%"
call npm run build
if %errorlevel% neq 0 (
    echo.
    echo [ERREUR] Compilation echouee.
    echo   Essaie la version portable : npm run build:portable
    echo.
    pause & exit /b 1
)

echo.
echo [2/2] Succes !
echo.
echo Fichiers generes dans : %ROOT%\dist\
echo.
for %%F in ("%ROOT%\dist\*.exe") do echo   %%~nxF
echo.
pause
