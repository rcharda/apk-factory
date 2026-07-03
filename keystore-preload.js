// keystore-preload.js
// Preload DÉDIÉ à la fenêtre modale de configuration de la signature de
// production. Volontairement séparé du preload principal (celui utilisé par
// builder.html / l'agent IA) : cette fenêtre est la SEULE à pouvoir déclencher
// la génération/l'import du keystore et la saisie du mot de passe. L'agent IA
// n'a et n'aura jamais accès à ces méthodes.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('keystoreSetupAPI', {
  pickFile: () => ipcRenderer.invoke('keystore-setup-pick-file'),
  generate: (payload) => ipcRenderer.invoke('keystore-setup-generate', payload),
  importExisting: (payload) => ipcRenderer.invoke('keystore-setup-import', payload),
  cancel: () => ipcRenderer.invoke('keystore-setup-cancel'),
});
