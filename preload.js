// preload.js — bridge sécurisé entre le process Electron et la page web
// contextIsolation: true → seul ce fichier peut exposer des APIs Node.js à la page

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Version de l'app
  getVersion: () => ipcRenderer.invoke('get-version'),

  // Licence : appelé par license-renderer.js quand une licence valide est confirmée
  licenseValid: (licenseKey, expiresAt) => ipcRenderer.send('license-valid', { licenseKey, expiresAt }),

  // Session persistante — permet de rester connecté après fermeture/réouverture
  // de l'app, et identifiant unique d'appareil pour la licence "1 appareil à la fois".
  getDeviceId:     () => ipcRenderer.invoke('get-device-id'),
  getStoredSession:() => ipcRenderer.invoke('get-stored-session'),
  saveSession:     (data) => ipcRenderer.invoke('save-session', data),
  clearSession:    () => ipcRenderer.invoke('clear-session'),

  // Ouvrir un lien dans le vrai navigateur (paiement FedaPay, support, etc.)
  openExternal: (url) => ipcRenderer.send('open-external', url),

  // Auto-updater
  onUpdateAvailable: (cb) => ipcRenderer.on('update-available', (_, data) => cb(data)),
  onUpdateProgress:  (cb) => ipcRenderer.on('update-progress',  (_, data) => cb(data)),
  checkForUpdates:   ()   => ipcRenderer.invoke('check-for-updates'),

  // Signature de production — appelé par l'agent IA (build_project, mode
  // 'release'). Le main process (main.js) déchiffre et injecte lui-même les
  // identifiants de signature ; ils ne transitent jamais par le renderer.
  // IMPORTANT : le handler main.js attend UN SEUL objet {endpoint, body} —
  // on empaquette donc ici les 3 arguments positionnels appelés côté
  // agent-engine.js (endpoint, body, keystoreKey) dans cet objet.
  buildWithReleaseSigning: (endpoint, body, keystoreKey) =>
    ipcRenderer.invoke('build-with-release-signing', { endpoint, body, keystoreKey }),
  releaseKeystoreStatus: () => ipcRenderer.invoke('release-keystore-status'),
  // Liste des clés de signature existantes (multi-keystore) — appelée par
  // agent-engine.js/resolveSigningKey() pour proposer réutilisation vs
  // nouvelle signature dédiée. Chaque entrée : { key, alias, createdAt, source }.
  releaseKeystoreList: () => ipcRenderer.invoke('release-keystore-list'),

  // Installation modulaire des composants (Gradle, Kotlin, jadx, Android SDK...)
  setupListComponents:    ()      => ipcRenderer.invoke('setup-list-components'),
  setupInstallComponents: (ids)   => ipcRenderer.invoke('setup-install-components', ids),
  onSetupProgress:        (cb)    => ipcRenderer.on('setup-progress', (_, data) => cb(data)),
  onOpenComponentsModal:  (cb)    => ipcRenderer.on('open-components-modal', () => cb()),

  // Composants trouvés par l'IA hors du registre connu (recherche GitHub) —
  // la recherche seule ne télécharge rien ; l'installation n'est déclenchée
  // que par un clic explicite du client dans le modal Composants.
  setupSearchGithubComponent:   (query) => ipcRenderer.invoke('setup-search-github-component', query),
  setupInstallDynamicComponent: (def)   => ipcRenderer.invoke('setup-install-dynamic-component', def),

  // Suggestions IA en attente (persistées) — pour réafficher au démarrage
  // les composants trouvés par l'IA lors d'une session précédente et pas
  // encore installés, sans les perdre au redémarrage de l'app.
  setupListAiSuggestions:  ()   => ipcRenderer.invoke('setup-list-ai-suggestions'),
  setupSaveAiSuggestion:   (def) => ipcRenderer.invoke('setup-save-ai-suggestion', def),
  setupRemoveAiSuggestion: (id) => ipcRenderer.invoke('setup-remove-ai-suggestion', id),

  // Vérification des mises à jour de COMPOSANTS (Gradle, jadx...) — pas de
  // l'application elle-même — et réinstallation par-dessus l'existant.
  setupCheckComponentUpdates: () => ipcRenderer.invoke('setup-check-component-updates'),
  setupUpdateComponent:       (id) => ipcRenderer.invoke('setup-update-component', id),
  setupGetLastUpdateCheck:    () => ipcRenderer.invoke('setup-get-last-update-check'),

  // Recherche dans les grandes banques d'API publiques (Maven Central, npm,
  // pub.dev, F-Droid) pour enrichir une app générée avec de vraies libs/
  // plugins — recherche seule, n'installe/n'écrit jamais rien elle-même.
  // Appelée par agent-engine.js (search_public_library) et par l'onglet
  // Extensions/Composants si le client veut chercher une lib manuellement.
  searchPublicLibrary: (source, query, limit) =>
    ipcRenderer.invoke('search-public-library', { source, query, limit }),

  // IAReparateur — diagnostic seul (aperçu, ne modifie rien) et réparation
  // automatique (JDK/SDK/apktool/Gradle/keystore) — voir iareparateur.js.
  iaRepairDiagnose: (logText) => ipcRenderer.invoke('ia-repair-diagnose', logText),
  iaRepairRun: (logText, context) => ipcRenderer.invoke('ia-repair-run', { logText, context }),

  // IA locale (hors-ligne) — llama.cpp : moteur + modèles GGUF téléchargeables,
  // puis serveur local OpenAI-compatible (mêmes appels fetch que OpenRouter,
  // juste une URL http://127.0.0.1:PORT différente côté builder.html).
  aiListModels:        ()        => ipcRenderer.invoke('ai-list-models'),
  aiInstallEngine:      ()       => ipcRenderer.invoke('ai-install-engine'),
  aiInstallModel:       (id)     => ipcRenderer.invoke('ai-install-model', id),
  aiStartLocalServer:   (id)     => ipcRenderer.invoke('ai-start-local-server', id),
  aiStopLocalServer:    ()       => ipcRenderer.invoke('ai-stop-local-server'),
});
