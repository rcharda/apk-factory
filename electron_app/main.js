const { app, BrowserWindow, shell, dialog, Menu, ipcMain } = require('electron');
const { autoUpdater } = require('electron-updater');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const PORT = 7842;
let mainWindow = null;
let pythonProcess = null;
let serverReady = false;
let licensedEmail = null; // rempli une fois la licence validée

// ─── Auto-updater ─────────────────────────────────────────────────────────────
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
function findPython() {
  const candidates = ['python', 'python3', 'py'];
  // En mode packagé, chercher le python embarqué si présent
  if (app.isPackaged) {
    const embeddedPy = path.join(process.resourcesPath, 'python', 'python.exe');
    if (fs.existsSync(embeddedPy)) return embeddedPy;
  }
  return candidates[0]; // fallback : python du système
}

// ─── Trouver server.py ────────────────────────────────────────────────────────
function getServerPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'server.py');
  }
  return path.join(__dirname, '..', 'server.py');
}

// ─── Démarrer le serveur Python ───────────────────────────────────────────────
function startPythonServer() {
  return new Promise((resolve, reject) => {
    const pythonExe = findPython();
    const serverScript = getServerPath();

    if (!fs.existsSync(serverScript)) {
      reject(new Error(`server.py introuvable : ${serverScript}`));
      return;
    }

    console.log(`[APK Factory] Démarrage du serveur : ${pythonExe} ${serverScript}`);

    pythonProcess = spawn(pythonExe, [serverScript], {
      cwd: app.isPackaged ? process.resourcesPath : path.dirname(serverScript),
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    pythonProcess.stdout.on('data', (data) => {
      console.log('[Python]', data.toString().trim());
    });

    pythonProcess.stderr.on('data', (data) => {
      console.error('[Python stderr]', data.toString().trim());
    });

    pythonProcess.on('error', (err) => {
      console.error('[APK Factory] Erreur démarrage Python :', err);
      reject(err);
    });

    pythonProcess.on('exit', (code) => {
      if (code !== 0 && code !== null) {
        console.warn(`[APK Factory] Python s'est arrêté (code ${code})`);
      }
    });

    // Attendre que le serveur réponde (max 30 secondes)
    waitForServer(resolve, reject, 0);
  });
}

function waitForServer(resolve, reject, attempts) {
  if (attempts > 60) {
    reject(new Error('Le serveur Python ne répond pas après 30 secondes.'));
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
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'assets', 'icon.ico')
    : path.join(__dirname, '..', 'assets', 'icon.ico');

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

  // Au démarrage : afficher l'écran de licence (PAS le serveur Python directement)
  const licensePath = app.isPackaged
    ? path.join(process.resourcesPath, 'license.html')
    : path.join(__dirname, '..', 'license.html');

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
ipcMain.on('license-valid', async (event, { email, expiresAt }) => {
  licensedEmail = email;
  console.log(`[APK Factory] Licence validée pour ${email}, expire le ${expiresAt}`);

  if (!mainWindow) return;

  // Si le serveur Python n'est pas encore prêt, on attend qu'il le soit
  try {
    if (!serverReady) {
      mainWindow.webContents.send('license-status', { status: 'starting-server' });
      await waitForServerReady();
    }
    mainWindow.loadURL(`http://localhost:${PORT}`);
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

function waitForServerReady() {
  return new Promise((resolve, reject) => {
    if (serverReady) {
      resolve();
      return;
    }
    const checkInterval = setInterval(() => {
      if (serverReady) {
        clearInterval(checkInterval);
        resolve();
      }
    }, 300);
    setTimeout(() => {
      clearInterval(checkInterval);
      if (!serverReady) reject(new Error('Timeout : le serveur Python met trop de temps à démarrer.'));
    }, 30000);
  });
}

// ─── Cycle de vie Electron ────────────────────────────────────────────────────
app.whenReady().then(async () => {
  // Afficher l'écran de licence immédiatement
  createWindow();

  // Initialiser l'auto-updater après création de la fenêtre
  setupAutoUpdater();

  // Démarrer le serveur Python en arrière-plan, sans bloquer l'UI de licence
  startPythonServer().catch((err) => {
    console.error('[APK Factory] Échec démarrage Python en arrière-plan :', err);
    // L'erreur sera gérée au moment où license-valid tentera de basculer
  });
});

app.on('window-all-closed', () => {
  stopPythonServer();
  app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', () => {
  stopPythonServer();
});
