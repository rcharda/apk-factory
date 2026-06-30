@echo off
setlocal enabledelayedexpansion
title APK Factory Pro - Installation

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "TOOLS=%ROOT%\tools"
set "JDK_DIR=%TOOLS%\jdk"
set "PY_DIR=%TOOLS%\python"
set "NODE_DIR=%TOOLS%\node"
set "EAPP=%ROOT%\electron_app"

echo.
echo ============================================================
echo   APK Factory Pro v3 - Installation des dependances
echo   Java JDK 17 + apktool + Python 3.11 + Node.js + Electron
echo ============================================================
echo.
echo Racine du projet : %ROOT%
echo.

mkdir "%TOOLS%" 2>nul
mkdir "%JDK_DIR%" 2>nul
mkdir "%PY_DIR%" 2>nul
mkdir "%NODE_DIR%" 2>nul

:: ============================================================
:: ETAPE 1 - JAVA JDK
:: ============================================================
echo [1/5] Verification de Java...

if exist "%JDK_DIR%\bin\java.exe" (
    echo [OK] JDK portable deja present.
    goto java_ok
)
where java >nul 2>nul
if %errorlevel% == 0 (
    echo [OK] Java systeme detecte.
    goto java_ok
)

echo [INFO] Telechargement Eclipse Temurin JDK 17 (~180 Mo)...
set "JDK_ZIP=%TOOLS%\jdk17.zip"
set "JDK_URL=https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.11+9/OpenJDK17U-jdk_x64_windows_hotspot_17.0.11_9.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%JDK_URL%' -OutFile '%JDK_ZIP%' -UseBasicParsing }"
if not exist "%JDK_ZIP%" ( echo [ERREUR] Telechargement JDK echoue. & goto error_exit )

echo [INFO] Extraction JDK...
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%JDK_ZIP%' -DestinationPath '%TOOLS%\jdk_tmp' -Force }"
for /d %%D in ("%TOOLS%\jdk_tmp\*") do move "%%D" "%JDK_DIR%" >nul 2>nul
rmdir /q /s "%TOOLS%\jdk_tmp" 2>nul
del "%JDK_ZIP%" 2>nul
if not exist "%JDK_DIR%\bin\java.exe" ( echo [ERREUR] Extraction JDK echouee. & goto error_exit )
echo [OK] JDK 17 installe dans tools\jdk\

:java_ok
echo.

:: ============================================================
:: ETAPE 2 - APKTOOL
:: ============================================================
echo [2/5] Verification d apktool...

if exist "%TOOLS%\apktool.jar" (
    echo [OK] apktool.jar deja present.
    goto apktool_bat
)

echo [INFO] Telechargement apktool 2.9.3 (~10 Mo)...
set "APKTOOL_URL=https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%APKTOOL_URL%' -OutFile '%TOOLS%\apktool.jar' -UseBasicParsing }"
if not exist "%TOOLS%\apktool.jar" ( echo [ERREUR] Telechargement apktool echoue. & goto error_exit )
echo [OK] apktool.jar telecharge.

:apktool_bat
echo @echo off > "%TOOLS%\apktool.bat"
if exist "%JDK_DIR%\bin\java.exe" (
    echo set "PATH=%JDK_DIR%\bin;%%PATH%%" >> "%TOOLS%\apktool.bat"
)
echo java -jar "%TOOLS%\apktool.jar" %%* >> "%TOOLS%\apktool.bat"
echo [OK] apktool.bat cree.
echo.

:: ============================================================
:: ETAPE 3 - PYTHON
:: ============================================================
echo [3/5] Verification de Python...

if exist "%PY_DIR%\python.exe" (
    echo [OK] Python portable deja present.
    goto python_ok
)
where python >nul 2>nul
if %errorlevel% == 0 ( echo [OK] Python systeme detecte. & goto python_ok )

echo [INFO] Telechargement Python 3.11.9 embeddable (~10 Mo)...
set "PY_ZIP=%TOOLS%\py_embed.zip"
set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_ZIP%' -UseBasicParsing }"
if not exist "%PY_ZIP%" ( echo [ERREUR] Telechargement Python echoue. & goto error_exit )

echo [INFO] Extraction Python...
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%PY_ZIP%' -DestinationPath '%PY_DIR%' -Force }"
del "%PY_ZIP%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem '%PY_DIR%' -Filter '*._pth' | ForEach-Object { (Get-Content $_.FullName) -replace '#import site','import site' | Set-Content $_.FullName }"
if not exist "%PY_DIR%\python.exe" ( echo [ERREUR] Extraction Python echouee. & goto error_exit )
echo [OK] Python 3.11 installe dans tools\python\

:python_ok
echo.

:: ============================================================
:: ETAPE 4 - NODE.JS PORTABLE
:: ============================================================
echo [4/5] Verification de Node.js...

if exist "%NODE_DIR%\node.exe" (
    set "PATH=%NODE_DIR%;%PATH%"
    echo [OK] Node.js portable deja present.
    goto node_ok
)
where node >nul 2>nul
if %errorlevel% == 0 (
    echo [OK] Node.js systeme detecte.
    goto node_ok
)

echo [INFO] Telechargement Node.js 20 LTS portable (~30 Mo)...
set "NODE_ZIP=%TOOLS%\node.zip"
set "NODE_URL=https://nodejs.org/dist/v20.18.0/node-v20.18.0-win-x64.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%NODE_URL%' -OutFile '%NODE_ZIP%' -UseBasicParsing }"
if not exist "%NODE_ZIP%" ( echo [ERREUR] Telechargement Node.js echoue. & goto error_exit )

echo [INFO] Extraction Node.js...
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%NODE_ZIP%' -DestinationPath '%TOOLS%\node_tmp' -Force }"
for /d %%D in ("%TOOLS%\node_tmp\*") do move "%%D" "%NODE_DIR%" >nul 2>nul
rmdir /q /s "%TOOLS%\node_tmp" 2>nul
del "%NODE_ZIP%" 2>nul
if not exist "%NODE_DIR%\node.exe" ( echo [ERREUR] Extraction Node.js echouee. & goto error_exit )

set "PATH=%NODE_DIR%;%PATH%"
echo [OK] Node.js 20 installe dans tools\node\

:node_ok
for /f "tokens=*" %%v in ('node -v 2^>nul') do echo [OK] Node.js version : %%v
echo.

:: ============================================================
:: ETAPE 5 - ELECTRON (npm install)
:: ============================================================
echo [5/5] Installation Electron...

cd /d "%EAPP%"
echo [INFO] npm install en cours (premiere fois : ~5 min)...
call npm install
if %errorlevel% neq 0 (
    echo [ERREUR] npm install a echoue.
    goto error_exit
)
echo [OK] Electron installe.

:: ============================================================
:: GENERER setenv.bat
:: ============================================================
echo @echo off > "%ROOT%\setenv.bat"
echo set "ROOT=%ROOT%" >> "%ROOT%\setenv.bat"
echo set "TOOLS=%TOOLS%" >> "%ROOT%\setenv.bat"
if exist "%JDK_DIR%\bin\java.exe" (
    echo set "JAVA_HOME=%JDK_DIR%" >> "%ROOT%\setenv.bat"
    echo set "PATH=%JDK_DIR%\bin;%%PATH%%" >> "%ROOT%\setenv.bat"
)
if exist "%NODE_DIR%\node.exe" (
    echo set "PATH=%NODE_DIR%;%%PATH%%" >> "%ROOT%\setenv.bat"
)
if exist "%PY_DIR%\python.exe" (
    echo set "PYTHON=%PY_DIR%\python.exe" >> "%ROOT%\setenv.bat"
) else (
    echo set "PYTHON=python" >> "%ROOT%\setenv.bat"
)

echo.
echo ============================================================
echo   INSTALLATION TERMINEE - BILAN
echo ============================================================
echo.
where java >nul 2>nul
if %errorlevel% == 0 ( echo [OK] Java          : detecte ) else if exist "%JDK_DIR%\bin\java.exe" ( echo [OK] Java JDK 17  : tools\jdk\ )
if exist "%TOOLS%\apktool.jar"   echo [OK] apktool      : tools\apktool.jar
where python >nul 2>nul
if %errorlevel% == 0 ( echo [OK] Python        : detecte ) else if exist "%PY_DIR%\python.exe" ( echo [OK] Python 3.11  : tools\python\ )
where node >nul 2>nul
if %errorlevel% == 0 ( echo [OK] Node.js       : detecte ) else if exist "%NODE_DIR%\node.exe" ( echo [OK] Node.js 20   : tools\node\ )
if exist "%TOOLS%\android-sdk\build-tools\34.0.0\zipalign.exe"  echo [OK] zipalign     : OK (deja dans le projet)
if exist "%TOOLS%\android-sdk\build-tools\34.0.0\apksigner.bat" echo [OK] apksigner    : OK (deja dans le projet)
echo.
echo Prochaines etapes :
echo   1. Tester le serveur  ->  LANCER_SERVEUR.bat
echo   2. Compiler le .exe   ->  BUILD_EXE.bat
echo.
pause
exit /b 0

:error_exit
echo.
echo Installation interrompue. Relis les erreurs ci-dessus.
pause
exit /b 1
