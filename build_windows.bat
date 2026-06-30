@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title APK Factory Pro — Build Windows

echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║       APK Factory Pro — Compilateur Windows         ║
echo ╚══════════════════════════════════════════════════════╝
echo.

:: ─── Vérification Node.js ─────────────────────────────────────────────────────
where node >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Node.js n'est pas installé ou pas dans le PATH.
    echo  Télécharge-le sur : https://nodejs.org  (version LTS recommandée)
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node -v') do set NODE_VER=%%v
echo [OK] Node.js détecté : %NODE_VER%

:: ─── Vérification npm ─────────────────────────────────────────────────────────
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] npm introuvable.
    pause
    exit /b 1
)
echo [OK] npm détecté.

:: ─── Vérification Python ──────────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installé ou pas dans le PATH.
    echo  Télécharge-le sur : https://python.org (cocher "Add to PATH")
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do set PY_VER=%%v
echo [OK] Python détecté : %PY_VER%

:: ─── Structure : placer les fichiers Electron dans un sous-dossier ────────────
set ROOT=%~dp0
set ELECTRON_DIR=%ROOT%electron_app

if not exist "%ELECTRON_DIR%" (
    echo [ERREUR] Le dossier electron_app est introuvable.
    echo  Place ce script à la racine du projet (à côté de server.py).
    pause
    exit /b 1
)

echo.
echo [1/4] Installation des dépendances Node.js...
cd /d "%ELECTRON_DIR%"
call npm install
if errorlevel 1 (
    echo [ERREUR] npm install a échoué.
    pause
    exit /b 1
)
echo [OK] Dépendances installées.

:: ─── Icône par défaut si absente ──────────────────────────────────────────────
if not exist "%ROOT%tools\icon.ico" (
    echo [INFO] Aucune icône trouvée dans tools\icon.ico
    echo  L'application utilisera l'icône Electron par défaut.
    echo  Pour personnaliser : place ton icon.ico dans le dossier tools\
)

:: ─── Dossiers requis ──────────────────────────────────────────────────────────
if not exist "%ROOT%output" mkdir "%ROOT%output"
if not exist "%ROOT%workspace" mkdir "%ROOT%workspace"

echo.
echo [2/4] Vérification de server.py...
if not exist "%ROOT%server.py" (
    echo [ERREUR] server.py introuvable à la racine du projet.
    pause
    exit /b 1
)
echo [OK] server.py trouvé.

echo.
echo [3/4] Compilation Electron (installateur + portable)...
echo  Cela peut prendre 2-5 minutes selon ta connexion (téléchargement Electron).
echo.
call npm run build
if errorlevel 1 (
    echo.
    echo [ERREUR] La compilation a échoué. Lis les messages d'erreur ci-dessus.
    pause
    exit /b 1
)

echo.
echo [4/4] Compilation terminée !
echo.
echo ══════════════════════════════════════════════════════
echo  Fichiers générés dans : %ROOT%dist_electron\
echo.
dir /b "%ROOT%dist_electron\*.exe" 2>nul
echo ══════════════════════════════════════════════════════
echo.
echo  - APK Factory Pro Setup *.exe  → installateur Windows (recommandé)
echo  - APK Factory Pro *.exe        → version portable (pas d'installation)
echo.
pause
