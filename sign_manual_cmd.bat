@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title APK Factory - Signature manuelle
color 0A
cd /d "C:\Users\elecp\Downloads\files (4)"
echo.
echo  ========================================================
echo    APK Factory - Signature manuelle
echo  ========================================================
echo.
echo  APK a signer  : C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed.apk
echo  APK aligne    : C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_aligned.apk
echo  APK signe out : C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_signed.apk
echo.
echo  ── ETAPE 1 (si pas encore de keystore) ──────────────────
echo  Creer un keystore de production :
echo.
echo    "keytool" -genkey -v -keystore "C:\Users\elecp\Downloads\files (4)\tools\mon.keystore" -alias monapp -keyalg RSA -keysize 2048 -validity 10000
echo.
echo  ── ETAPE 2 : Zipalign ───────────────────────────────────
echo.
echo    "C:\Users\elecp\Downloads\files (4)\tools\android-sdk\build-tools\34.0.0\zipalign.exe" -f -v 4 "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed.apk" "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_aligned.apk"
echo.
echo  ── ETAPE 3 : Signer avec apksigner ─────────────────────
echo.
echo    "C:\Users\elecp\Downloads\files (4)\tools\android-sdk\build-tools\34.0.0\apksigner.bat" sign --ks "C:\Users\elecp\Downloads\files (4)\tools\mon.keystore" --ks-key-alias monapp --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true --out "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_signed.apk" "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_aligned.apk"
echo.
echo  ── ETAPE 4 : Verifier la signature ──────────────────────
echo.
echo    "C:\Users\elecp\Downloads\files (4)\tools\android-sdk\build-tools\34.0.0\apksigner.bat" verify --verbose "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_signed.apk"
echo.
echo  ── (optionnel) Signer directement sans zipalign ─────────
echo.
echo    "C:\Users\elecp\Downloads\files (4)\tools\android-sdk\build-tools\34.0.0\apksigner.bat" sign --ks "C:\Users\elecp\Downloads\files (4)\tools\mon.keystore" --ks-key-alias monapp --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true --out "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed_signed.apk" "C:\Users\elecp\Downloads\files (4)\output\MonApp_1.0_signed.apk"
echo.
echo  ========================================================
echo  COPIE-COLLE les commandes ci-dessus dans ce terminal.
echo  ========================================================
echo.
cmd /k