# 🪟 Convertir APK Factory Pro en logiciel Windows (.exe)

## Structure à mettre en place

```
apk-factory/          ← ta racine existante
├── server.py
├── builder.html
├── launcher.bat
├── tools/
│   ├── android-sdk/
│   └── icon.ico      ← AJOUTE une icône ici (sinon icône Electron par défaut)
├── output/
├── workspace/
│
└── electron_app/     ← NOUVEAU dossier (contenu de ce zip)
    ├── main.js
    ├── preload.js
    ├── package.json
    └── build_windows.bat
```

---

## Prérequis

| Outil | Version | Lien |
|-------|---------|------|
| **Node.js** | 18+ (LTS) | https://nodejs.org |
| **Python** | 3.8+ | https://python.org |
| **Java JDK** | 17+ | https://adoptium.net |

---

## Étapes

### 1. Placer le dossier `electron_app`
Extrait ce zip et place le dossier `electron_app` à la racine de ton projet
(au même niveau que `server.py`).

### 2. (Optionnel) Ajouter une icône
Place un fichier `icon.ico` dans le dossier `tools/` de ton projet.
- Taille recommandée : 256×256 px
- Convertisseur en ligne : https://convertio.co/fr/png-ico/

### 3. Lancer le build
Double-clique sur `electron_app\build_windows.bat`

Le script :
- Installe automatiquement Electron + electron-builder via npm
- Compile l'application
- Génère les fichiers `.exe` dans `dist_electron\`

### 4. Récupérer les fichiers générés
Dans `dist_electron\` tu trouveras :
- **`APK Factory Pro Setup 3.0.0.exe`** — installateur Windows (recommandé pour distribution)
- **`APK Factory Pro 3.0.0.exe`** — version portable (aucune installation requise)

---

## Comment ça marche ?

```
[Electron]
    │
    ├─ Ouvre une fenêtre native Windows
    │
    └─ Lance Python server.py en arrière-plan
            │
            └─ Écoute sur http://localhost:7842
                    │
                    └─ L'interface s'affiche dans la fenêtre Electron
```

L'utilisateur final **n'a pas besoin** d'installer Python ou Java séparément —
tout est embarqué dans l'installateur (les outils Android SDK sont dans `tools/`).

⚠️ **Exception** : Python doit être installé sur la machine **de l'utilisateur final**
car il n'est pas embarqué par défaut. Pour un vrai déploiement "zéro prérequis",
voir la section avancée ci-dessous.

---

## Version "zéro prérequis" (avancée)

Pour embarquer Python dans l'exe (aucune installation côté utilisateur) :

1. Télécharge **Python Embeddable Package** (Windows x64) :
   https://www.python.org/downloads/windows/
   → Cherche "Windows embeddable package (64-bit)"

2. Extrait-le dans `electron_app\python\`

3. Dans `main.js`, la ligne `findPython()` détectera automatiquement
   `resources/python/python.exe` en mode packagé.

4. Installe les dépendances Python dans ce dossier embarqué :
   ```
   python\python.exe -m pip install flask --target=python\Lib\site-packages
   ```

---

## Dépannage

| Problème | Solution |
|----------|----------|
| `node` introuvable | Installe Node.js et coche "Add to PATH" |
| Fenêtre blanche | Python prend du temps à démarrer, attends 5-10 sec |
| Erreur NSIS | Installe [NSIS](https://nsis.sourceforge.io/) ou utilise `build:portable` |
| Python introuvable au lancement | Installe Python et coche "Add to PATH" |
| Antivirus bloque le .exe | Normal pour les apps non signées — ajoute une exception |
