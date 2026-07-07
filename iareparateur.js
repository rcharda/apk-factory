/* =====================================================================
   IAREPARATEUR.JS — Réparateur automatique du système APK Factory Pro
   =====================================================================

   RÔLE : quand une opération sur un APK échoue — décompilation (apktool/
   jadx), signature (keystore/keytool/jarsigner/apksigner), ou build
   (Gradle/Android SDK) — ce module analyse le journal d'erreur produit
   par server.py/setup.js, reconnaît les pannes DÉJÀ RENCONTRÉES ET
   DOCUMENTÉES dans ce projet (voir les commentaires "BUG-xxx" dans
   server.py et setup.js), et applique lui-même la correction connue
   AVANT de redemander quoi que ce soit au client.

   Ce module tourne côté process principal Electron (comme setup.js),
   car il a besoin d'un accès disque direct (tools/, caches Gradle,
   cache framework apktool...) — pas côté renderer.

   CE QUI EST RÉELLEMENT AUTO-RÉPARABLE (actions ci-dessous) :
   - JDK manquant/corrompu (keytool/java introuvables)      → réinstalle 'jdk'
   - apktool.jar manquant/corrompu                          → réinstalle 'apktool'
   - Android SDK / build-tools manquants ou incomplets      → réinstalle 'androidSdk'
   - Gradle cache corrompu (téléchargement interrompu, etc.)→ vide ~/.gradle/caches
   - Cache framework apktool corrompu (AndrolibException)   → vide le cache framework apktool
   - debug.keystore corrompu (pas custom, pas de mdp client)→ régénéré automatiquement
   - Fichiers "phantom" (package.json fantôme, cache electron-builder) → nettoyés

   CE QUI N'EST JAMAIS AUTO-RÉPARABLE (on le signale clairement, sans
   jamais improviser une action risquée à la place) :
   - Mot de passe de keystore incorrect (custom/production)  → il faut le bon mdp
   - Keystore de PRODUCTION corrompu                          → perte de clé = pas de solution magique
   - APK/ZIP fourni par le client corrompu                    → il faut le refournir
   - Erreur de code applicatif (bug métier dans le projet)    → hors du périmètre "système"
   ===================================================================== */

const fs = require('fs');
const path = require('path');
const os = require('os');

let setupManager = null;
try { setupManager = require('./setup.js'); } catch (e) { /* environnement de test sans setup.js */ }

function log(msg) {
  console.log(`[IAReparateur] ${msg}`);
}

// -----------------------------------------------------------------------
// Utilitaires disque — jamais d'exception qui remonte : une réparation qui
// échoue ne doit jamais faire planter le diagnostic dans son ensemble.
// -----------------------------------------------------------------------
function safeRemoveDir(dirPath) {
  try {
    if (dirPath && fs.existsSync(dirPath)) {
      fs.rmSync(dirPath, { recursive: true, force: true });
      return true;
    }
  } catch (e) {
    log(`⚠ Impossible de nettoyer ${dirPath} : ${e.message}`);
  }
  return false;
}

function safeRemoveFile(filePath) {
  try {
    if (filePath && fs.existsSync(filePath)) {
      fs.unlinkSync(filePath);
      return true;
    }
  } catch (e) {
    log(`⚠ Impossible de supprimer ${filePath} : ${e.message}`);
  }
  return false;
}

// Emplacements connus du cache "framework" d'apktool (fichiers 1.apk, 2.apk...
// mis en cache après décodage d'un framework Android). Un cache corrompu
// (téléchargement interrompu, version framework incompatible) est la cause
// la plus fréquente d'AndrolibException/"Can't find framework resources"
// lors d'une décompilation qui marchait avant.
function apktoolFrameworkDirs() {
  const home = os.homedir();
  const dirs = [];
  if (process.platform === 'win32') {
    dirs.push(path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'), 'apktool', 'framework'));
  } else if (process.platform === 'darwin') {
    dirs.push(path.join(home, 'Library', 'apktool', 'framework'));
  } else {
    dirs.push(path.join(home, '.local', 'share', 'apktool', 'framework'));
  }
  // apktool respecte aussi la variable d'env APKTOOL_FRAMEWORK_DIR si définie.
  if (process.env.APKTOOL_FRAMEWORK_DIR) dirs.push(process.env.APKTOOL_FRAMEWORK_DIR);
  return dirs;
}

function gradleCacheDirs() {
  const home = os.homedir();
  const gh = process.env.GRADLE_USER_HOME || path.join(home, '.gradle');
  return [path.join(gh, 'caches'), path.join(gh, 'daemon')];
}

// -----------------------------------------------------------------------
// Table de signatures : chaque entrée reconnaît un motif d'erreur précis
// (regex sur le texte de log brut renvoyé par server.py) et sait soit le
// réparer directement, soit expliquer pourquoi ce n'est pas automatisable.
// L'ORDRE compte : la première signature qui matche déclenche sa réparation
// ; plusieurs signatures indépendantes peuvent matcher sur un même log (ex :
// JDK manquant ET cache Gradle corrompu en même temps après une coupure
// réseau) — toutes les signatures qui matchent sont traitées.
// -----------------------------------------------------------------------
function buildSignatures() {
  return [
    {
      id: 'jdk_missing',
      test: /JDK introuvable|Java non trouvé|keytool.*introuvable|installe (le composant )?['"]?jdk['"]?/i,
      diagnosis: "Le JDK (java/keytool/jarsigner) est introuvable ou incomplet sur cette machine.",
      autoFixable: true,
      repair: async (ctx, onProgress) => {
        if (!setupManager) return { fixed: false, detail: 'setupManager indisponible.' };
        onProgress?.({ status: 'repairing', message: 'Réinstallation du JDK...' });
        const r = await setupManager.updateComponent('jdk', onProgress).then(() => ({ ok: true })).catch(e => ({ ok: false, error: e.message }));
        return r.ok
          ? { fixed: true, detail: 'JDK réinstallé (Temurin 17).' }
          : { fixed: false, detail: `Échec réinstallation JDK : ${r.error}` };
      },
    },
    {
      id: 'apktool_broken',
      test: /brut\.androlib\.AndrolibException|Can'?t find framework resources|apktool\.jar.*(introuvable|corrompu)|Input file was not found or was not readable/i,
      diagnosis: "apktool a échoué à décompiler/recompiler — jar corrompu ou cache framework invalide.",
      autoFixable: true,
      repair: async (ctx, onProgress) => {
        const cleared = apktoolFrameworkDirs().map(safeRemoveDir).some(Boolean);
        let jarFixed = false;
        if (setupManager) {
          onProgress?.({ status: 'repairing', message: 'Réinstallation d\'apktool...' });
          try { await setupManager.updateComponent('apktool', onProgress); jarFixed = true; }
          catch (e) { log(`⚠ Échec réinstallation apktool: ${e.message}`); }
        }
        if (cleared || jarFixed) {
          return { fixed: true, detail: `Cache framework apktool nettoyé${jarFixed ? ' + apktool.jar réinstallé' : ''}.` };
        }
        return { fixed: false, detail: "Rien à nettoyer côté apktool (cache déjà vide) — relance quand même." };
      },
    },
    {
      id: 'sdk_broken',
      test: /aapt.*introuvable|zipalign.*introuvable|build-tools.*introuvable|platforms\/android-\d+.*introuvable|Android SDK.*(introuvable|incomplet)/i,
      diagnosis: "Le SDK Android (build-tools/platforms) est manquant ou incomplet.",
      autoFixable: true,
      repair: async (ctx, onProgress) => {
        if (!setupManager) return { fixed: false, detail: 'setupManager indisponible.' };
        onProgress?.({ status: 'repairing', message: 'Réinstallation du SDK Android...' });
        try { await setupManager.updateComponent('androidSdk', onProgress); return { fixed: true, detail: 'SDK Android réinstallé.' }; }
        catch (e) { return { fixed: false, detail: `Échec réinstallation SDK Android : ${e.message}` }; }
      },
    },
    {
      id: 'gradle_cache_corrupt',
      test: /Could not resolve|zip END header not found|Premature end of Content-Length|Gradle.*(corrompu|failed to sync)|Execution failed for task/i,
      diagnosis: "Le cache Gradle semble corrompu (téléchargement de dépendance interrompu ou cache incohérent).",
      autoFixable: true,
      repair: async (ctx, onProgress) => {
        onProgress?.({ status: 'repairing', message: 'Nettoyage du cache Gradle...' });
        const cleared = gradleCacheDirs().map(safeRemoveDir).some(Boolean);
        return cleared
          ? { fixed: true, detail: 'Cache Gradle vidé — le prochain build re-téléchargera les dépendances nécessaires (peut prendre plus de temps la 1ère fois).' }
          : { fixed: false, detail: 'Aucun cache Gradle trouvé à nettoyer.' };
      },
    },
    {
      id: 'debug_keystore_corrupt',
      test: /debug\.keystore.*(corrompu|invalid|tampered)|keystore was tampered with.*debug/i,
      diagnosis: "debug.keystore est corrompu (signature de test, pas une clé de production).",
      autoFixable: true,
      repair: async (ctx, onProgress) => {
        if (!ctx || !ctx.toolsDir) return { fixed: false, detail: 'Chemin des outils inconnu — impossible de localiser debug.keystore.' };
        const ksPath = path.join(ctx.toolsDir, 'debug.keystore');
        const removed = safeRemoveFile(ksPath);
        return removed
          ? { fixed: true, detail: 'debug.keystore corrompu supprimé — il sera régénéré automatiquement au prochain build debug.' }
          : { fixed: false, detail: 'debug.keystore introuvable (rien à supprimer) — sera créé au prochain build.' };
      },
    },
    {
      id: 'wrong_password',
      test: /mot de passe incorrect|wrong_password|keystore was tampered with|password verification failed/i,
      diagnosis: "Le mot de passe fourni ne correspond pas au keystore existant.",
      autoFixable: false,
      repair: async () => ({
        fixed: false,
        detail: "Non auto-réparable : c'est un mot de passe ou une clé de PRODUCTION — la seule action sûre est de redonner le bon mot de passe, ou de cocher « Régénérer un nouveau keystore » en sachant que les mises à jour de l'app déjà installée ne seront alors plus possibles.",
      }),
    },
    {
      id: 'corrupt_upload',
      test: /Archive corrompue|ZIP corrompu ou illisible|APK final corrompu|n'est pas un APK\/AAB valide/i,
      diagnosis: "Le fichier fourni (APK/AAB/ZIP) est corrompu ou incomplet.",
      autoFixable: false,
      repair: async () => ({
        fixed: false,
        detail: "Non auto-réparable : le fichier lui-même est illisible — il faut le re-télécharger/re-fournir depuis une source saine.",
      }),
    },
    {
      id: 'cryptography_pip_fail',
      test: /Impossible d'installer cryptography/i,
      diagnosis: "Le fallback Python 'cryptography' n'a pas pu s'installer (pas de JDK détecté ET pas d'accès pip).",
      autoFixable: true,
      repair: async (ctx, onProgress) => {
        if (!setupManager) return { fixed: false, detail: 'setupManager indisponible.' };
        onProgress?.({ status: 'repairing', message: 'Installation du JDK (pour générer le keystore via keytool, sans dépendre de pip)...' });
        try { await setupManager.updateComponent('jdk', onProgress); return { fixed: true, detail: "JDK installé — la génération de keystore utilisera désormais keytool, sans dépendre de pip/internet." }; }
        catch (e) { return { fixed: false, detail: `Échec installation JDK : ${e.message}` }; }
      },
    },
  ];
}

// =========================================================================
// RAPPORT POUR IA EXTERNE — quand IAReparateur ne peut pas (ou pas
// totalement) corriger seul, on prépare tout ce qu'il faut pour qu'un client
// puisse coller le problème dans une AUTRE IA (ChatGPT, Claude web, etc.) et
// obtenir de l'aide utile du premier coup : contexte projet, environnement,
// diagnostic déjà posé (pour ne pas lui faire redécouvrir ce qu'on sait
// déjà), et le journal d'erreur brut. Rien de tout ça ne modifie le disque.
// =========================================================================

// Longueur max du journal inclus dans le rapport — au-delà, on tronque en
// gardant le DÉBUT (contexte de la commande lancée) et surtout la FIN (le
// message d'erreur réel est presque toujours dans les dernières lignes).
const MAX_LOG_CHARS_IN_REPORT = 6000;

function truncateLogForReport(logText) {
  const s = String(logText || '');
  if (s.length <= MAX_LOG_CHARS_IN_REPORT) return { text: s, truncated: false };
  const headLen = 1500;
  const tailLen = MAX_LOG_CHARS_IN_REPORT - headLen - 120;
  const omitted = s.length - headLen - tailLen;
  return {
    text: `${s.slice(0, headLen)}\n\n[... journal tronqué (${omitted} caractères omis au milieu) ...]\n\n${s.slice(-tailLen)}`,
    truncated: true,
  };
}

// Photo instantanée de l'environnement — best-effort partout : un détail
// indisponible (ex: pas d'Electron, tests hors app) ne doit jamais faire
// planter la génération du rapport.
function buildSystemSnapshot() {
  const snap = {
    platform: process.platform,
    arch: process.arch,
    osRelease: 'inconnue',
    nodeVersion: process.version,
    packaged: null,
    toolsDir: (setupManager && setupManager.ROOT) || null,
    components: [],
  };
  try { snap.osRelease = os.release(); } catch (e) { /* ignore */ }
  try {
    const { app } = require('electron');
    snap.packaged = !!(app && app.isPackaged);
  } catch (e) { /* setupManager/iaReparateur peut tourner hors Electron (tests CLI) */ }
  try {
    if (setupManager && typeof setupManager.listComponents === 'function') {
      snap.components = setupManager.listComponents();
    }
  } catch (e) {
    log(`⚠ Impossible de lister les composants pour le rapport : ${e.message}`);
  }
  return snap;
}

// Construit le texte complet, prêt à copier-coller tel quel dans une IA
// externe. `actions` = tableau d'actions déjà tentées par repair() (peut être
// vide si diagnose() seul a été utilisé, ou si aucune signature n'a matché).
function buildExternalAiPrompt({ logText, actions, snapshot }) {
  const { text: logExcerpt, truncated } = truncateLogForReport(logText);

  const diagLines = (actions || []).map(a =>
    `- [${a.autoFixable ? 'auto-réparable' : 'MANUEL requis'}] ${a.id} — ${a.diagnosis}\n  → ${a.fixed ? '✅ déjà corrigé automatiquement : ' : '❌ non résolu par IAReparateur : '}${a.detail}`
  ).join('\n');

  const compLines = (snapshot.components || [])
    .map(c => `  - ${c.label} (${c.id}) : ${c.installed ? 'installé' : 'ABSENT'}`)
    .join('\n');

  return [
    "Tu es sollicité(e) en renfort par « IAReparateur », le module de diagnostic automatique intégré à APK Factory Pro (application Electron Windows qui décompile/recompile/signe des APK Android via apktool, jadx, Gradle + Android SDK, et keytool/jarsigner/apksigner).",
    "",
    "IAReparateur a déjà tenté une réparation automatique du problème ci-dessous et n'a pas pu tout résoudre seul (sinon on ne te solliciterait pas). Voici tout le contexte disponible pour éviter de redemander des infos déjà connues :",
    "",
    "### Environnement",
    `- OS : ${snapshot.platform} ${snapshot.arch} (${snapshot.osRelease})`,
    `- Node.js embarqué : ${snapshot.nodeVersion}`,
    `- App packagée (build client) : ${snapshot.packaged === null ? 'inconnu' : (snapshot.packaged ? 'oui' : 'non, mode développement')}`,
    `- Dossier des outils : ${snapshot.toolsDir || 'inconnu'}`,
    ...(compLines ? [`- Composants installés/absents :\n${compLines}`] : []),
    "",
    "### Diagnostic déjà posé par IAReparateur",
    diagLines || "(aucune signature connue reconnue dans le journal — le problème n'est probablement pas d'ordre système JDK/SDK/apktool/Gradle/keystore, plutôt lié au contenu ou au code du projet généré lui-même)",
    "",
    `### Journal d'erreur brut${truncated ? ' (tronqué — début + fin conservés, l\'essentiel est presque toujours à la fin)' : ''}`,
    "```",
    logExcerpt || "(journal vide)",
    "```",
    "",
    "### Ce qu'on attend de toi",
    "1. Identifie la cause précise (pas juste la catégorie déjà détectée ci-dessus).",
    "2. Si une action corrective existe, donne un script PRÊT À COLLER (PowerShell ou CMD — l'app tourne sous Windows) OU un patch précis (fichier + lignes exactes) à appliquer, pas une explication vague.",
    "3. Si le blocage est un mot de passe manquant, une clé de production perdue, ou un fichier fourni par le client corrompu, dis-le clairement : ce n'est PAS auto-réparable, il n'existe pas de contournement technique sûr.",
    "4. Reste dans le périmètre outils système (JDK/keytool, apktool, Android SDK/build-tools, Gradle, keystore) sauf si le journal montre clairement une erreur de code applicatif du projet généré — dans ce cas dis-le aussi.",
  ].join('\n');
}

// -----------------------------------------------------------------------
// diagnose() — analyse seule, sans rien modifier sur le disque. Utile pour
// un aperçu ("voici ce que je vais réparer") avant d'agir, ou pour une UI
// qui veut juste afficher le diagnostic sans lancer la réparation.
// -----------------------------------------------------------------------
function diagnose(logText) {
  const text = String(logText || '');
  const signatures = buildSignatures();
  return signatures
    .filter(sig => sig.test.test(text))
    .map(sig => ({ id: sig.id, diagnosis: sig.diagnosis, autoFixable: sig.autoFixable }));
}

// -----------------------------------------------------------------------
// generateExternalAiReport() — même chose que diagnose(), mais renvoie en
// plus le texte prêt-à-coller pour une IA externe (voir section RAPPORT
// POUR IA EXTERNE plus haut). N'exécute AUCUNE réparation, ne modifie rien
// sur le disque — utilisable pour un bouton "📋 Copier pour une autre IA"
// côté UI, indépendamment d'un appel à repair().
// -----------------------------------------------------------------------
function generateExternalAiReport(logText) {
  const matched = diagnose(logText);
  const snapshot = buildSystemSnapshot();
  // On ne dispose pas ici du détail "fixed/detail" (repair() ne tourne pas)
  // — on présente donc chaque signature reconnue comme diagnostic seul.
  const actions = matched.map(m => ({ ...m, fixed: false, detail: 'diagnostic seul (aucune réparation lancée depuis cet aperçu).' }));
  return {
    matched: matched.map(m => m.id),
    report: buildExternalAiPrompt({ logText, actions, snapshot }),
  };
}

// -----------------------------------------------------------------------
// repair() — diagnostique ET applique les corrections auto-réparables.
// context : { toolsDir } au minimum (= setupManager.ROOT côté appelant si
// non fourni). onProgress(p) est ré-émis vers le renderer comme pour les
// installations de composants classiques (même canal 'setup-progress').
// -----------------------------------------------------------------------
async function repair(logText, context, onProgress) {
  const text = String(logText || '');
  const ctx = { toolsDir: (setupManager && setupManager.ROOT) || null, ...(context || {}) };
  const signatures = buildSignatures();
  const matched = signatures.filter(sig => sig.test.test(text));

  if (!matched.length) {
    const snapshot = buildSystemSnapshot();
    return {
      matched: [],
      fixedCount: 0,
      actions: [],
      canRetry: false,
      summary: "Aucune panne connue reconnue dans ce journal — le problème n'est probablement pas d'ordre système (JDK/SDK/apktool/Gradle/keystore), plutôt lié au contenu du projet lui-même.",
      // Aucune signature connue ⇒ c'est justement le cas le plus utile à
      // remonter tel quel à une IA externe, qui n'est pas limitée à notre
      // table de signatures et peut reconnaître un cas inédit.
      externalAiReport: buildExternalAiPrompt({ logText: text, actions: [], snapshot }),
    };
  }

  log(`🔎 ${matched.length} signature(s) reconnue(s) : ${matched.map(m => m.id).join(', ')}`);

  const actions = [];
  for (const sig of matched) {
    onProgress?.({ status: 'diagnosing', id: sig.id, message: sig.diagnosis });
    try {
      const result = await sig.repair(ctx, onProgress);
      actions.push({ id: sig.id, diagnosis: sig.diagnosis, autoFixable: sig.autoFixable, ...result });
    } catch (e) {
      actions.push({ id: sig.id, diagnosis: sig.diagnosis, autoFixable: sig.autoFixable, fixed: false, detail: `Erreur pendant la réparation : ${e.message}` });
    }
  }

  const fixedCount = actions.filter(a => a.fixed).length;
  const unresolvedManual = actions.filter(a => !a.autoFixable);
  const canRetry = fixedCount > 0; // au moins une correction a été appliquée → ça vaut le coup de relancer l'opération

  let summary;
  if (fixedCount === actions.length) {
    summary = `✅ ${fixedCount} panne(s) corrigée(s) automatiquement — relance l'opération, ça devrait passer.`;
  } else if (fixedCount > 0) {
    summary = `⚠ ${fixedCount}/${actions.length} panne(s) corrigée(s). ${unresolvedManual.length} nécessite(nt) une action de ta part (voir détails).`;
  } else {
    summary = `❌ Aucune correction automatique possible pour ce cas précis (voir détails) — une intervention manuelle est nécessaire.`;
  }

  log(summary);

  // Rapport pour IA externe : généré dès qu'il reste au moins une action non
  // résolue (manuelle ou auto-réparable mais qui a quand même échoué) — pas
  // besoin de coller quoi que ce soit dans une autre IA si tout est ✅.
  const stillUnresolved = actions.some(a => !a.fixed);
  const externalAiReport = stillUnresolved
    ? buildExternalAiPrompt({ logText: text, actions, snapshot: buildSystemSnapshot() })
    : null;

  return { matched: matched.map(m => m.id), fixedCount, actions, canRetry, summary, externalAiReport };
}

module.exports = { diagnose, repair, buildSignatures, generateExternalAiReport, buildSystemSnapshot };
