# APK Factory Pro — Guide complet Windows

## Structure finale du projet

```
APK_Factory_Pro_v3/          ← dossier racine de ton projet existant
│
├── server.py                ✅ déjà présent
├── builder.html             ✅ déjà présent
├── tools/                   ✅ déjà présent
│   ├── android-sdk/         ✅ (zipalign, apksigner, aapt déjà là)
│   ├── jdk/                 ← sera téléchargé par INSTALL.bat
│   ├── python/              ← sera téléchargé par INSTALL.bat
│   ├── apktool.jar          ← sera téléchargé par INSTALL.bat
│   └── icon.ico             ← AJOUTE ton icône ici (optionnel)
│
├── INSTALL.bat              ← NOUVEAU (depuis ce zip)
├── LANCER_SERVEUR.bat       ← NOUVEAU (depuis ce zip)
├── BUILD_EXE.bat            ← NOUVEAU (depuis ce zip)
│
└── electron_app/            ← NOUVEAU (depuis ce zip)
    ├── main.js
    ├── preload.js
    └── package.json
```

---

## ÉTAPE 0 — Prérequis unique : Node.js

Node.js est le seul outil à installer manuellement (tout le reste est automatique).

1. Va sur https://nodejs.org
2. Télécharge la version **LTS (64-bit)**
3. Installe-le en cochant **"Add to PATH"**
4. Vérifie dans CMD : `node -v` → doit afficher v18+ ou v20+

---

## ÉTAPE 1 — Placer les fichiers

Extrais ce zip et copie ces fichiers/dossiers à la **racine de ton projet** (là où est `server.py`) :

```
INSTALL.bat          → racine/
LANCER_SERVEUR.bat   → racine/
BUILD_EXE.bat        → racine/
electron_app/        → racine/electron_app/
```

---

## ÉTAPE 2 — Installation automatique des dépendances

Double-clique sur **`INSTALL.bat`**

Le script va automatiquement :

| Outil | Action |
|-------|--------|
| Java JDK 17 (Eclipse Temurin) | Télécharge ~180 Mo → `tools/jdk/` |
| apktool 2.9.3 | Télécharge ~10 Mo → `tools/apktool.jar` |
| Python 3.11 embeddable | Télécharge ~10 Mo → `tools/python/` |
| Electron + electron-builder | `npm install` dans `electron_app/` |

**Durée** : 2-5 minutes selon ta connexion.

---

## ÉTAPE 3 — Tester le serveur

Double-clique sur **`LANCER_SERVEUR.bat`**

→ Ouvre `http://localhost:7842` dans ton navigateur

Si tout fonctionne, tu verras l'interface APK Factory.

---

## ÉTAPE 4 — Compiler l'application Windows (.exe)

Double-clique sur **`BUILD_EXE.bat`**

Fichiers générés dans `dist_electron/` :

| Fichier | Usage |
|---------|-------|
| `APK Factory Pro Setup 3.0.0.exe` | Installateur (recommandé pour distribuer) |
| `APK Factory Pro 3.0.0.exe`       | Version portable (aucune installation) |

---

## Comportement au premier lancement (client final)

```
[Client lance APK Factory Pro.exe]
          ↓
[Electron démarre → écran de chargement]
          ↓
[Recherche Python sur la machine client]
    ├── Trouvé → démarrage direct
    └── Absent → téléchargement auto Python 3.11 (~10 Mo)
              via PowerShell (intégré Windows 7+)
          ↓
[server.py démarre avec Java + outils embarqués]
          ↓
[Interface APK Factory s'affiche]
```

**Le client final n'installe rien manuellement.**
Java JDK, apktool, zipalign, apksigner sont tous embarqués dans l'exe.

---

## Dépannage

| Problème | Solution |
|----------|----------|
| `node` introuvable | Installe Node.js LTS depuis nodejs.org |
| INSTALL.bat échoue sur JDK | Vérifie la connexion ; ou télécharge JDK manuellement depuis adoptium.net |
| Fenêtre blanche au lancement | Python prend 5-10s à démarrer, attends |
| Antivirus bloque le .exe | Normal pour apps non signées → ajouter exception |
| Port 7842 déjà utilisé | Ferme l'autre instance du serveur |
| `apktool non trouvé` dans l'app | Vérifie que `tools/apktool.jar` existe |

---

## Commandes CMD utiles

```cmd
:: Tester server.py directement
tools\python\python.exe server.py

:: Tester Java embarqué
tools\jdk\bin\java.exe -version

:: Tester apktool
tools\jdk\bin\java.exe -jar tools\apktool.jar --version

:: Compiler uniquement la version portable (sans NSIS)
cd electron_app
npm run build:portable

:: Lancer en mode dev (sans compiler)
cd electron_app
npm start
```
