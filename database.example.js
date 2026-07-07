/**
 * database.example.js — MODÈLE / DOCUMENTATION du fichier de persistance
 * réel utilisé par setup.js : <ROOT>/installed-components.json
 *
 * Ce fichier n'est PAS exécuté par l'application — c'est un exemple commenté
 * qui montre la forme exacte des données que le logiciel écrit sur disque,
 * pour que rien (composants installés, composants trouvés par l'IA,
 * suggestions IA en attente, dernier check de mises à jour) ne soit perdu
 * si le client ferme puis rouvre l'app, ou change de machine en copiant
 * ce fichier.
 *
 * Emplacement réel : ROOT/installed-components.json (ROOT = dossier racine
 * de l'app, voir const ROOT dans setup.js). Lu/écrit uniquement via
 * readState()/writeState() dans setup.js — ne JAMAIS éditer ce fichier à la
 * main pendant que l'app tourne (écrasement possible au prochain writeState).
 *
 * Pour restaurer les données d'un client sur une nouvelle installation :
 * copier son installed-components.json vers le ROOT de la nouvelle
 * installation AVANT le premier lancement (ou pendant que l'app est fermée).
 */

module.exports = {

  // ── Composants "officiels" (jdk, apktool, androidSdk, gradle, nodejs,
  //    flutter, cordova, reactNativeCli, bubblewrap, jadx, bundletool,
  //    python...) : PAS stockés ici en réalité — leur statut "installé"
  //    est déduit à la volée par isInstalled(id) qui vérifie l'existence
  //    de comp.checkFile sur disque (voir COMPONENTS dans setup.js).
  //    Rien à sauvegarder pour eux : le fichier réel sur disque EST la
  //    source de vérité, cette clé n'apparaît donc jamais dans le vrai
  //    installed-components.json.

  // ── Composants dynamiques installés via une suggestion IA
  //    (installDynamicComponent). Clé = 'dyn:' + id. Conservé pour
  //    toujours tant que le dossier n'est pas supprimé manuellement.
  "dyn:ai_some_tool": {
    installedAt: "2026-07-05T10:15:00.000Z",
    source: "https://github.com/owner/some-tool/releases/download/v1.2.0/some-tool-win.zip",
    ai: true
  },

  // ── Suggestions IA PAS ENCORE installées (trouvées par
  //    search_missing_component / searchGithubReleaseAsset, affichées dans
  //    le modal Composants avec le badge « IA », en attente que le client
  //    coche la case et clique Installer). Sans cette persistance, une
  //    suggestion trouvée disparaissait au redémarrage de l'app et l'IA
  //    devait refaire la recherche GitHub à chaque nouvelle session.
  aiSuggestions: {
    "ai_some_other_tool": {
      ok: true,
      id: "ai_some_other_tool",
      label: "some-other-tool (some-other-tool-win.zip)",
      url: "https://github.com/owner/some-other-tool/releases/download/v0.9.0/some-other-tool-win.zip",
      repoUrl: "https://github.com/owner/some-other-tool",
      sizeApprox: "4.2 Mo",
      type: "zip",
      assetName: "some-other-tool-win.zip",
      foundAt: "2026-07-05T09:50:00.000Z"
    }
  },

  // ── Dernier résultat connu de checkComponentUpdates(), pour affichage
  //    immédiat dans builder.html à l'ouverture (avant même qu'un nouveau
  //    check réseau ne revienne, ou si le client est hors ligne).
  lastUpdateCheck: {
    checkedAt: "2026-07-05T09:00:00.000Z",
    results: [
      {
        id: "gradle",
        label: "Gradle",
        currentVersion: "8.9",
        latestVersion: "8.10",
        updateAvailable: true,
        releaseUrl: "https://github.com/gradle/gradle/releases"
      },
      {
        id: "apktool",
        label: "Apktool",
        currentVersion: "2.9.3",
        latestVersion: "2.9.3",
        updateAvailable: false,
        releaseUrl: "https://github.com/iBotPeaches/Apktool/releases"
      }
    ]
  }

  // ── NOTE — ce que ce fichier NE stocke PAS (vit ailleurs, à documenter
  //    séparément si besoin d'une sauvegarde complète du poste client) :
  //    - Réglages et historique de chat IA (clés API, modèle choisi,
  //      historique de conversation par session, mode autonome/pilote
  //      automatique, TTS...) : stockés dans localStorage du renderer
  //      Electron (clés apkfactory_ai_*, voir builder.html), PAS ici.
  //    - Projets eux-mêmes (sessions scratch/cordova/flutter/reactnative,
  //      fichiers du projet, smali...) : stockés dans workspace/ (exclu du
  //      .gitignore), PAS dans ce fichier.
  //    - Clé de licence / keystore de signature : ks_pass.txt et fichiers
  //      .jks, gérés séparément, jamais mélangés à ce fichier.
};
