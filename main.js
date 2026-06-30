const { app, BrowserWindow, shell, dialog, Menu } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const PORT = 7842;
let mainWindow = null;
let pythonProcess = null;
let serverReady = false;

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
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
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
    ? path.join(process.resourcesPath, 'tools', 'icon.ico')
    : path.join(__dirname, '..', 'tools', 'icon.ico');

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

  // Charger l'app
  mainWindow.loadURL(`http://localhost:${PORT}`);

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

// ─── Cycle de vie Electron ────────────────────────────────────────────────────
app.whenReady().then(async () => {
  try {
    await startPythonServer();
    createWindow();
  } catch (err) {
    dialog.showErrorBox(
      'APK Factory Pro — Erreur de démarrage',
      `Impossible de démarrer le serveur Python.\n\n${err.message}\n\nVérifiez que Python 3.8+ est installé et accessible.`
    );
    app.quit();
  }
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
