// preload.js — bridge sécurisé entre le process Electron et la page web
// contextIsolation: true → seul ce fichier peut exposer des APIs Node.js à la page

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Version de l'app
  getVersion: () => ipcRenderer.invoke('get-version'),

  // Licence : appelé par license-renderer.js quand une licence valide est confirmée
  licenseValid: (email, expiresAt) => ipcRenderer.send('license-valid', { email, expiresAt }),

  // Ouvrir un lien dans le vrai navigateur (paiement FedaPay, support, etc.)
  openExternal: (url) => ipcRenderer.send('open-external', url),
});
