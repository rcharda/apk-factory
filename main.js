const { app, BrowserWindow, shell, dialog, Menu, ipcMain, safeStorage } = require('electron');
const { autoUpdater } = require('electron-updater');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const http = require('http');
const setupManager = require('./setup.js');
const iaReparateur = require('./iareparateur.js');

const PORT = 7842;
let mainWindow = null;
let pythonProcess = null;
let serverReady = false;
let licensedKey = null; // rempli une fois la licence validée (clé, plus d'email)

// ─── Licence : session persistante + verrouillage "1 appareil à la fois" ─────
// Système par CLÉ DE LICENCE (10 caractères) — il n'y a plus de compte
// email/mot de passe ni de Supabase Auth. La "session" stockée localement ne
// contient qu'une license_key. Les identifiants Supabase du renderer
// (license-renderer.js) sont dupliqués ici volontairement : ce sont des clés
// PUBLIQUES (anon key), le process principal ne fait qu'une vérification
// périodique en arrière-plan (heartbeat), toute la logique d'activation reste
// gérée côté renderer (license-renderer.js).
const SUPABASE_URL = "https://yvcdadenofftnbljutwk.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inl2Y2RhZGVub2ZmdG5ibGp1dHdrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI4NzQ0ODIsImV4cCI6MjA4ODQ1MDQ4Mn0.xqJzLpQszFmph599FBIvdE7NF88_i-JkABG-aSrAndE";
const SESSION_FILE = () => path.join(app.getPath('userData'), 'session.json');
const DEVICE_FILE  = () => path.join(app.getPath('userData'), 'device.json');
const DEVICE_HEARTBEAT_MS = 2 * 60 * 1000; // vérifie toutes les 2 minutes
let deviceHeartbeat = null;

function getDeviceId() {
  try {
    const f = DEVICE_FILE();
    if (fs.existsSync(f)) {
      const d = JSON.parse(fs.readFileSync(f, 'utf-8'));
      if (d && d.deviceId) return d.deviceId;
    }
  } catch (e) { /* fichier corrompu -> on en régénère un */ }
  const id = crypto.randomUUID();
  try { fs.writeFileSync(DEVICE_FILE(), JSON.stringify({ deviceId: id })); } catch (e) {}
  return id;
}

// IP publique de CET appareil, à titre INFORMATIF uniquement (support/audit
// dans Supabase). Ne sert JAMAIS de critère pour bloquer/autoriser un accès :
// une IP change en permanence (4G, changement de box, VPN) et plusieurs
// appareils différents peuvent légitimement partager la même IP (routeur
// familial/bureau). Le seul critère de verrouillage reste device_id.
async function getPublicIp() {
  try {
    const r = await fetch('https://api.ipify.org?format=json');
    if (!r.ok) return null;
    const d = await r.json();
    return d.ip || null;
  } catch (e) { return null; }
}

function readStoredSession() {
  try { return JSON.parse(fs.readFileSync(SESSION_FILE(), 'utf-8')); } catch (e) { return null; }
}
function writeStoredSession(data) {
  try { fs.writeFileSync(SESSION_FILE(), JSON.stringify(data)); return true; } catch (e) { return false; }
}
function clearStoredSession() {
  try { fs.unlinkSync(SESSION_FILE()); } catch (e) {}
  return true;
}

ipcMain.handle('get-device-id', () => getDeviceId());
ipcMain.handle('get-stored-session', () => readStoredSession());
ipcMain.handle('save-session', (event, data) => writeStoredSession(data));
ipcMain.handle('clear-session', () => clearStoredSession());

function getLicensePath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'license.html')
    : path.join(__dirname, 'license.html');
}

// Interroge Supabase pour la licence active correspondant à cette clé. Retourne :
//  { ok: true, license }   -> licence active trouvée (peut être liée à un autre appareil)
//  { ok: false, expired }  -> plus de licence active / expirée / clé introuvable
//  { ok: true, networkError: true } -> souci réseau/serveur, on ne prend aucune décision
// Nombre de résultats vides CONSÉCUTIFS requis avant de considérer la licence
// comme réellement révoquée. Une policy RLS/PostgREST peut renvoyer 200 OK
// avec un tableau vide lors d'un blip transitoire (cache, race sur le JWT,
// etc.) — ce n'est PAS une erreur HTTP, donc `!r.ok` ne l'attrape pas.
// On exige plusieurs échecs de suite pour ne pas couper la session au
// moindre accroc.
const MAX_CONSECUTIVE_EMPTY = 3;
const EMPTY_RETRY_DELAY_MS = 700;
let consecutiveEmptyChecks = 0;

function sleep(ms) { return new Promise((res) => setTimeout(res, ms)); }

async function queryLicenseOnce(licenseKey) {
  const url = `${SUPABASE_URL}/rest/v1/apk_factory_licenses?license_key=eq.${encodeURIComponent(licenseKey)}&limit=1`;
  const r = await fetch(url, {
    headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${SUPABASE_ANON_KEY}` },
  });
  if (!r.ok) return { networkError: true };
  const rows = await r.json();
  return { rows };
}

async function fetchActiveLicense(licenseKey) {
  try {
    let { networkError, rows } = await queryLicenseOnce(licenseKey);
    if (networkError) return { ok: true, networkError: true };

    // Tableau vide sur un 200 OK : on retente une fois tout de suite avant
    // même de compter ça comme un "empty check", ça absorbe la plupart des
    // blips de cache PostgREST à lui seul.
    if (!rows.length) {
      await sleep(EMPTY_RETRY_DELAY_MS);
      const retry = await queryLicenseOnce(licenseKey);
      if (retry.networkError) return { ok: true, networkError: true };
      rows = retry.rows;
    }

    if (!rows.length) {
      consecutiveEmptyChecks++;
      if (consecutiveEmptyChecks < MAX_CONSECUTIVE_EMPTY) {
        // On ne sait pas encore si c'est réel ou transitoire : on ne coupe rien.
        return { ok: true, networkError: true };
      }
      // Seuil atteint : on considère que c'est confirmé (clé vraiment introuvable/révoquée).
      consecutiveEmptyChecks = 0;
      return { ok: false, expired: false };
    }

    // Résultat non vide : on repart de zéro sur le compteur.
    consecutiveEmptyChecks = 0;
    const license = rows[0];
    if (license.status !== 'active') return { ok: false, expired: false };
    if (license.expires_at && new Date(license.expires_at) < new Date()) return { ok: false, expired: true };
    return { ok: true, license };
  } catch (e) {
    return { ok: true, networkError: true };
  }
}

// Relie la licence à CET appareil (reprend la main sur un autre appareil déjà connecté).
async function reclaimDeviceForLicense(licenseId) {
  const ip = await getPublicIp(); // informatif seulement, voir getPublicIp()
  try {
    await fetch(`${SUPABASE_URL}/rest/v1/rpc/apk_factory_bind_device`, {
      method: 'POST',
      headers: {
        apikey: SUPABASE_ANON_KEY,
        Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ p_license_id: licenseId, p_device_id: getDeviceId(), p_ip: ip }),
    });
  } catch (e) {}
}

function stopDeviceHeartbeat() {
  if (deviceHeartbeat) { clearInterval(deviceHeartbeat); deviceHeartbeat = null; }
}

// Vérifie périodiquement que la licence est toujours active et toujours liée
// à CET appareil. Si un AUTRE appareil a pris la main, on ne coupe pas
// directement : on propose un choix (comme au moment de la connexion) —
// reprendre la main ici (l'autre appareil sera déconnecté à son prochain
// contrôle) ou rester déconnecté et laisser l'autre appareil actif.
function startDeviceHeartbeat(licenseKey) {
  stopDeviceHeartbeat();
  deviceHeartbeat = setInterval(async () => {
    if (!mainWindow) return;
    const res = await fetchActiveLicense(licenseKey);
    if (res.networkError) return; // pas de décision sur un simple souci réseau

    if (!res.ok) {
      // Licence plus active / expirée / clé révoquée : pas de choix possible, on déconnecte.
      stopDeviceHeartbeat();
      clearStoredSession();
      const detail = res.expired
        ? "Cette licence a expiré. Renouvelle ton abonnement pour continuer à utiliser l'application."
        : "Cette clé de licence n'est plus valide. " +
          "Si tu penses que c'est une erreur, contacte le support avec cette information.";
      dialog.showErrorBox('Session terminée', detail);
      mainWindow.loadFile(getLicensePath());
      return;
    }

    const myId = getDeviceId();
    if (res.license.device_id && res.license.device_id !== myId) {
      stopDeviceHeartbeat(); // suspendu pendant qu'on attend la réponse de l'utilisateur
      const { response } = await dialog.showMessageBox(mainWindow, {
        type: 'question',
        buttons: ['Reprendre ici', 'Rester déconnecté'],
        defaultId: 0,
        cancelId: 1,
        title: 'Licence utilisée sur un autre appareil',
        message: 'Cette licence est maintenant utilisée sur un autre appareil.',
        detail: "« Reprendre ici » redonne l'accès à CET appareil (l'autre sera déconnecté). " +
                "« Rester déconnecté » laisse l'autre appareil actif et déconnecte celui-ci.",
      });
      if (!mainWindow) return;
      if (response === 0) {
        await reclaimDeviceForLicense(res.license.id);
        startDeviceHeartbeat(licenseKey); // reprend la surveillance normalement
      } else {
        clearStoredSession();
        mainWindow.loadFile(getLicensePath());
      }
      return;
    }
  }, DEVICE_HEARTBEAT_MS);
}

// ─── Signature de production automatisée ("release") ────────────────────────
// Objectif : une seule interruption humaine, UNE FOIS par machine (générer un
// nouveau keystore ou importer un existant + son mot de passe), puis plus
// jamais aucune saisie ni aucune exposition du mot de passe — ni à l'agent IA
// (qui tourne dans le renderer, contextIsolation activée), ni dans les logs,
// ni dans le réseau visible du DevTools de la fenêtre principale. Le mot de
// passe n'existe en clair QUE dans la mémoire de CE process (main), et
// uniquement le temps d'un appel de signature.
//
// Stockage : le mot de passe est chiffré avec safeStorage (DPAPI sous
// Windows, Keychain sous macOS, libsecret sous Linux) — déchiffrable
// uniquement par CE compte utilisateur sur CETTE machine. Le fichier
// .keystore lui-même est conservé en base64 dans le même fichier JSON
// (le fichier gardé par le serveur Python dans tools/ reste aussi la copie
// de référence, mais on ne dépend pas de sa persistance).
const RELEASE_KS_META_FILE = () => path.join(app.getPath('userData'), 'release-keystore.json');

// ---------------------------------------------------------------------
// MULTI-KEYSTORE : le fichier stockait à l'origine UN SEUL keystore global
// (un objet unique, écrasé à chaque génération/import). Or agent-engine.js
// (resolveSigningKey) est conçu pour PLUSIEURS clés — une par app/package,
// avec réutilisation possible d'une clé existante — via
// window.electronAPI.releaseKeystoreList(), qui n'existait nulle part.
//
// Nouveau format sur disque : { keystores: [ { id, alias, ksName, dname,
// encStorePass, encKeyPass, keystoreB64, createdAt, source }, ... ] }
// id = "default" (première clé jamais créée sur la machine) ou le
// packageName de l'app pour une "signature dédiée" (voir agent-engine.js,
// resolveSigningKey → finalKey = pkg).
//
// Rétrocompatibilité : si le fichier existant est encore l'ancien objet
// unique (repérable par la présence de "ksName" à la racine), on le migre
// automatiquement en { keystores: [{ id: 'default', ...ancienObjet }] }
// et on réécrit le fichier dans le nouveau format dès la première lecture.
// ---------------------------------------------------------------------
function readKeystoreStore() {
  let raw;
  try { raw = JSON.parse(fs.readFileSync(RELEASE_KS_META_FILE(), 'utf-8')); }
  catch (e) { return { keystores: [] }; }

  if (raw && Array.isArray(raw.keystores)) return raw;

  // Ancien format (objet unique à la racine) → migration silencieuse.
  if (raw && raw.ksName) {
    const migrated = { keystores: [{ id: 'default', ...raw }] };
    try { writeKeystoreStore(migrated); } catch { /* pas bloquant, on retentera au prochain write */ }
    return migrated;
  }

  return { keystores: [] };
}

function writeKeystoreStore(store) {
  fs.writeFileSync(RELEASE_KS_META_FILE(), JSON.stringify(store));
}

function findKeystoreEntry(id) {
  const store = readKeystoreStore();
  if (!store.keystores.length) return null;
  const wanted = id || 'default';
  return store.keystores.find((k) => k.id === wanted) || null;
}

// Ajoute (ou remplace, si le même id existe déjà — ex: régénération
// volontaire) une entrée dans la liste, sans toucher aux autres clés.
function upsertKeystoreEntry(id, fields) {
  const store = readKeystoreStore();
  const finalId = id || 'default';
  const idx = store.keystores.findIndex((k) => k.id === finalId);
  const entry = { id: finalId, ...fields };
  if (idx >= 0) store.keystores[idx] = entry;
  else store.keystores.push(entry);
  writeKeystoreStore(store);
  return entry;
}

// Conservées pour compatibilité avec le code existant qui ne raisonnait
// qu'en "la" clé (ex: valeur par défaut avant que keystoreKey ne soit
// branché) — retombent désormais sur l'entrée 'default' ou, à défaut,
// la première clé connue.
function readReleaseKeystoreMeta(id) {
  return findKeystoreEntry(id) || readKeystoreStore().keystores[0] || null;
}

function decryptReleasePasswords(meta) {
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error(
      "Le coffre sécurisé du système (safeStorage) n'est pas disponible sur cette machine. " +
      "Impossible de déverrouiller le mot de passe du keystore de production en toute sécurité."
    );
  }
  return {
    storePass: safeStorage.decryptString(Buffer.from(meta.encStorePass, 'base64')),
    keyPass:   safeStorage.decryptString(Buffer.from(meta.encKeyPass,   'base64')),
  };
}

// Liste des clés de signature existantes — c'est CE handler qui manquait
// et que agent-engine.js appelait déjà en aveugle (protégé par un
// typeof === 'function', donc aucun crash, mais la fonctionnalité de choix
// "réutiliser vs créer" ne se déclenchait jamais). Le champ `key` correspond
// à l'`id` interne (utilisé ensuite comme `keystoreKey` par buildWithReleaseSigning).
ipcMain.handle('release-keystore-list', () => {
  const store = readKeystoreStore();
  return store.keystores.map((k) => ({
    key: k.id,
    alias: k.alias,
    createdAt: k.createdAt,
    source: k.source,
  }));
});

ipcMain.handle('release-keystore-status', () => {
  const store = readKeystoreStore();
  if (!store.keystores.length) return { configured: false, count: 0 };
  const meta = store.keystores[0];
  return { configured: true, count: store.keystores.length, alias: meta.alias, createdAt: meta.createdAt, source: meta.source };
});

// Interroge une opération asynchrone du serveur Python local (même logique
// que /status côté renderer, dupliquée ici car le main process ne partage
// pas le contexte JS du renderer).
async function pollLocalOp(token, { timeoutMs = 30000, intervalMs = 800 } = {}) {
  const start = Date.now();
  let last = null;
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/status?session=${encodeURIComponent(token)}`);
      const d = await r.json();
      last = d;
      if (d.status === 'done' || d.status === 'error') return d;
    } catch (e) { /* serveur pas encore prêt, on retente */ }
    await new Promise((res) => setTimeout(res, intervalMs));
  }
  return { status: 'timeout', ...(last || {}) };
}

// ── Fenêtre modale de configuration (générer / importer) ───────────────────
// Complètement isolée de builder.html : son propre preload, aucune méthode
// partagée avec le chat IA. C'est la SEULE porte d'entrée pour créer ou
// saisir un mot de passe de production.
let keystoreSetupWindow = null;
let keystoreSetupResolve = null;
// Id sous lequel la PROCHAINE génération/import (keystore-setup-generate /
// keystore-setup-import) doit être enregistrée. Positionné juste avant
// d'ouvrir la fenêtre par openKeystoreSetupWindow(id) — 'default' si aucun
// id explicite (comportement historique inchangé pour la toute première clé).
let pendingKeystoreId = 'default';

function openKeystoreSetupWindow(id) {
  pendingKeystoreId = id || 'default';
  if (keystoreSetupWindow) {
    keystoreSetupWindow.focus();
    return new Promise((resolve) => { keystoreSetupResolve = resolve; });
  }
  return new Promise((resolve) => {
    keystoreSetupResolve = resolve;
    keystoreSetupWindow = new BrowserWindow({
      width: 480,
      height: 480,
      resizable: false,
      minimizable: false,
      maximizable: false,
      parent: mainWindow || undefined,
      modal: !!mainWindow,
      title: 'Signature de production',
      backgroundColor: '#1a1a2e',
      webPreferences: {
        preload: path.join(__dirname, 'keystore-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    keystoreSetupWindow.setMenuBarVisibility(false);
    keystoreSetupWindow.loadFile(path.join(__dirname, 'keystore-setup.html'));
    keystoreSetupWindow.on('closed', () => {
      keystoreSetupWindow = null;
      if (keystoreSetupResolve) {
        const meta = findKeystoreEntry(pendingKeystoreId);
        keystoreSetupResolve(meta ? { configured: true, alias: meta.alias } : { configured: false, cancelled: true });
        keystoreSetupResolve = null;
      }
    });
  });
}

function closeKeystoreSetupWindow(delayMs = 900) {
  setTimeout(() => { if (keystoreSetupWindow) keystoreSetupWindow.close(); }, delayMs);
}

ipcMain.handle('keystore-setup-pick-file', async () => {
  const r = await dialog.showOpenDialog(keystoreSetupWindow || mainWindow, {
    title: 'Choisir un keystore existant',
    filters: [{ name: 'Keystore', extensions: ['jks', 'keystore', 'p12'] }],
    properties: ['openFile'],
  });
  if (r.canceled || !r.filePaths.length) return { cancelled: true };
  return { path: r.filePaths[0], name: path.basename(r.filePaths[0]) };
});

ipcMain.handle('keystore-setup-generate', async (event, { alias, appName }) => {
  try {
    const finalAlias = (alias || '').trim() || 'release';
    const storePass = crypto.randomBytes(24).toString('hex');
    const keyPass = storePass; // keystore PKCS12 (create_keystore_python) : un seul mot de passe pour le store et la clé
    const safeCn = (appName || 'App').replace(/[,=]/g, ' ').trim() || 'App';
    const dname = `CN=${safeCn},O=APK Factory,C=BJ`;
    // BUG CORRIGÉ (multi-keystore) : le nom de fichier keystore côté serveur
    // Python était TOUJOURS le même littéral ('apk_factory_pro_release.keystore'),
    // quelle que soit l'app. Dès la 2e "signature dédiée" créée pour un
    // package différent, /create-keystore retombait sur le même fichier
    // déjà écrit avec un AUTRE mot de passe → erreur 'wrong_password' à
    // chaque génération suivante. Le nom inclut maintenant l'id de la clé
    // (pendingKeystoreId, ex: le packageName) pour rester unique par clé.
    const idSlug = String(pendingKeystoreId || 'default').replace(/[^a-zA-Z0-9_-]+/g, '_').slice(0, 60);
    const ksName = `apk_factory_pro_release_${idSlug}.keystore`;

    const startRes = await fetch(`http://127.0.0.1:${PORT}/create-keystore`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: ksName, alias: finalAlias, storePass, keyPass, dname, validity: 10000, forceNew: false }),
    });
    const startData = await startRes.json();
    if (!startData.started) throw new Error("Le serveur local n'a pas démarré la génération du keystore.");

    const result = await pollLocalOp(startData.token, { timeoutMs: 30000 });
    if (result.status !== 'done' || !result.result || !result.result.keystoreB64) {
      if (result.result?.error === 'wrong_password') {
        throw new Error(
          `Un fichier '${ksName}' existe déjà sur cette machine avec un mot de passe différent (probablement créé manuellement avant). ` +
          "Pour ne pas risquer d'écraser une clé déjà utilisée pour signer des APK en circulation, utilise plutôt « Importer un keystore existant » avec le bon mot de passe."
        );
      }
      throw new Error(result.result?.message || 'Échec de la génération du keystore.');
    }

    upsertKeystoreEntry(pendingKeystoreId, {
      ksName, alias: finalAlias, dname,
      encStorePass: safeStorage.encryptString(storePass).toString('base64'),
      encKeyPass: safeStorage.encryptString(keyPass).toString('base64'),
      keystoreB64: result.result.keystoreB64,
      createdAt: new Date().toISOString(),
      source: 'generated',
    });

    closeKeystoreSetupWindow();
    return { configured: true, alias: finalAlias, generated: true };
  } catch (e) {
    return { configured: false, error: e.message || String(e) };
  }
});

ipcMain.handle('keystore-setup-import', async (event, { filePath, alias, storePass, keyPass }) => {
  try {
    if (!filePath || !fs.existsSync(filePath)) throw new Error('Fichier keystore introuvable.');
    if (!storePass) throw new Error('Mot de passe requis.');
    const bytes = fs.readFileSync(filePath);

    upsertKeystoreEntry(pendingKeystoreId, {
      ksName: path.basename(filePath),
      alias: (alias || '').trim() || 'release',
      dname: null,
      encStorePass: safeStorage.encryptString(storePass).toString('base64'),
      encKeyPass: safeStorage.encryptString(keyPass || storePass).toString('base64'),
      keystoreB64: bytes.toString('base64'),
      createdAt: new Date().toISOString(),
      source: 'imported',
    });

    closeKeystoreSetupWindow();
    return { configured: true, alias: (alias || '').trim() || 'release', generated: false };
  } catch (e) {
    return { configured: false, error: e.message || String(e) };
  }
});

ipcMain.handle('keystore-setup-cancel', () => {
  if (keystoreSetupWindow) keystoreSetupWindow.close();
});

// ── Proxy de build signé release, appelé depuis l'agent (renderer) ─────────
// Le renderer/l'agent IA envoie {endpoint, body} EXACTEMENT comme il l'aurait
// envoyé lui-même en fetch() direct — sauf qu'ici c'est CE process (main) qui
// fait la requête HTTP vers le serveur Python local, après avoir injecté les
// identifiants de signature déchiffrés dans body.config.signing. Le mot de
// passe ne transite donc jamais par le contexte JS du renderer/agent.
ipcMain.handle('build-with-release-signing', async (event, { endpoint, body, keystoreKey }) => {
  // BUG CORRIGÉ (multi-keystore) : keystoreKey était reçu par ce handler
  // (preload.js le transmet déjà) mais totalement IGNORÉ — on retombait
  // systématiquement sur readReleaseKeystoreMeta() sans id, donc toujours
  // la même clé globale, quel que soit le choix fait dans resolveSigningKey()
  // (agent-engine.js) entre "réutiliser une clé existante" et "signature
  // dédiée". On cherche maintenant l'entrée précise correspondant à
  // keystoreKey, et on ouvre la fenêtre de configuration POUR CET id
  // précis si elle n'existe pas encore.
  let finalMeta = findKeystoreEntry(keystoreKey);
  if (!finalMeta) {
    const setupResult = await openKeystoreSetupWindow(keystoreKey);
    if (!setupResult.configured) {
      return { needsSetup: true, cancelled: true, error: 'Configuration de la signature de production annulée par le client.' };
    }
    finalMeta = findKeystoreEntry(keystoreKey);
  }
  if (!finalMeta) return { needsSetup: true, error: 'Configuration introuvable après la fenêtre de configuration.' };

  let passwords;
  try { passwords = decryptReleasePasswords(finalMeta); }
  catch (e) { return { error: e.message || String(e) }; }

  const signedBody = {
    ...body,
    config: {
      ...(body.config || {}),
      signing: {
        mode: 'custom',
        keystoreB64: finalMeta.keystoreB64,
        alias: finalMeta.alias,
        storePass: passwords.storePass,
        keyPass: passwords.keyPass,
      },
    },
  };

  try {
    const r = await fetch(`http://127.0.0.1:${PORT}${endpoint}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(signedBody),
    });
    const data = await r.json();
    passwords = null; // n'existe plus que le temps de cet appel
    return data;
  } catch (e) {
    return { error: e.message || String(e) };
  }
});


function setupAutoUpdater() {
  // Logs visibles dans la console Electron
  autoUpdater.logger = require('electron-log');
  autoUpdater.logger.transports.file.level = 'info';

  // Vérifier silencieusement au démarrage (pas de popup intempestif)
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('checking-for-update', () => {
    console.log('[Updater] Vérification des mises à jour...');
  });

  autoUpdater.on('update-available', (info) => {
    console.log(`[Updater] Mise à jour disponible : v${info.version}`);
    // Notifier l'UI (optionnel)
    mainWindow?.webContents.send('update-available', { version: info.version });
  });

  autoUpdater.on('update-not-available', () => {
    console.log('[Updater] Aucune mise à jour disponible.');
  });

  autoUpdater.on('download-progress', (progress) => {
    const pct = Math.round(progress.percent);
    console.log(`[Updater] Téléchargement : ${pct}%`);
    mainWindow?.webContents.send('update-progress', { percent: pct });
  });

  autoUpdater.on('update-downloaded', (info) => {
    console.log(`[Updater] Mise à jour v${info.version} téléchargée.`);
    dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: 'Mise à jour disponible',
      message: `APK Factory Pro v${info.version} est prête à être installée.`,
      detail: 'L\'application va redémarrer pour appliquer la mise à jour.',
      buttons: ['Redémarrer maintenant', 'Plus tard'],
      defaultId: 0,
    }).then(({ response }) => {
      if (response === 0) autoUpdater.quitAndInstall();
    });
  });

  autoUpdater.on('error', (err) => {
    console.error('[Updater] Erreur :', err.message);
    // Pas de dialog d'erreur pour ne pas perturber l'utilisateur
  });

  // Lancer la vérification (uniquement en mode packagé)
  if (app.isPackaged) {
    autoUpdater.checkForUpdates().catch((err) => {
      console.error('[Updater] checkForUpdates échoué :', err.message);
    });
  }
}

// ─── Trouver Python ───────────────────────────────────────────────────────────
// IMPORTANT : on ne se contente plus de renvoyer 'python' en aveugle (ça plantait
// silencieusement sur les machines où seul 'py' ou 'python3' existe). On teste
// chaque candidat avec --version avant de le retenir, et on garde la trace de
// ce qui a été essayé pour pouvoir l'afficher dans le message d'erreur final.
function testPythonCandidate(exe) {
  try {
    const r = spawnSync(exe, ['--version'], { timeout: 5000, windowsHide: true });
    if (r.error) return { ok: false, error: r.error.message };
    if (r.status !== 0) return { ok: false, error: `code de sortie ${r.status}` };
    const out = ((r.stdout || '').toString() + (r.stderr || '').toString()).trim();
    return { ok: true, version: out };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function findPython() {
  const attempts = [];

  // En mode packagé, le python embarqué "de base" livré avec l'installeur
  // (s'il existe un jour dans extraResources) est prioritaire.
  if (app.isPackaged) {
    const embeddedPy = path.join(process.resourcesPath, 'python', 'python.exe');
    if (fs.existsSync(embeddedPy)) {
      const test = testPythonCandidate(embeddedPy);
      attempts.push({ exe: embeddedPy, ...test });
      if (test.ok) return { exe: embeddedPy, attempts };
    }
  }

  // BUG CORRIGÉ : le composant "python" de setup.js (installable depuis le
  // modal Composants) télécharge Python dans setupManager.ROOT/python/
  // (dossier utilisateur inscriptible), PAS dans resourcesPath/python (qui
  // n'existe même pas — absent de extraResources). Sans ce check, un
  // client qui installe "Python embarqué" via l'UI ne verrait jamais cet
  // interpréteur utilisé : main.js retombait toujours sur le PATH système
  // et échouait sur une machine sans Python préinstallé.
  try {
    const componentPy = path.join(setupManager.ROOT, 'python', 'python.exe');
    if (fs.existsSync(componentPy)) {
      const test = testPythonCandidate(componentPy);
      attempts.push({ exe: componentPy, ...test });
      if (test.ok) return { exe: componentPy, attempts };
    }
  } catch { /* setupManager.ROOT indisponible pour une raison quelconque */ }

  for (const exe of ['python', 'python3', 'py']) {
    const test = testPythonCandidate(exe);
    attempts.push({ exe, ...test });
    if (test.ok) return { exe, attempts };
  }

  // Aucun candidat ne fonctionne : on renvoie quand même 'python' comme dernier
  // recours (au cas où testPythonCandidate aurait un faux négatif), mais avec
  // la liste complète des échecs pour le diagnostic.
  return { exe: 'python', attempts, allFailed: true };
}

// ─── Trouver server.py ────────────────────────────────────────────────────────
function getServerPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'server.py');
  }
  return path.join(__dirname, 'server.py');
}

// ─── Tampon des dernières sorties Python (pour diagnostic en cas d'échec) ─────
const PYTHON_LOG_TAIL_MAX = 4000;
let pythonStdoutTail = '';
let pythonStderrTail = '';
let pythonExitInfo = null; // { code, signal } une fois le process terminé
let lastPythonAttempts = [];
let lastServerScriptPath = '';

function appendTail(current, chunk) {
  const next = current + chunk;
  return next.length > PYTHON_LOG_TAIL_MAX ? next.slice(-PYTHON_LOG_TAIL_MAX) : next;
}

// ─── Démarrer le serveur Python ───────────────────────────────────────────────
function startPythonServer() {
  return new Promise((resolve, reject) => {
    const { exe: pythonExe, attempts, allFailed } = findPython();
    const serverScript = getServerPath();
    lastPythonAttempts = attempts;
    lastServerScriptPath = serverScript;

    if (allFailed) {
      console.error('[APK Factory] Aucun interpréteur Python fonctionnel trouvé.', attempts);
    }

    if (!fs.existsSync(serverScript)) {
      reject(new Error(`server.py introuvable : ${serverScript}`));
      return;
    }

    console.log(`[APK Factory] Démarrage du serveur : ${pythonExe} ${serverScript}`);

    pythonProcess = spawn(pythonExe, [serverScript], {
      cwd: app.isPackaged ? process.resourcesPath : path.dirname(serverScript),
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        // CRITIQUE : server.py doit lire les composants dans le MÊME
        // dossier que celui où setup.js les installe réellement
        // (resolveRoot() → userData/tools en packagé). Sans ça, server.py
        // retombe sur BASE_DIR/tools (= resources/tools, en lecture seule)
        // et ne trouve jamais rien de ce que setup.js vient d'installer.
        APKF_TOOLS_DIR: setupManager.ROOT,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    pythonProcess.stdout.on('data', (data) => {
      const text = data.toString();
      console.log('[Python]', text.trim());
      pythonStdoutTail = appendTail(pythonStdoutTail, text);
    });

    pythonProcess.stderr.on('data', (data) => {
      const text = data.toString();
      console.error('[Python stderr]', text.trim());
      pythonStderrTail = appendTail(pythonStderrTail, text);
    });

    pythonProcess.on('error', (err) => {
      console.error('[APK Factory] Erreur démarrage Python :', err);
      reject(err);
    });

    pythonProcess.on('exit', (code, signal) => {
      pythonExitInfo = { code, signal };
      if (code !== 0 && code !== null) {
        console.warn(`[APK Factory] Python s'est arrêté (code ${code})`);
      }
      // Si le process meurt avant d'avoir répondu sur le port, inutile d'attendre
      // les 30 secondes de timeout : on rejette tout de suite avec le vrai motif.
      if (!serverReady) {
        reject(buildStartupError());
      }
    });

    // Attendre que le serveur réponde (max 30 secondes)
    waitForServer(resolve, reject, 0);
  });
}

// Construit un message d'erreur exploitable à partir de tout ce qu'on a observé :
// interpréteurs Python testés, chemin du script, code de sortie, stderr récent.
function buildStartupError() {
  const lines = [];
  if (lastPythonAttempts.length) {
    lines.push('Interpréteurs Python testés :');
    for (const a of lastPythonAttempts) {
      lines.push(a.ok ? `  ✓ ${a.exe} → ${a.version}` : `  ✗ ${a.exe} → ${a.error}`);
    }
  }
  lines.push(`Script serveur : ${lastServerScriptPath}`);
  if (pythonExitInfo) {
    lines.push(`Process Python terminé (code=${pythonExitInfo.code}, signal=${pythonExitInfo.signal || 'aucun'})`);
  }
  if (pythonStderrTail.trim()) {
    lines.push('Dernière sortie d\'erreur Python :');
    lines.push(pythonStderrTail.trim().split('\n').slice(-20).join('\n'));
  } else if (pythonStdoutTail.trim()) {
    lines.push('Dernière sortie Python :');
    lines.push(pythonStdoutTail.trim().split('\n').slice(-20).join('\n'));
  } else {
    lines.push('Aucune sortie reçue de Python (le process n\'a peut-être jamais démarré).');
  }
  return new Error(lines.join('\n'));
}

function waitForServer(resolve, reject, attempts) {
  if (attempts > 60) {
    reject(buildStartupError());
    return;
  }

  http.get(`http://localhost:${PORT}/`, (res) => {
    if (res.statusCode < 500) {
      console.log('[APK Factory] Serveur prêt !');
      serverReady = true;
      resolve();
    } else {
      setTimeout(() => waitForServer(resolve, reject, attempts + 1), 500);
    }
  }).on('error', () => {
    setTimeout(() => waitForServer(resolve, reject, attempts + 1), 500);
  });
}

// ─── Créer la fenêtre principale ──────────────────────────────────────────────
function createWindow() {
  // BUG-icon01 corrigé : 'resources/tools/icon.ico' n'est jamais copié —
  // aucune entrée extraResources dans package.json ne le prévoit, donc
  // fs.existsSync() échouait quand même silencieusement (icône Electron
  // par défaut). On ajoute une entrée extraResources dédiée
  // ({ "from": "assets/icon.ico", "to": "icon.ico" }) et on pointe ici
  // directement vers resources/icon.ico (racine de resources, pas tools/).
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'icon.ico')
    : path.join(__dirname, 'assets', 'icon.ico');

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    title: 'APK Factory Pro',
    icon: fs.existsSync(iconPath) ? iconPath : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false, // Afficher seulement quand prêt
    backgroundColor: '#1a1a2e',
  });

  // Menu minimaliste
  const menu = Menu.buildFromTemplate([
    {
      label: 'APK Factory',
      submenu: [
        {
          label: 'Actualiser',
          accelerator: 'F5',
          click: () => mainWindow?.webContents.reload(),
        },
        { type: 'separator' },
        {
          label: 'Ouvrir dans le navigateur',
          click: () => shell.openExternal(`http://localhost:${PORT}`),
        },
        { type: 'separator' },
        {
          label: 'Vérifier les mises à jour',
          click: () => {
            if (app.isPackaged) {
              autoUpdater.checkForUpdates().catch((err) => {
                dialog.showErrorBox('Mise à jour', `Erreur : ${err.message}`);
              });
            } else {
              dialog.showMessageBox(mainWindow, {
                type: 'info',
                message: 'Mises à jour uniquement disponibles en version packagée.',
              });
            }
          },
        },
        { type: 'separator' },
        {
          label: 'Composants...',
          click: () => mainWindow?.webContents.send('open-components-modal'),
        },
        { type: 'separator' },
        { role: 'quit', label: 'Quitter' },
      ],
    },
    {
      label: 'Affichage',
      submenu: [
        { role: 'zoomIn', label: 'Zoom +' },
        { role: 'zoomOut', label: 'Zoom -' },
        { role: 'resetZoom', label: 'Zoom normal' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: 'Plein écran' },
        {
          label: 'Outils de développement',
          accelerator: 'F12',
          click: () => mainWindow?.webContents.toggleDevTools(),
        },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);

  // ─── Au démarrage : afficher l'écran de licence (PAS le serveur Python directement) ──
  const licensePath = app.isPackaged
    ? path.join(process.resourcesPath, 'license.html')
    : path.join(__dirname, 'license.html');

  mainWindow.loadFile(licensePath);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    mainWindow.focus();
  });

  // Ouvrir les liens externes dans le vrai navigateur
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(`http://localhost:${PORT}`)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    stopDeviceHeartbeat();
  });
}

// ─── Arrêter proprement le serveur Python ─────────────────────────────────────
function stopPythonServer() {
  if (pythonProcess) {
    console.log('[APK Factory] Arrêt du serveur Python...');
    pythonProcess.kill('SIGTERM');
    pythonProcess = null;
  }
}

// ─── IPC : licence validée → démarrer/attendre le serveur Python et basculer ──
ipcMain.on('license-valid', async (event, { licenseKey, expiresAt }) => {
  licensedKey = licenseKey;
  console.log(`[APK Factory] Licence validée pour la clé ${licenseKey}, expire le ${expiresAt}`);

  if (!mainWindow) return;

  // Si le serveur Python n'est pas encore prêt, on attend qu'il le soit
  try {
    if (!serverReady) {
      mainWindow.webContents.send('license-status', { status: 'starting-server' });
      await waitForServerReady();
    }
    mainWindow.loadURL(`http://localhost:${PORT}`);
    startDeviceHeartbeat(licenseKey);
  } catch (err) {
    dialog.showErrorBox(
      'APK Factory Pro — Erreur de démarrage',
      `Licence validée mais le serveur Python n'a pas démarré.\n\n${err.message}`
    );
  }
});

// ─── IPC : ouvrir un lien externe (paiement FedaPay, support, etc.) ───────────
ipcMain.on('open-external', (event, url) => {
  if (typeof url === 'string' && /^https?:\/\//.test(url)) {
    shell.openExternal(url);
  }
});

// ─── IPC : version de l'app ────────────────────────────────────────────────────
ipcMain.handle('get-version', () => app.getVersion());

// ─── IPC : forcer une vérification de mise à jour depuis le renderer ───────────
ipcMain.handle('check-for-updates', async () => {
  if (!app.isPackaged) return { status: 'dev-mode' };
  try {
    await autoUpdater.checkForUpdates();
    return { status: 'checking' };
  } catch (err) {
    return { status: 'error', message: err.message };
  }
});

let serverStartupError = null; // posé si startPythonServer() rejette en arrière-plan

function waitForServerReady() {
  return new Promise((resolve, reject) => {
    if (serverReady) {
      resolve();
      return;
    }
    if (serverStartupError) {
      reject(serverStartupError);
      return;
    }
    const checkInterval = setInterval(() => {
      if (serverReady) {
        clearInterval(checkInterval);
        resolve();
      } else if (serverStartupError) {
        clearInterval(checkInterval);
        clearTimeout(timeoutId);
        reject(serverStartupError);
      }
    }, 300);
    const timeoutId = setTimeout(() => {
      clearInterval(checkInterval);
      if (!serverReady) reject(buildStartupError());
    }, 30000);
  });
}

// ─── IA locale (hors-ligne) — llama-server.exe, API OpenAI-compatible ─────────
const AI_PORT = 8090;
let aiProcess = null;
let aiCurrentModel = null;

ipcMain.handle('ai-list-models', () => {
  return {
    engineInstalled: setupManager.isEngineInstalled(),
    models: setupManager.listLocalAiModels()
  };
});

ipcMain.handle('ai-install-engine', async () => {
  try {
    await setupManager.installEngine((p) => mainWindow?.webContents.send('setup-progress', p));
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('ai-install-model', async (event, modelId) => {
  try {
    await setupManager.installModel(modelId, (p) => mainWindow?.webContents.send('setup-progress', p));
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// Démarre (ou redémarre si le modèle demandé change) le serveur llama.cpp
// local. Réutilise le même principe que startPythonServer : spawn + attente
// que le port réponde avant de résoudre.
ipcMain.handle('ai-start-local-server', async (event, modelId) => {
  if (!setupManager.isEngineInstalled()) {
    return { ok: false, error: 'Moteur IA locale non installé.' };
  }
  const modelInfo = setupManager.getModelInfo(modelId);
  if (!modelInfo || !fs.existsSync(modelInfo.checkFile)) {
    return { ok: false, error: 'Modèle non installé.' };
  }

  if (aiProcess && aiCurrentModel === modelId) {
    return { ok: true, port: AI_PORT, alreadyRunning: true };
  }
  if (aiProcess) {
    aiProcess.kill('SIGTERM');
    aiProcess = null;
  }

  const enginePath = setupManager.getEnginePath();
  return new Promise((resolve) => {
    aiProcess = spawn(enginePath, [
      '-m', modelInfo.checkFile,
      '--port', String(AI_PORT),
      '-c', String(modelInfo.contextSize || 4096),
      '--host', '127.0.0.1'
    ], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });

    aiCurrentModel = modelId;
    let resolved = false;

    aiProcess.stderr.on('data', (data) => console.log('[AI local]', data.toString().trim()));
    aiProcess.on('error', (err) => {
      if (!resolved) { resolved = true; resolve({ ok: false, error: err.message }); }
    });
    aiProcess.on('exit', () => {
      aiProcess = null;
      aiCurrentModel = null;
    });

    const waitReady = (attempts) => {
      if (resolved) return;
      if (attempts > 60) { resolved = true; return resolve({ ok: false, error: 'Timeout démarrage IA locale.' }); }
      http.get(`http://127.0.0.1:${AI_PORT}/health`, (res) => {
        if (res.statusCode === 200) { resolved = true; resolve({ ok: true, port: AI_PORT }); }
        else setTimeout(() => waitReady(attempts + 1), 500);
      }).on('error', () => setTimeout(() => waitReady(attempts + 1), 500));
    };
    setTimeout(() => waitReady(0), 500);
  });
});

ipcMain.handle('ai-stop-local-server', () => {
  if (aiProcess) { aiProcess.kill('SIGTERM'); aiProcess = null; aiCurrentModel = null; }
  return { ok: true };
});

// ─── IPC : installation modulaire des composants (Gradle, Kotlin, jadx...) ────
ipcMain.handle('setup-list-components', () => {
  return setupManager.listComponents();
});

ipcMain.handle('setup-install-components', async (event, ids) => {
  if (!Array.isArray(ids) || ids.length === 0) {
    return { ok: false, error: 'Aucun composant sélectionné.' };
  }
  try {
    const results = await setupManager.installComponents(ids, (progress) => {
      mainWindow?.webContents.send('setup-progress', progress);
    });
    return { ok: true, results };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ─── IPC : vérifie si une version plus récente d'un composant (Gradle, jadx...)
// est disponible sur GitHub que celle actuellement codée/installée ─────────
ipcMain.handle('setup-check-component-updates', async () => {
  try {
    const updates = await setupManager.checkComponentUpdates();
    return { ok: true, updates };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ─── IPC : réinstalle un composant déjà présent par-dessus l'existant,
// pour appliquer une mise à jour détectée par setup-check-component-updates ──
ipcMain.handle('setup-update-component', async (event, id) => {
  if (!id) return { ok: false, error: 'Identifiant de composant manquant.' };
  try {
    await setupManager.updateComponent(id, (progress) => {
      mainWindow?.webContents.send('setup-progress', progress);
    });
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ─── IPC : composants trouvés par l'IA hors du registre connu ─────────────────
// Recherche seule (lecture publique GitHub) — n'installe jamais rien.
ipcMain.handle('setup-search-github-component', async (event, query) => {
  try {
    return await setupManager.searchGithubReleaseAsset(query);
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// Installation d'un composant trouvé par l'IA — appelé UNIQUEMENT après que
// le client a coché la case et confirmé la popup côté builder.html (voir
// startComponentsInstall()) : jamais depuis l'IA elle-même.
ipcMain.handle('setup-install-dynamic-component', async (event, def) => {
  if (!def || !def.id || !def.url) {
    return { ok: false, error: 'Définition de composant invalide.' };
  }
  try {
    const result = await setupManager.installDynamicComponent(def, (progress) => {
      mainWindow?.webContents.send('setup-progress', progress);
    });
    return { ok: true, ...result };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ─── IPC : IAReparateur — diagnostic + réparation automatique des pannes
// système (JDK/SDK/apktool/Gradle/keystore) détectées dans un journal de
// build/décompilation/signature. Appelé soit par le client (bouton "🩹
// Réparer" dans l'UI), soit par l'agent IA lui-même via le tool
// 'repair_system' (voir agent-engine.js) juste après l'échec d'un
// build_project/decompile/sign, avant de redemander quoi que ce soit.
ipcMain.handle('ia-repair-diagnose', (event, logText) => {
  try {
    return { ok: true, matched: iaReparateur.diagnose(logText) };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('ia-repair-run', async (event, { logText, context } = {}) => {
  try {
    const result = await iaReparateur.repair(logText, context, (progress) => {
      mainWindow?.webContents.send('setup-progress', progress);
    });
    return { ok: true, ...result };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ─── IPC : suggestions IA en attente (persistées dans installed-components.json)
// Permet à builder.html de réafficher, dès l'ouverture de l'app, les
// composants trouvés par l'IA lors d'une session précédente et pas encore
// installés — sans ça une suggestion disparaissait au redémarrage.
ipcMain.handle('setup-list-ai-suggestions', () => {
  return setupManager.listAiSuggestions();
});

ipcMain.handle('setup-save-ai-suggestion', (event, def) => {
  if (!def || !def.id) return { ok: false, error: 'Définition invalide.' };
  setupManager.saveAiSuggestion(def);
  return { ok: true };
});

ipcMain.handle('setup-remove-ai-suggestion', (event, id) => {
  if (!id) return { ok: false, error: 'Identifiant manquant.' };
  setupManager.removeAiSuggestion(id);
  return { ok: true };
});

// ─── IPC : dernier résultat connu de vérification des mises à jour de
// composants, pour affichage immédiat à l'ouverture (avant même qu'un
// nouveau check réseau ne revienne, ou si le client est hors ligne).
ipcMain.handle('setup-get-last-update-check', () => {
  return setupManager.getLastUpdateCheck();
});

// ─── IPC : recherche dans de grandes banques d'API publiques pour ENRICHIR
// les apps générées (libs Android, plugins Cordova, packages Flutter, apps
// F-Droid de référence) — indépendant de setupManager/setup.js, car ce ne
// sont pas des "composants de build" (Gradle, JDK...) mais des dépendances
// de CODE que l'IA peut proposer d'ajouter au projet du client. Recherche
// seule : n'écrit jamais rien tout seul, c'est add_dependency (agent-engine)
// + write_file/replace_line qui insèrent réellement la ligne choisie dans
// build.gradle / config.xml / pubspec.yaml après lecture du résultat.
async function searchMavenCentral(query, limit) {
  const url = `https://search.maven.org/solrsearch/select?q=${encodeURIComponent(query)}&rows=${limit}&wt=json`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Maven Central a répondu ${r.status}`);
  const data = await r.json();
  const docs = (data.response && data.response.docs) || [];
  return docs.map((d) => ({
    source: 'maven',
    id: `${d.g}:${d.a}`,
    label: `${d.g}:${d.a}`,
    version: d.latestVersion || d.v || null,
    description: `Groupe ${d.g}`,
    url: `https://search.maven.org/artifact/${d.g}/${d.a}`,
    // Ligne prête à coller dans build.gradle (dependencies { ... })
    gradleLine: d.latestVersion ? `implementation '${d.g}:${d.a}:${d.latestVersion}'` : null,
  }));
}

async function searchNpmRegistry(query, limit) {
  const url = `https://registry.npmjs.org/-/v1/search?text=${encodeURIComponent(query)}&size=${limit}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`npm registry a répondu ${r.status}`);
  const data = await r.json();
  const objs = data.objects || [];
  return objs.map((o) => ({
    source: 'npm',
    id: o.package.name,
    label: `${o.package.name}@${o.package.version}`,
    version: o.package.version,
    description: o.package.description || '',
    url: (o.package.links && o.package.links.npm) || `https://www.npmjs.com/package/${o.package.name}`,
  }));
}

async function searchPubDev(query, limit) {
  const url = `https://pub.dev/api/search?q=${encodeURIComponent(query)}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`pub.dev a répondu ${r.status}`);
  const data = await r.json();
  const packages = (data.packages || []).slice(0, limit);
  // Un second appel par paquet pour avoir la version courante — limité aux
  // premiers résultats seulement pour ne pas multiplier les requêtes réseau.
  const detailed = await Promise.all(packages.map(async (p) => {
    try {
      const dr = await fetch(`https://pub.dev/api/packages/${encodeURIComponent(p.package)}`);
      if (!dr.ok) throw new Error('détail indisponible');
      const dd = await dr.json();
      const version = dd.latest && dd.latest.version;
      return {
        source: 'pub',
        id: p.package,
        label: `${p.package}: ^${version || '?'}`,
        version: version || null,
        description: (dd.latest && dd.latest.pubspec && dd.latest.pubspec.description) || '',
        url: `https://pub.dev/packages/${p.package}`,
        pubspecLine: version ? `${p.package}: ^${version}` : null,
      };
    } catch (e) {
      return { source: 'pub', id: p.package, label: p.package, version: null, description: '', url: `https://pub.dev/packages/${p.package}` };
    }
  }));
  return detailed;
}

async function searchFDroid(query, limit) {
  const url = `https://search.f-droid.org/api/search_apps?q=${encodeURIComponent(query)}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`F-Droid a répondu ${r.status}`);
  const data = await r.json();
  const results = (data.hits || data.results || []).slice(0, limit);
  return results.map((h) => {
    const src = h._source || h;
    return {
      source: 'fdroid',
      id: src.packageName || src.name,
      label: src.name || src.packageName,
      version: src.suggestedVersionName || null,
      description: (src.summary || '').slice(0, 200),
      url: `https://f-droid.org/packages/${src.packageName || ''}`,
      // Rappel légal : app F-Droid = code tiers sous SA PROPRE licence
      // (souvent copyleft GPL/AGPL) — à utiliser comme RÉFÉRENCE/inspiration
      // de fonctionnalité, jamais copié tel quel dans un APK client sans
      // vérifier la compatibilité de licence.
      licenseNote: 'Vérifier la licence du projet avant toute réutilisation de code.',
    };
  });
}

ipcMain.handle('search-public-library', async (event, args) => {
  const { source, query } = args || {};
  const limit = Math.min(Math.max(parseInt((args && args.limit) || 8, 10) || 8, 1), 20);
  if (!query || !String(query).trim()) return { ok: false, error: 'Requête de recherche vide.' };
  try {
    let results;
    if (source === 'maven') results = await searchMavenCentral(query, limit);
    else if (source === 'npm') results = await searchNpmRegistry(query, limit);
    else if (source === 'pub') results = await searchPubDev(query, limit);
    else if (source === 'fdroid') results = await searchFDroid(query, limit);
    else return { ok: false, error: `Source inconnue : '${source}'. Utilise 'maven', 'npm', 'pub' ou 'fdroid'.` };
    return { ok: true, source, query, results };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ─── Cycle de vie Electron ────────────────────────────────────────────────────
app.whenReady().then(async () => {
  // Afficher l'écran de licence immédiatement
  createWindow();

  // Initialiser l'auto-updater après création de la fenêtre
  setupAutoUpdater();

  // Démarrer le serveur Python en arrière-plan, sans bloquer l'UI de licence
  startPythonServer().catch((err) => {
    console.error('[APK Factory] Échec démarrage Python en arrière-plan :', err);
    serverStartupError = err;
    // L'erreur sera affichée immédiatement (avec le détail complet) au moment où
    // license-valid tentera de basculer, plutôt que d'attendre un nouveau timeout.
  });
});

function stopAiServer() {
  if (aiProcess) {
    console.log('[APK Factory] Arrêt du serveur IA locale...');
    aiProcess.kill('SIGTERM');
    aiProcess = null;
    aiCurrentModel = null;
  }
}

app.on('window-all-closed', () => {
  stopPythonServer();
  stopAiServer();
  app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', () => {
  stopPythonServer();
  stopAiServer();
});
