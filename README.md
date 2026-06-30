# APK Factory v2 📦🛠

Générateur d'APK WebView — **Mode Simple** (comme avant) + **Mode Dev** (accès total : smali, fichiers, signature personnalisée, site complet).

## Démarrage rapide

1. **Lance `launcher.bat`** (Windows) — il vérifie/installe tout (une seule fois)
2. Le navigateur s'ouvre sur `http://localhost:7842`
3. Choisis **⚡ Simple** ou **🛠 Dev** en haut à droite

## Ce qui a changé vs v1

| | v1 | v2 |
|---|---|---|
| Modification de l'APK | Champs prédéfinis seulement (package, nom, URL, icône, permissions cochées) | + **navigateur de fichiers complet** : ouvre et édite n'importe quel fichier décompilé, y compris **chaque `.smali`**, `AndroidManifest.xml`, `res/`, `assets/` |
| Permissions | Liste fixe à cocher | Liste à cocher **+ champ libre** : n'importe quelle permission Android ou tierce (`com.android.vending.BILLING`, etc.) |
| Signature | Keystore debug uniquement | Debug **ou keystore de production** (upload .jks/.keystore + alias + mots de passe) |
| Contenu WebView | URL simple ou un seul fichier HTML collé | + **upload d'un site complet en .zip** (html/css/js/images), extrait dans `assets/www/` |
| Téléchargement des outils | apktool seulement, zipalign/apksigner = manuel | **SDK Android officiel installé automatiquement une seule fois** (zipalign + apksigner Google, via `sdkmanager`). Les lancements suivants vérifient et passent directement au serveur. |
| Session de build | Le dossier décompilé est supprimé après chaque build | **Sessions persistantes** : tu peux décompiler, éditer (même le smali), recompiler et re-signer en boucle sans repartir de zéro |

## Mode Simple

Identique à avant, avec 3 ajouts visibles :
- Onglet **🗂 Site complet (zip)** en plus de URL / HTML
- Panneau **🔏 Signature** (debug ou ton propre keystore)
- Case **"Garder la session ouverte en Mode Dev après le build"** → après génération, un bouton te bascule directement en Mode Dev sur la session qui vient d'être créée, pour aller éditer un smali précis sans recommencer.

## Mode Dev

1. **Décompiler** un template (upload ou `template.apk` local) → crée une session persistante
2. **Arborescence** à gauche : navigue dans tous les dossiers de l'APK décompilé (`smali`, `smali_classes2`, `res`, `assets`, `AndroidManifest.xml`...)
3. Clique un fichier → s'ouvre dans l'**éditeur** (texte pour XML/smali/JSON/etc., aperçu image pour les PNG/JPG). Modifie, clique **💾 Enregistrer**.
4. Panneau **⚙️ Appliquer config rapide** : applique en une fois le renommage de package, le manifest, les permissions et l'URL/HTML/site — exactement comme le Mode Simple, mais sur la session que tu es en train d'éditer à la main.
5. **🔨 Recompiler cette session** : reconstruit l'APK avec tout ce que tu as changé (config auto + edits manuels de smali), zipaligne, signe (debug ou ton keystore), et te donne le lien de téléchargement.
6. Les sessions restent listées (panneau **💾 Sessions**) — tu peux reprendre ou supprimer une session à tout moment.

⚠️ Si la recompilation échoue après une édition manuelle de smali, regarde les logs : c'est presque toujours une erreur de syntaxe smali (registre invalide, instruction mal formée). apktool indique la ligne en faute.

## 🩺 Indicateur de santé & journal de bugs

En haut à droite, à côté du statut de build, un indicateur coloré résume l'état global de l'outil :
- 🟢 **Vert** — tout fonctionne, rien à signaler.
- 🟡 **Jaune** — au moins un avertissement actif (ex. un outil comme apktool/zipalign/apksigner est introuvable, ou une URL vide a déclenché un fallback). Ça n'empêche pas forcément de continuer, mais regarde le détail.
- 🔴 **Rouge** — au moins une erreur active (échec de build, échec de signature...). Le texte à côté du point explique toujours **pourquoi**, jamais juste une couleur seule.

Clique sur l'indicateur pour ouvrir le panneau complet :
- **Onglet Journal** — historique de toutes les erreurs/avertissements détectés automatiquement pendant les builds, **et** des bugs que tu as signalés toi-même. Chaque entrée peut être marquée "résolue" ou supprimée.
- **Onglet Signaler un bug** — un mini bloc-notes : choisis la gravité (🔴 bloquant / 🟡 gênant / ℹ️ remarque), décris le problème et colle les logs concernés. Ça reste enregistré sur ta machine (`tools/bug_log.json`), même après avoir fermé le navigateur.

Ce journal est indépendant de la console de build (qui elle ne garde que les logs du dernier build) — c'est la mémoire long terme des problèmes rencontrés.

## Structure du dossier

```
apk-factory/
├── launcher.bat
├── server.py
├── builder.html
├── template.apk            ← optionnel, ton APK de base
├── tools/
│   ├── apktool.jar         ← téléchargé une seule fois
│   ├── debug.keystore      ← créé une seule fois
│   ├── bug_log.json        ← journal de bugs/avertissements (indicateur santé)
│   └── android-sdk/        ← SDK officiel Google, installé une seule fois
│       ├── cmdline-tools/
│       ├── platform-tools/
│       └── build-tools/<version>/   ← contient zipalign + apksigner réels
├── workspace/
│   └── <session_id>/decompiled/    ← sessions persistantes (Mode Dev)
└── output/                  ← APKs générés
```

## Sécurité — keystore de production

- Le keystore custom et ses mots de passe ne quittent jamais ta machine : ils sont envoyés uniquement à `http://localhost:7842` (ton propre serveur Python local), jamais sur internet.
- Le fichier keystore custom uploadé est écrit temporairement dans `workspace/<session>/custom.keystore` puis **supprimé automatiquement** juste après la signature.
- Pour une vraie publication Play Store, garde une copie de ton keystore de prod en lieu sûr — ce n'est pas l'outil qui le stocke pour toi.
- **Un keystore de prod (`tools/mon.keystore` par défaut) n'est plus jamais régénéré automatiquement** : si le fichier existe déjà, l'outil le réutilise pour rester compatible avec les mises à jour des installations existantes. Une régénération n'a lieu que si tu coches explicitement "Régénérer un nouveau keystore" dans la fenêtre de signature.
- ⚠️ **`tools/ks_pass.txt` contient ton mot de passe de keystore en clair.** Ne le commit jamais dans un dépôt Git, ne le partage jamais (même avec ce projet en pièce jointe), et ne l'envoie jamais en même temps que les autres fichiers du projet. Un `.gitignore` est fourni avec ce dépôt pour l'exclure automatiquement (ainsi que `tools/*.keystore`, `workspace/` et `output/`) — vérifie qu'il est bien présent et utilisé.

## Prérequis

- **Python 3.8+** — https://python.org (cocher "Add to PATH")
- **Java JDK 17+** — https://adoptium.net (nécessaire pour apktool *et* le SDK Android)
- Tout le reste (apktool, SDK Android/zipalign/apksigner, keystore debug) est géré automatiquement par `launcher.bat`, une seule fois.

## Si le téléchargement échoue encore

`launcher.bat` essaie 3 méthodes dans l'ordre (curl normal → curl avec vérification SSL assouplie → PowerShell). Si tout échoue (réseau d'entreprise très restrictif, proxy) :
- Télécharge manuellement `apktool_2.9.3.jar` depuis https://apktool.org et place-le dans `tools\apktool.jar`
- Télécharge manuellement le SDK command-line tools depuis https://developer.android.com/studio#command-tools, extrait-le dans `tools\android-sdk\`, puis lance manuellement `sdkmanager --licenses` et `sdkmanager "build-tools;34.0.0"`
