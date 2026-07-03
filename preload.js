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

  // Installation modulaire des composants (Gradle, Kotlin, jadx, Android SDK...)
  setupListComponents:    ()      => ipcRenderer.invoke('setup-list-components'),
  setupInstallComponents: (ids)   => ipcRenderer.invoke('setup-install-components', ids),
  onSetupProgress:        (cb)    => ipcRenderer.on('setup-progress', (_, data) => cb(data)),
  onOpenComponentsModal:  (cb)    => ipcRenderer.on('open-components-modal', () => cb()),

  // IA locale (hors-ligne) — llama.cpp : moteur + modèles GGUF téléchargeables,
  // puis serveur local OpenAI-compatible (mêmes appels fetch que OpenRouter,
  // juste une URL http://127.0.0.1:PORT différente côté builder.html).
  aiListModels:        ()        => ipcRenderer.invoke('ai-list-models'),
  aiInstallEngine:      ()       => ipcRenderer.invoke('ai-install-engine'),
  aiInstallModel:       (id)     => ipcRenderer.invoke('ai-install-model', id),
  aiStartLocalServer:   (id)     => ipcRenderer.invoke('ai-start-local-server', id),
  aiStopLocalServer:    ()       => ipcRenderer.invoke('ai-stop-local-server'),
});
