@echo off
setlocal EnableDelayedExpansion
title APK Factory v2
color 0A

echo.
echo  ====================================
echo     APK Factory v2 - Launcher
echo  ====================================
echo.

cd /d "%~dp0"

:: ── Python ──────────────────────────────────────────────────
set PYTHON=
where python >nul 2>&1 && set PYTHON=python
if "!PYTHON!"=="" where python3 >nul 2>&1 && set PYTHON=python3
if "!PYTHON!"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if "!PYTHON!"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if "!PYTHON!"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set PYTHON=%LOCALAPPDATA%\Programs\Python\Python310\python.exe
if "!PYTHON!"=="" if exist "C:\Python312\python.exe" set PYTHON=C:\Python312\python.exe
if "!PYTHON!"=="" if exist "C:\Python311\python.exe" set PYTHON=C:\Python311\python.exe
if "!PYTHON!"=="" (
    echo [ERREUR] Python introuvable!
    echo Installe Python 3.10+ depuis https://python.org ^(coche Add to PATH^)
    pause
    exit /b 1
)
echo [OK] Python: !PYTHON!

:: ── Pillow ───────────────────────────────────────────────────
!PYTHON! -c "import PIL" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installation Pillow...
    !PYTHON! -m pip install --quiet Pillow >nul 2>&1
    !PYTHON! -c "import PIL" >nul 2>&1
    if errorlevel 1 (echo [WARN] Pillow indisponible) else (echo [OK] Pillow installe)
) else (
    echo [OK] Pillow present
)

:: ── Java ─────────────────────────────────────────────────────
set JAVA_OK=0
java -version >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=3" %%v in ('java -version 2^>^&1 ^| findstr /i "version"') do set JAVA_VER=%%v
    echo !JAVA_VER! | findstr /r "^\"1\." >nul 2>&1
    if not errorlevel 1 (
        echo [WARN] Java 8 detecte - apktool requiert Java 11+
    ) else (
        echo [OK] Java: !JAVA_VER!
        set JAVA_OK=1
    )
) else (
    echo [WARN] Java absent - apktool ne fonctionnera pas
)

:: ── Dossiers ─────────────────────────────────────────────────
if not exist "tools"             mkdir tools
if not exist "tools\android-sdk" mkdir tools\android-sdk
if not exist "output"            mkdir output
if not exist "workspace"         mkdir workspace
echo [OK] Dossiers prets

:: ── APKTool ──────────────────────────────────────────────────
if exist "tools\apktool.jar" (
    echo [OK] apktool.jar present
) else (
    echo [INFO] Telechargement apktool...
    call :download "https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar" "tools\apktool.jar"
    if exist "tools\apktool.jar" (
        echo [OK] apktool telecharge
    ) else (
        echo [ECHEC] apktool - telechargez depuis https://apktool.org et placez dans tools\apktool.jar
    )
)

:: ── Keystore debug ───────────────────────────────────────────
if exist "tools\debug.keystore" (
    echo [OK] debug.keystore present
) else (
    call :find_keytool
    if "!KEYTOOL!"=="" (
        echo [WARN] keytool introuvable
    ) else (
        "!KEYTOOL!" -genkey -v -keystore "tools\debug.keystore" -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 -storepass android -keypass android -dname "CN=Android Debug,O=Android,C=US" >nul 2>&1
        if exist "tools\debug.keystore" (echo [OK] debug.keystore cree) else (echo [WARN] creation keystore echouee)
    )
)

:: ── SDK Android ──────────────────────────────────────────────
call :check_bt
if "!BT_FOUND!"=="1" (
    echo [OK] SDK Android present ^(zipalign + apksigner^)
) else (
    if "!JAVA_OK!"=="1" (
        echo [INFO] Installation SDK Android...
        call :install_sdk
    ) else (
        echo [SKIP] Java absent - SDK Android ignore
    )
)

:: ── Bilan ────────────────────────────────────────────────────
echo.
echo  ---- Bilan ------------------------------------------
if exist "tools\apktool.jar"    (echo  [OK] apktool.jar) else (echo  [XX] apktool.jar MANQUANT)
if exist "tools\debug.keystore" (echo  [OK] debug.keystore) else (echo  [--] debug.keystore absent)
call :check_bt
if "!BT_FOUND!"=="1" (echo  [OK] zipalign + apksigner) else (echo  [XX] zipalign/apksigner MANQUANTS)
echo  -----------------------------------------------------

if not exist "template.apk" (
    echo.
    echo [INFO] Pas de template.apk - upload depuis l'interface.
) else (
    echo [OK] template.apk present
)

:: ── Lancer serveur ───────────────────────────────────────────
echo.
echo [START] http://127.0.0.1:7842
echo.
start "" cmd /c "timeout /t 3 /nobreak >nul & explorer http://127.0.0.1:7842"
!PYTHON! server.py
echo.
echo Serveur arrete.
pause
exit /b 0


:: =====================================================================
:: SOUS-ROUTINES
:: =====================================================================

:find_keytool
set KEYTOOL=
where keytool >nul 2>&1 && set KEYTOOL=keytool && exit /b 0
if defined JAVA_HOME (
    if exist "%JAVA_HOME%\bin\keytool.exe" set KEYTOOL=%JAVA_HOME%\bin\keytool.exe && exit /b 0
)
for /d %%J in (
    "%ProgramFiles%\Java\jdk*"
    "%ProgramFiles%\Eclipse Adoptium\jdk*"
    "%ProgramFiles%\Microsoft\jdk*"
    "%ProgramFiles%\BellSoft\LibericaJDK*"
) do (
    if exist "%%J\bin\keytool.exe" set KEYTOOL=%%J\bin\keytool.exe
)
exit /b 0

:check_bt
set BT_FOUND=0
set "BT_DIR=%~dp0tools\android-sdk\build-tools"
if not exist "!BT_DIR!" exit /b 0
for /f "delims=" %%D in ('dir /b /ad "!BT_DIR!" 2^>nul') do (
    if exist "!BT_DIR!\%%D\zipalign.exe" set BT_FOUND=1
)
exit /b 0

:install_sdk
set "SDK_ROOT=%~dp0tools\android-sdk"
set "SDKMGR_A=%~dp0tools\android-sdk\cmdline-tools\cmdline-tools\bin\sdkmanager.bat"
set "SDKMGR_B=%~dp0tools\android-sdk\cmdline-tools\bin\sdkmanager.bat"
if exist "!SDKMGR_A!" goto :run_sdk
if exist "!SDKMGR_B!" set "SDKMGR_A=!SDKMGR_B!" && goto :run_sdk
set "CLZIP=%~dp0tools\cmdline-tools.zip"
echo [INFO] Telechargement SDK Android command-line tools...
call :download "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip" "!CLZIP!"
if not exist "!CLZIP!" (
    echo [ECHEC] Telechargement SDK echoue
    exit /b 1
)
echo [INFO] Extraction SDK...
powershell -NoProfile -Command "Expand-Archive -Path '!CLZIP!' -DestinationPath '!SDK_ROOT!\cmdline-tools' -Force" >nul 2>&1
del /q "!CLZIP!" >nul 2>&1
if exist "!SDKMGR_A!" goto :run_sdk
if exist "!SDKMGR_B!" set "SDKMGR_A=!SDKMGR_B!" && goto :run_sdk
echo [ECHEC] sdkmanager introuvable apres extraction
exit /b 1
:run_sdk
echo [INFO] Installation build-tools 34.0.0...
(echo y & echo y & echo y & echo y & echo y & echo y & echo y) | call "!SDKMGR_A!" "--sdk_root=!SDK_ROOT!" --licenses >nul 2>&1
call "!SDKMGR_A!" "--sdk_root=!SDK_ROOT!" "build-tools;34.0.0"
echo [OK] SDK Android installe
exit /b 0

:download
set "_URL=%~1"
set "_OUT=%~2"
curl -L --fail --progress-bar "%_URL%" -o "%_OUT%" 2>nul
if exist "%_OUT%" for %%S in ("%_OUT%") do if %%~zS GTR 1000 exit /b 0
echo [INFO] Tentative SSL assouplie...
curl -L --fail --ssl-no-revoke --progress-bar "%_URL%" -o "%_OUT%" 2>nul
if exist "%_OUT%" for %%S in ("%_OUT%") do if %%~zS GTR 1000 exit /b 0
echo [INFO] Tentative PowerShell...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;try{Invoke-WebRequest -Uri '%_URL%' -OutFile '%_OUT%' -UseBasicParsing}catch{exit 1}" 2>nul
if exist "%_OUT%" for %%S in ("%_OUT%") do if %%~zS GTR 1000 exit /b 0
echo [INFO] Tentative bitsadmin...
bitsadmin /transfer "APKDl" /download /priority normal "%_URL%" "%_OUT%" >nul 2>&1
if exist "%_OUT%" for %%S in ("%_OUT%") do if %%~zS GTR 1000 exit /b 0
if exist "%_OUT%" del /q "%_OUT%" >nul 2>&1
exit /b 1
