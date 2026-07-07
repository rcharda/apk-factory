; installer.nsh — hooks NSIS personnalisés pour electron-builder
; À référencer dans package.json : "build": { "nsis": { "include": "installer.nsh" } }
;
; Ajoute une question à la désinstallation : garder ou supprimer les
; composants téléchargés (Node.js, JDK, Android SDK, react-native CLI...)
; stockés dans %APPDATA%\<nom-app>\tools, HORS du dossier d'installation.
; L'uninstaller NSIS standard ne touche déjà PAS à %APPDATA% par défaut —
; ce script sert uniquement à proposer la suppression EXPLICITE si
; l'utilisateur le souhaite, plutôt que de le faire sans lui demander.
;
; IMPORTANT : le dossier réel dans %APPDATA% N'EST PAS le productName
; ("APK Factory Pro v3") mais le champ "name" du package.json racine
; ("apk-factory-pro"), car main.js n'appelle jamais app.setName() — Electron
; utilise donc "name" par défaut pour app.getPath('userData'). ${APP_FILENAME}
; n'est PAS une variable reconnue par electron-builder : on hardcode le
; vrai nom ci-dessous. Si "name" change un jour dans package.json, il faut
; répercuter le changement ici.

!macro customUnInstall
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "Voulez-vous aussi supprimer les composants téléchargés (Node.js, JDK, SDK Android, etc.) ?$\r$\n$\r$\nSi vous choisissez Non, ils seront conservés et réutilisés automatiquement lors d'une prochaine installation (pas de retéléchargement)." \
    /SD IDNO IDYES delete_tools IDNO keep_tools

  delete_tools:
    RMDir /r "$APPDATA\apk-factory-pro\tools"
    Goto uninstall_done

  keep_tools:
    ; Ne rien faire : $APPDATA\apk-factory-pro\tools reste en place pour une future install.
    Goto uninstall_done

  uninstall_done:
!macroend
