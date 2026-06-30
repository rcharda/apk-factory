@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title APK Factory - Signature automatique
color 0A
cd /d "%~dp0"

:: ============================================================
:: CONFIG - ton keystore de production
:: ============================================================
set KEYSTORE=tools\mon.keystore
set ALIAS=monapp

:: ============================================================
:: USAGE : sign_release.bat "chemin\vers\app.apk" [nom_sortie.apk]
:: ============================================================
if "%~1"=="" (
    echo Usage: sign_release.bat "chemin\vers\app_a_signer.apk" [nom_sortie.apk]
    echo Exemple: sign_release.bat output\MonApp_1.0.apk
    echo.
    pause
    exit /b 1
)

set "SRC=%~1"
if not exist "%SRC%" (
    echo [ERREUR] Fichier introuvable: %SRC%
    pause
    exit /b 1
)

if "%~2"=="" (
    set "OUT=%~dpn1_release.apk"
) else (
    set "OUT=%~2"
)
set "ALIGNED=%~dpn1_aligned_tmp.apk"

:: ── Mot de passe du keystore ──────────────────────────────────────────────
:: Ordre de priorité (du plus pratique au plus sûr) :
::   1) variable d'environnement KS_PASS déjà définie (ex: via "set KS_PASS=..." dans CETTE session cmd)
::   2) fichier tools\ks_pass.txt (UNE ligne = le mot de passe) — NE JAMAIS partager ce fichier/dossier
::   3) saisie manuelle au clavier (mot de passe non affiché)
::
:: BUG-M03 — avec EnableDelayedExpansion, les "!" dans le mot de passe sont
:: interprétés comme délimiteurs de variable différée → mot de passe tronqué /
:: erreur "keystore password was incorrect". On désactive DelayedExpansion le
:: temps de lire le mot de passe, puis on le réactive.
if "%KS_PASS%"=="" if exist "tools\ks_pass.txt" (
    setlocal DisableDelayedExpansion
    for /f "usebackq delims=" %%P in ("tools\ks_pass.txt") do set "KS_PASS=%%P"
    endlocal & set "KS_PASS=%KS_PASS%"
)
if "%KS_PASS%"=="" (
    echo.
    echo [INFO] Entrez le mot de passe du keystore (il ne sera pas affiché)
    set /p "KS_PASS=Mot de passe keystore [%ALIAS%]: "
)
if "!KS_PASS!"=="" (
    echo [ERREUR] Mot de passe vide — abandon
    pause
    exit /b 1
)

:: ── Detection automatique de la derniere version de build-tools ──
set BT=
for /f "delims=" %%D in ('dir /b /ad /o-n "tools\android-sdk\build-tools" 2^>nul') do (
    if "!BT!"=="" set "BT=tools\android-sdk\build-tools\%%D"
)
if "!BT!"=="" (
    echo [ERREUR] build-tools introuvable sous tools\android-sdk\build-tools
    echo          Relance launcher.bat pour installer le SDK Android.
    pause
    exit /b 1
)

set "ZIPALIGN=!BT!\zipalign.exe"
set "APKSIGNER=!BT!\apksigner.bat"

if not exist "!ZIPALIGN!" (
    echo [ERREUR] zipalign introuvable: !ZIPALIGN!
    pause & exit /b 1
)
if not exist "!APKSIGNER!" (
    echo [ERREUR] apksigner introuvable: !APKSIGNER!
    pause & exit /b 1
)
if not exist "%KEYSTORE%" (
    echo [ERREUR] Keystore introuvable: %KEYSTORE%
    echo          Cree-le d'abord avec keytool ou via l'interface APK Factory.
    pause & exit /b 1
)

:: ── Detection minSdkVersion (pour activer v4-signing seulement si pertinent) ──
:: v4 (APK Signature Scheme v4) accélère l'installation sur Android 11+ (API 30+)
:: mais n'a pas d'utilité (et peut gêner) sur des appareils plus anciens.
set "ENABLE_V4=false"

echo.
echo  ========================================================
echo     APK Factory - Signature automatique
echo  ========================================================
echo  Source  : %SRC%
echo  Sortie  : %OUT%
echo  Keystore: %KEYSTORE% (alias: %ALIAS%)
echo  ========================================================
echo.

echo [1/3] Zipalign...
if exist "%ALIGNED%" del /q "%ALIGNED%" >nul 2>&1
"!ZIPALIGN!" -f -v 4 "%SRC%" "%ALIGNED%"
if not exist "%ALIGNED%" (
    echo [ECHEC] zipalign a echoue
    pause & exit /b 1
)

echo.
echo [2/3] Signature avec apksigner...
if exist "%OUT%" del /q "%OUT%" >nul 2>&1
:: SR-01: écrire le mot de passe dans un fichier temp pour éviter l'exposition
:: dans la liste des processus Windows (tasklist /V, Process Hacker, etc.)
set "PASS_FILE=%TEMP%\ks_pass_%RANDOM%.tmp"
echo !KS_PASS!> "!PASS_FILE!"
:: apksigner.bat est un script .bat — DOIT être appelé avec "call", sinon
:: cmd.exe transfère le contrôle de façon permanente au sous-script et ce
:: script ne reprend jamais la main après (les lignes suivantes ne
:: s'exécutent jamais, même en cas de succès de la signature).
call "!APKSIGNER!" sign --ks "%KEYSTORE%" --ks-pass "file:!PASS_FILE!" ^
    --ks-key-alias %ALIAS% ^
    --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true ^
    --v4-signing-enabled !ENABLE_V4! ^
    --out "%OUT%" "%ALIGNED%"
del /q "!PASS_FILE!" >nul 2>&1
if not exist "%OUT%" (
    echo [ECHEC] signature a echoue - verifiez le mot de passe et le keystore
    pause & exit /b 1
)

del /q "%ALIGNED%" >nul 2>&1

echo.
echo [3/3] Verification...
call "!APKSIGNER!" verify --verbose "%OUT%"

echo.
echo  ========================================================
echo  [OK] APK signe pret a installer : %OUT%
echo  ========================================================
echo.
pause
