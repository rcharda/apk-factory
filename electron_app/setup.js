/* =====================================================================
   setup.js — Installateur modulaire APK Factory Pro
   ---------------------------------------------------------------------
   Rôle : télécharger + installer automatiquement, sans droits admin,
   les outils nécessaires selon ce que l'utilisateur a coché dans l'UI
   (WebView/Scratch, jadx, Natif Kotlin/Java, etc.)

   Fonctionnement :
   - Chaque "composant" a une liste de MIROIRS (URLs de secours). Si le
     premier lien ne répond pas (404, timeout, mirror mort), on essaie
     automatiquement le suivant.
   - Extraction faite via des commandes CMD/PowerShell déjà présentes
     sur Windows 10/11 (Expand-Archive, puis tar.exe en secours) —
     aucune dépendance npm supplémentaire à télécharger.
   - Un fichier installed-components.json garde l'état pour ne jamais
     re-télécharger un composant déjà installé.
   ===================================================================== */

const https = require('https');
const http  = require('http');
const fs    = require('fs');
const path  = require('path');
const { execFile } = require('child_process');
const { pipeline } = require('stream/promises');

// ---------------------------------------------------------------------
// Dossier racine où tout est installé — même logique que server.py dans
// main.js : resourcesPath/tools une fois packagé (à côté de l'exe),
// sinon ./tools à la racine du projet en dev.
// ---------------------------------------------------------------------
function resolveRoot() {
  if (process.env.APKF_TOOLS_DIR) return process.env.APKF_TOOLS_DIR;
  try {
    const { app } = require('electron');
    if (app && app.isPackaged) return path.join(process.resourcesPath, 'tools');
  } catch { /* setup.js peut aussi tourner hors Electron (node setup.js en CLI) */ }
  return path.join(__dirname, 'tools');
}
const ROOT = resolveRoot();
const STATE_FILE = path.join(ROOT, 'installed-components.json');

function log(msg) {
  console.log(`[Setup] ${msg}`);
}

// ---------------------------------------------------------------------
// Registre des composants installables. Chaque entrée peut être cochée
// indépendamment dans l'UI. "urls" = miroirs essayés dans l'ordre.
// ---------------------------------------------------------------------
const COMPONENTS = {

  python: {
    label: 'Python embarqué',
    destDir: path.join(ROOT, 'python'),
    checkFile: path.join(ROOT, 'python', 'python.exe'),
    type: 'zip',
    sizeApprox: '15 Mo',
    urls: [
      'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip',
      'https://npmmirror.com/mirrors/python/3.11.9/python-3.11.9-embed-amd64.zip',
      'https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-embed-amd64.zip'
    ]
  },

  jdk: {
    label: 'Java JDK (Temurin 17)',
    destDir: path.join(ROOT, 'jdk'),
    checkFile: path.join(ROOT, 'jdk', 'bin', 'java.exe'),
    type: 'zip',
    stripTopFolder: true, // l'archive contient un dossier jdk-17.x.x+9 à la racine
    sizeApprox: '190 Mo',
    urls: [
      'https://github.com/adoptium/temurin17-binaries/releases/latest/download/OpenJDK17U-jdk_x64_windows_hotspot.zip',
      'https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse'
    ]
  },

  apktool: {
    label: 'Apktool',
    destDir: path.join(ROOT, 'apktool'),
    checkFile: path.join(ROOT, 'apktool', 'apktool.jar'),
    type: 'file', // fichier .jar unique, pas d'archive à extraire
    destFileName: 'apktool.jar',
    sizeApprox: '20 Mo',
    urls: [
      'https://github.com/iBotPeaches/Apktool/releases/latest/download/apktool.jar',
      'https://bitbucket.org/iBotPeaches/apktool/downloads/apktool.jar'
    ]
  },

  bundletool: {
    label: 'bundletool (App Bundles .aab → APK)',
    destDir: path.join(ROOT, 'bundletool'),
    checkFile: path.join(ROOT, 'bundletool', 'bundletool.jar'),
    type: 'file',
    destFileName: 'bundletool.jar',
    sizeApprox: '25 Mo',
    urls: [
      'https://github.com/google/bundletool/releases/latest/download/bundletool-all.jar'
    ]
  },

  androidSdk: {
    label: 'Android SDK (platform-tools + build-tools + platforms)',
    // sdkmanager a besoin d'un JDK pour tourner — sans ça postInstall échoue.
    dependsOn: ['jdk'],
    destDir: path.join(ROOT, 'android-sdk'),
    // Le check porte sur android.jar (platforms;android-34), pas juste sur
    // sdkmanager.bat : c'est android.jar qui manque le plus souvent et qui
    // fait échouer un build Gradle natif avec un message peu clair.
    checkFile: path.join(ROOT, 'android-sdk', 'platforms', 'android-34', 'android.jar'),
    type: 'zip',
    sizeApprox: '150 Mo (+ ~300 Mo via sdkmanager : platform-tools/build-tools/platforms)',
    urls: [
      'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip',
      'https://dl.google.com/android/repository/commandlinetools-win-10406996_latest.zip'
    ],
    // Étape post-extraction : le zip cmdline-tools seul ne suffit pas à
    // compiler quoi que ce soit. On enchaîne avec sdkmanager pour installer
    // réellement les paquets nécessaires et accepter les licences — sans
    // quoi Gradle échoue systématiquement sur un poste neuf.
    async postInstall(destDir, onProgress, id) {
      const sdkmgrCandidates = [
        path.join(destDir, 'cmdline-tools', 'bin', 'sdkmanager.bat'),
        path.join(destDir, 'bin', 'sdkmanager.bat'),
      ];
      const sdkmgr = sdkmgrCandidates.find(p => fs.existsSync(p));
      if (!sdkmgr) {
        throw new Error('sdkmanager.bat introuvable après extraction du SDK — installation incomplète.');
      }

      const jdkJava = path.join(ROOT, 'jdk', 'bin', 'java.exe');
      const env = Object.assign({}, process.env);
      if (fs.existsSync(jdkJava)) {
        env.JAVA_HOME = path.join(ROOT, 'jdk');
        env.PATH = path.join(ROOT, 'jdk', 'bin') + path.delimiter + (env.PATH || '');
      }

      const run = (args, input) => new Promise((resolve, reject) => {
        const child = execFile(sdkmgr, args, {
          cwd: destDir, env, windowsHide: true, maxBuffer: 1024 * 1024 * 32, timeout: 10 * 60 * 1000
        }, (err, stdout, stderr) => {
          if (err) return reject(new Error(stderr || err.message));
          resolve(stdout);
        });
        if (input) { child.stdin.write(input); child.stdin.end(); }
      });

      if (onProgress) onProgress({ id, status: 'downloading', pct: undefined });
      log('📜 Acceptation des licences Android SDK...');
      await run([`--sdk_root=${destDir}`, '--licenses'], 'y\n'.repeat(10));

      log('📦 Installation platform-tools + build-tools;34.0.0 + platforms;android-34...');
      await run([`--sdk_root=${destDir}`,
        'platform-tools', 'build-tools;34.0.0', 'platforms;android-34']);

      if (!fs.existsSync(path.join(destDir, 'platforms', 'android-34', 'android.jar'))) {
        throw new Error('sdkmanager a tourné mais android.jar est toujours absent (platforms;android-34).');
      }
      log('✅ Paquets Android SDK installés (android.jar présent).');
    }
  },

  gradle: {
    label: 'Gradle',
    destDir: path.join(ROOT, 'gradle'),
    checkFile: path.join(ROOT, 'gradle', 'bin', 'gradle.bat'),
    type: 'zip',
    stripTopFolder: true, // gradle-8.9/ à la racine de l'archive
    sizeApprox: '135 Mo',
    urls: [
      'https://services.gradle.org/distributions/gradle-8.9-bin.zip',
      'https://downloads.gradle-dn.com/distributions/gradle-8.9-bin.zip',
      'https://github.com/gradle/gradle-distributions/releases/download/v8.9.0/gradle-8.9-bin.zip'
    ]
  },

  // NOTE : le composant "kotlinc" (Kotlin Compiler standalone) a été retiré —
  // server.py ne l'utilise nulle part. La compilation Kotlin du pipeline
  // natif passe entièrement par Gradle (plugin org.jetbrains.kotlin.android),
  // pas par un kotlinc autonome. Le garder ici ne faisait que proposer un
  // téléchargement de ~135 Mo inutile à l'utilisateur.

  jadx: {
    label: 'jadx (décompilateur → Java lisible)',
    destDir: path.join(ROOT, 'jadx'),
    checkFile: path.join(ROOT, 'jadx', 'bin', 'jadx.bat'),
    type: 'zip',
    sizeApprox: '30 Mo',
    urls: [
      // Version fixe (le lien "latest" redirige vers un nom versionné que Node.js ne suit pas toujours)
      'https://github.com/skylot/jadx/releases/download/v1.5.1/jadx-1.5.1.zip',
      // Miroir release précédente en secours
      'https://github.com/skylot/jadx/releases/download/v1.5.0/jadx-1.5.0.zip',
      // Miroir GitHub via raw.githubusercontent CDN
      'https://objects.githubusercontent.com/github-production-release-asset-2e65be/34019800/jadx-1.5.1.zip'
    ]
  },

  nodejs: {
    label: 'Node.js (requis pour Cordova/React Native)',
    destDir: path.join(ROOT, 'nodejs'),
    checkFile: path.join(ROOT, 'nodejs', 'node.exe'),
    type: 'zip',
    stripTopFolder: true, // node-v22.23.0-win-x64/ à la racine de l'archive
    sizeApprox: '55 Mo',
    urls: [
      'https://nodejs.org/dist/v22.23.0/node-v22.23.0-win-x64.zip',
      'https://nodejs.org/dist/v20.20.2/node-v20.20.2-win-x64.zip'
    ]
  },

  flutter: {
    label: 'Flutter SDK (apps Flutter/Dart)',
    // Le zip officiel contient déjà un dossier "flutter/" à sa racine —
    // on extrait donc directement dans ROOT (pas de sous-dossier flutter/flutter).
    destDir: ROOT,
    checkFile: path.join(ROOT, 'flutter', 'bin', 'flutter.bat'),
    type: 'zip',
    // `flutter build apk` compile un vrai projet Gradle en interne : sans
    // JDK + Android SDK déjà en place, le build échoue avec une erreur
    // Gradle peu claire — même besoin réel que cordova/reactNativeCli,
    // qui déclarent déjà cette dépendance juste en dessous.
    dependsOn: ['jdk', 'androidSdk'],
    sizeApprox: '1 Go',
    urls: [
      'https://storage.googleapis.com/flutter_infra_release/releases/stable/windows/flutter_windows_3.44.2-stable.zip',
      'https://storage.flutter-io.cn/flutter_infra_release/releases/stable/windows/flutter_windows_3.44.2-stable.zip'
    ]
  },

  cordova: {
    label: 'Cordova CLI (apps hybrides packagées natif)',
    type: 'npm-global',
    npmPackage: 'cordova',
    // Cordova compile un vrai projet Android (gradlew) au build : sans
    // JDK + Android SDK déjà en place, `cordova build android` échoue
    // immédiatement — on les force donc comme prérequis réels, pas juste
    // Node.js (qui ne suffit qu'à installer le CLI lui-même).
    dependsOn: ['nodejs', 'jdk', 'androidSdk', 'gradle'],
    checkFile: path.join(ROOT, 'nodejs', 'cordova.cmd'),
    sizeApprox: '~20 Mo (+ Node.js si pas déjà installé)'
  },

  reactNativeCli: {
    label: 'React Native CLI',
    type: 'npm-global',
    npmPackage: '@react-native-community/cli',
    dependsOn: ['nodejs', 'jdk', 'androidSdk', 'gradle'],
    checkFile: path.join(ROOT, 'nodejs', 'react-native.cmd'),
    sizeApprox: '~15 Mo (+ Node.js si pas déjà installé)'
  },

  bubblewrap: {
    label: 'Bubblewrap (site web → APK/AAB via Trusted Web Activity)',
    type: 'npm-global',
    npmPackage: '@bubblewrap/cli',
    // bubblewrap appelle gradlew + apksigner en interne pour builder ;
    // il a aussi besoin d'un JDK et d'un Android SDK déjà présents
    // (server.py les lui indique via `bubblewrap updateConfig`).
    dependsOn: ['nodejs', 'jdk', 'androidSdk'],
    checkFile: path.join(ROOT, 'nodejs', 'bubblewrap.cmd'),
    sizeApprox: '~10 Mo (+ Node.js/JDK/SDK si pas déjà installés)'
  }
};

// =======================================================================
// IA locale (hors-ligne) — moteur llama.cpp (llama-server.exe, OpenAI-
// compatible sur /v1/chat/completions, exactement comme OpenRouter mais
// en local) + modèles GGUF téléchargeables à la carte.
// Registre séparé de COMPONENTS pour ne pas polluer la liste "outils
// Android" existante côté UI, mais réutilise exactement le même moteur
// de téléchargement (downloadWithFallback / extractZip / isInstalled).
// =======================================================================
const AI_ROOT = path.join(ROOT, '..', 'ai'); // resourcesPath/ai en packagé, ./ai en dev

// ---------------------------------------------------------------------
// Résolution dynamique de la dernière release GitHub d'un dépôt.
// BUG CORRIGÉ : l'URL "releases/latest/download/llama-b6100-bin-...zip"
// codait en dur le numéro de build (b6100). GitHub fait bien suivre
// "latest" vers la bonne release, MAIS le NOM du fichier change à
// chaque nouvelle version de llama.cpp (b6100 → b7075 → b7633 → ...).
// Résultat : dès qu'une nouvelle release sortait, le fichier demandé
// n'existait plus dans les assets → 404 → "erreur Node.js" affichée à
// l'installation du moteur IA locale.
// On interroge donc l'API GitHub pour lister les assets de la release
// "latest" et on choisit le bon par son motif de nom (peu importe le
// numéro de build), avec plusieurs variantes en cascade selon ce que
// le CPU de l'utilisateur supporte (avx2 la plus courante).
// ---------------------------------------------------------------------
function fetchJson(url, extraHeaders) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith('https') ? https : http;
    const req = client.get(url, {
      headers: Object.assign({ 'User-Agent': 'APKFactoryPro-Setup', 'Accept': 'application/vnd.github+json' }, extraHeaders || {})
    }, (res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
        res.resume();
        return resolve(fetchJson(res.headers.location, extraHeaders));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error(`HTTP ${res.statusCode} sur ${url}`)); }
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(new Error(`Réponse JSON invalide depuis ${url} : ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(15000, () => req.destroy(new Error(`Timeout en interrogeant ${url}`)));
  });
}

async function resolveLatestGithubAssetUrls(repo, namePatterns) {
  const release = await fetchJson(`https://api.github.com/repos/${repo}/releases/latest`);
  const assets = Array.isArray(release.assets) ? release.assets : [];
  const urls = [];
  for (const pattern of namePatterns) {
    const found = assets.filter(a => pattern.test(a.name));
    for (const a of found) if (a.browser_download_url && !urls.includes(a.browser_download_url)) urls.push(a.browser_download_url);
  }
  if (!urls.length) {
    throw new Error(`Aucun asset correspondant trouvé dans la dernière release de ${repo} (assets disponibles : ${assets.map(a => a.name).join(', ') || 'aucun'})`);
  }
  return urls;
}

// Motifs essayés dans l'ordre : AVX2 (le plus répandu sur PC récents),
// puis AVX (PC plus anciens), puis toute variante CPU générique restante.
// On ignore volontairement les variantes CUDA/ROCm/Vulkan (GPU) : le
// moteur local d'APK Factory Pro tourne en CPU pur pour rester simple
// à installer sans pilotes GPU spécifiques.
const LLAMA_CPP_ASSET_PATTERNS = [
  /^llama-b\d+-bin-win-avx2-x64\.zip$/i,
  /^llama-b\d+-bin-win-avx-x64\.zip$/i,
  /^llama-b\d+-bin-win-cpu-x64\.zip$/i,
  /^llama-b\d+-bin-win-x64\.zip$/i,
];

async function resolveLlamaServerUrls() {
  try {
    return await resolveLatestGithubAssetUrls('ggml-org/llama.cpp', LLAMA_CPP_ASSET_PATTERNS);
  } catch (e) {
    log(`⚠ Résolution dynamique de la dernière version de llama.cpp échouée (${e.message}) — utilisation des miroirs de secours.`);
    return [];
  }
}

const LOCAL_AI_ENGINE = {
  llamaServer: {
    label: 'Moteur IA locale (llama.cpp)',
    destDir: path.join(AI_ROOT, 'engine'),
    checkFile: path.join(AI_ROOT, 'engine', 'llama-server.exe'),
    type: 'zip',
    sizeApprox: '~35-60 Mo',
    // Résolu dynamiquement à chaque installation (voir installAiItem) —
    // trouve toujours le bon nom de fichier pour la release actuelle,
    // au lieu d'un numéro de build figé qui expire au fil des sorties.
    resolveUrls: resolveLlamaServerUrls,
    // Secours si l'API GitHub est inaccessible (proxy/pare-feu bloquant
    // api.github.com) : dernière version connue au moment de l'écriture.
    // Peut être obsolète — c'est pour cela que resolveUrls est prioritaire.
    urls: [
      'https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-b7075-bin-win-avx2-x64.zip'
    ]
  }
};

// ---------------------------------------------------------------------
// Plusieurs familles ET plusieurs tailles, comme demandé : l'utilisateur
// voit toutes les options avec leur poids approximatif et choisit
// librement celle qu'il télécharge (petit modèle rapide vs gros modèle
// plus capable). Tous les fichiers sont en quantization Q4_K_M (le
// meilleur compromis qualité/poids standard pour llama.cpp), servis
// depuis les dépôts GGUF de bartowski/Microsoft (mirroirs HuggingFace
// stables — les noms de fichiers n'y changent pas comme sur GitHub).
// ---------------------------------------------------------------------
const LOCAL_AI_MODELS = {
  'llama3.2-1b': {
    label: 'Llama 3.2 1B Instruct — ⚡ Ultra léger (PC très modestes)',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'llama-3.2-1b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'llama-3.2-1b-instruct-q4_k_m.gguf',
    sizeApprox: '~0.8 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf'
    ]
  },
  'qwen2.5-1.5b': {
    label: 'Qwen2.5 1.5B Instruct — ⚡ Très léger, bon FR/EN',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'qwen2.5-1.5b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'qwen2.5-1.5b-instruct-q4_k_m.gguf',
    sizeApprox: '~1 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf'
    ]
  },
  'qwen2.5-3b': {
    label: 'Qwen2.5 3B Instruct — ⚖ Léger, FR/EN, PC modestes',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'qwen2.5-3b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'qwen2.5-3b-instruct-q4_k_m.gguf',
    sizeApprox: '~2 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf',
      'https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf'
    ]
  },
  'llama3.2-3b': {
    label: 'Llama 3.2 3B Instruct — ⚖ Polyvalent, PC modestes',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'llama-3.2-3b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'llama-3.2-3b-instruct-q4_k_m.gguf',
    sizeApprox: '~2 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf'
    ]
  },
  'phi3-mini': {
    label: 'Phi-3 Mini 4K Instruct — ⚖ Rapide, bon en code',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'phi-3-mini-4k-instruct-q4.gguf'),
    type: 'file',
    destFileName: 'phi-3-mini-4k-instruct-q4.gguf',
    sizeApprox: '~2.3 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf'
    ]
  },
  'qwen2.5-coder-3b': {
    label: 'Qwen2.5 Coder 3B Instruct — ⚖ Spécialisé génération de code',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'qwen2.5-coder-3b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'qwen2.5-coder-3b-instruct-q4_k_m.gguf',
    sizeApprox: '~2 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/bartowski/Qwen2.5-Coder-3B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf'
    ]
  },
  'qwen2.5-7b': {
    label: 'Qwen2.5 7B Instruct — 🚀 Meilleure qualité, PC puissant',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'qwen2.5-7b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'qwen2.5-7b-instruct-q4_k_m.gguf',
    sizeApprox: '~4.7 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf'
    ]
  },
  'llama3.1-8b': {
    label: 'Llama 3.1 8B Instruct — 🚀 Meilleure qualité, PC puissant',
    destDir: path.join(AI_ROOT, 'models'),
    checkFile: path.join(AI_ROOT, 'models', 'llama-3.1-8b-instruct-q4_k_m.gguf'),
    type: 'file',
    destFileName: 'llama-3.1-8b-instruct-q4_k_m.gguf',
    sizeApprox: '~4.9 Go',
    contextSize: 4096,
    urls: [
      'https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'
    ]
  }
};

function isAiItemInstalled(registry, id) {
  const item = registry[id];
  return !!(item && fs.existsSync(item.checkFile));
}

async function installAiItem(registry, id, onProgress) {
  const item = registry[id];
  if (!item) throw new Error(`Élément IA inconnu : ${id}`);

  if (isAiItemInstalled(registry, id)) {
    log(`${item.label} déjà installé, on passe.`);
    if (onProgress) onProgress({ id, status: 'already-installed', pct: 100 });
    return;
  }

  fs.mkdirSync(item.destDir, { recursive: true });
  fs.mkdirSync(AI_ROOT, { recursive: true });
  const tmpFile = path.join(AI_ROOT, `_dl_${id}${item.type === 'file' ? path.extname(item.destFileName) : '.zip'}`);

  // Si l'élément fournit resolveUrls() (ex: moteur llama.cpp dont le nom de
  // fichier change à chaque release), on essaie d'abord ces URLs fraîchement
  // résolues, puis on retombe sur les URLs statiques de secours.
  let urlsToTry = item.urls || [];
  if (typeof item.resolveUrls === 'function') {
    try {
      const dynamicUrls = await item.resolveUrls();
      if (dynamicUrls && dynamicUrls.length) urlsToTry = [...dynamicUrls, ...urlsToTry];
    } catch (e) {
      log(`Résolution dynamique des URLs pour ${item.label} échouée : ${e.message}`);
    }
  }
  if (!urlsToTry.length) {
    throw new Error(`Aucune URL de téléchargement disponible pour ${item.label}.`);
  }

  if (onProgress) onProgress({ id, status: 'downloading', pct: 0 });
  await downloadWithFallback(urlsToTry, tmpFile, (pct, received, total) => {
    if (onProgress) onProgress({ id, status: 'downloading', pct: pct ?? undefined, received, total });
  });

  if (item.type === 'file') {
    fs.renameSync(tmpFile, path.join(item.destDir, item.destFileName));
  } else {
    if (onProgress) onProgress({ id, status: 'extracting', pct: 100 });
    await extractZip(tmpFile, item.destDir);
    fs.unlinkSync(tmpFile);
  }

  if (!fs.existsSync(item.checkFile)) {
    throw new Error(`Installation de ${item.label} incomplète : ${item.checkFile} introuvable.`);
  }
  log(`✅ ${item.label} installé.`);
  if (onProgress) onProgress({ id, status: 'done', pct: 100 });
}

function listLocalAiModels() {
  return Object.entries(LOCAL_AI_MODELS).map(([id, m]) => ({
    id, label: m.label, sizeApprox: m.sizeApprox,
    installed: isAiItemInstalled(LOCAL_AI_MODELS, id)
  }));
}

function isEngineInstalled() {
  return isAiItemInstalled(LOCAL_AI_ENGINE, 'llamaServer');
}

async function installEngine(onProgress) {
  return installAiItem(LOCAL_AI_ENGINE, 'llamaServer', onProgress);
}

async function installModel(modelId, onProgress) {
  return installAiItem(LOCAL_AI_MODELS, modelId, onProgress);
}

function getEnginePath() {
  return LOCAL_AI_ENGINE.llamaServer.checkFile;
}

function getModelInfo(modelId) {
  return LOCAL_AI_MODELS[modelId] || null;
}

// ---------------------------------------------------------------------
// État persistant (composants déjà installés)
// ---------------------------------------------------------------------
function readState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); }
  catch { return {}; }
}
function writeState(state) {
  fs.mkdirSync(ROOT, { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function isInstalled(id) {
  const comp = COMPONENTS[id];
  if (!comp) return false;
  if (fs.existsSync(comp.checkFile)) return true;
  return false;
}

// ---------------------------------------------------------------------
// Téléchargement d'une URL vers un fichier, avec suivi des redirections
// et rapport de progression (onProgress(pctOuNull, octetsRecus, total))
// ---------------------------------------------------------------------
function downloadOne(url, destPath, onProgress, redirectCount = 0) {
  return new Promise((resolve, reject) => {
    if (redirectCount > 8) return reject(new Error('Trop de redirections'));
    const client = url.startsWith('https') ? https : http;
    const req = client.get(url, { headers: { 'User-Agent': 'APKFactoryPro-Setup' } }, (res) => {
      // Redirection (GitHub "latest/download" etc. redirige souvent)
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
        res.resume();
        return resolve(downloadOne(res.headers.location, destPath, onProgress, redirectCount + 1));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode} sur ${url}`));
      }
      const total = parseInt(res.headers['content-length'] || '0', 10);
      let received = 0;
      const fileStream = fs.createWriteStream(destPath);
      res.on('data', (chunk) => {
        received += chunk.length;
        if (onProgress) onProgress(total ? Math.round((received / total) * 100) : null, received, total);
      });
      pipeline(res, fileStream).then(resolve).catch(reject);
    });
    req.on('error', reject);
    req.setTimeout(60000, () => req.destroy(new Error('Timeout de connexion')));
  });
}

// Essaie chaque miroir de la liste jusqu'à ce qu'un fonctionne
// Si tous les miroirs Node.js échouent, tente curl puis PowerShell
async function downloadWithFallback(urls, destPath, onProgress) {
  let lastErr = null;

  // ── Tentatives via Node.js (avec suivi de progression) ──────────────
  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    try {
      log(`Téléchargement (miroir ${i + 1}/${urls.length}) : ${url}`);
      await downloadOne(url, destPath, onProgress);
      const stat = fs.statSync(destPath);
      if (stat.size < 1024) throw new Error('Fichier téléchargé anormalement petit (lien probablement mort)');
      log(`OK — ${(stat.size / 1024 / 1024).toFixed(1)} Mo`);
      return url;
    } catch (err) {
      log(`Échec sur ce miroir : ${err.message} — tentative suivante...`);
      lastErr = err;
      try { fs.unlinkSync(destPath); } catch {}
    }
  }

  // ── Fallback système : curl ──────────────────────────────────────────
  for (const url of urls) {
    try {
      log(`Tentative curl : ${url}`);
      await new Promise((resolve, reject) => {
        const { execFile } = require('child_process');
        execFile('curl', ['-L', '--fail', '--ssl-no-revoke', '--max-redirs', '10',
          '-o', destPath, url],
          { windowsHide: true, timeout: 120000 },
          (err) => {
            if (err) return reject(err);
            try {
              if (fs.statSync(destPath).size < 1024) return reject(new Error('Fichier trop petit'));
              resolve();
            } catch(e) { reject(e); }
          });
      });
      const stat = fs.statSync(destPath);
      log(`OK via curl — ${(stat.size / 1024 / 1024).toFixed(1)} Mo`);
      return url;
    } catch (err) {
      log(`curl échoué : ${err.message}`);
      try { fs.unlinkSync(destPath); } catch {}
    }
  }

  // ── Fallback système : PowerShell ────────────────────────────────────
  for (const url of urls) {
    try {
      log(`Tentative PowerShell : ${url}`);
      await new Promise((resolve, reject) => {
        const { execFile } = require('child_process');
        const cmd = `[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;` +
          `Invoke-WebRequest -Uri '${url}' -OutFile '${destPath}' -UseBasicParsing -MaximumRedirection 10`;
        execFile('powershell.exe', ['-NoProfile', '-Command', cmd],
          { windowsHide: true, timeout: 120000 },
          (err) => {
            if (err) return reject(err);
            try {
              if (fs.statSync(destPath).size < 1024) return reject(new Error('Fichier trop petit'));
              resolve();
            } catch(e) { reject(e); }
          });
      });
      const stat = fs.statSync(destPath);
      log(`OK via PowerShell — ${(stat.size / 1024 / 1024).toFixed(1)} Mo`);
      return url;
    } catch (err) {
      log(`PowerShell échoué : ${err.message}`);
      try { fs.unlinkSync(destPath); } catch {}
    }
  }

  throw new Error(`Tous les téléchargements ont échoué. Dernière erreur Node.js : ${lastErr?.message}`);
}

// ---------------------------------------------------------------------
// Extraction d'une archive .zip via commandes Windows natives
// (aucune dépendance npm). Deux méthodes en cascade.
// ---------------------------------------------------------------------
function runCmd(cmd, args) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { windowsHide: true, maxBuffer: 1024 * 1024 * 32 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(stderr || err.message));
      resolve(stdout);
    });
  });
}

async function extractZip(zipPath, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  // Méthode 1 : PowerShell Expand-Archive (Windows 10+, pas besoin d'admin)
  try {
    await runCmd('powershell.exe', [
      '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
      `Expand-Archive -LiteralPath "${zipPath}" -DestinationPath "${destDir}" -Force`
    ]);
    return;
  } catch (err) {
    log(`Expand-Archive a échoué (${err.message}), tentative avec tar.exe...`);
  }
  // Méthode 2 (secours) : tar.exe intégré à Windows 10 1803+ (bsdtar, lit aussi les .zip)
  await runCmd('tar.exe', ['-xf', zipPath, '-C', destDir]);
}

// Si l'archive contient un seul dossier racine (ex: gradle-8.9/…),
// on "remonte" son contenu d'un cran pour que checkFile soit correct.
function flattenSingleRootFolder(destDir) {
  const entries = fs.readdirSync(destDir);
  if (entries.length === 1) {
    const inner = path.join(destDir, entries[0]);
    if (fs.statSync(inner).isDirectory()) {
      for (const f of fs.readdirSync(inner)) {
        fs.renameSync(path.join(inner, f), path.join(destDir, f));
      }
      fs.rmdirSync(inner);
    }
  }
}

// ---------------------------------------------------------------------
// Installation via npm global (Cordova, React Native CLI...) — utilise
// le npm.cmd du Node.js portable téléchargé par le composant 'nodejs'.
// Contrairement aux composants zip/file, il n'y a pas d'URL de miroir :
// npm gère lui-même le téléchargement depuis le registre npm officiel.
// ---------------------------------------------------------------------
function runNpmGlobalInstall(pkgName, onLine) {
  return new Promise((resolve, reject) => {
    const nodeDir = COMPONENTS.nodejs.destDir;
    const npmCmd = path.join(nodeDir, 'npm.cmd');
    if (!fs.existsSync(npmCmd)) {
      return reject(new Error('npm.cmd introuvable — Node.js doit être installé avant ce composant.'));
    }
    const env = Object.assign({}, process.env, {
      PATH: nodeDir + path.delimiter + (process.env.PATH || '')
    });
    const child = execFile(npmCmd, ['install', '-g', pkgName, '--no-fund', '--no-audit'],
      { cwd: nodeDir, env, windowsHide: true, maxBuffer: 1024 * 1024 * 32, timeout: 10 * 60 * 1000 },
      (err, stdout, stderr) => {
        if (onLine && stdout) stdout.split('\n').forEach(l => l.trim() && onLine(l));
        if (onLine && stderr) stderr.split('\n').forEach(l => l.trim() && onLine(l));
        if (err) return reject(new Error(stderr || err.message));
        resolve();
      });
  });
}

// ---------------------------------------------------------------------
// Installation d'un composant unique — résout d'abord ses dépendances
// (ex: cordova/reactNativeCli dépendent de nodejs) puis installe le
// composant lui-même, quel que soit son type (zip / file / npm-global).
// ---------------------------------------------------------------------
async function installComponent(id, onProgress) {
  const comp = COMPONENTS[id];
  if (!comp) throw new Error(`Composant inconnu : ${id}`);

  if (isInstalled(id)) {
    log(`${comp.label} déjà installé, on passe.`);
    if (onProgress) onProgress({ id, status: 'already-installed', pct: 100 });
    return;
  }

  // Dépendances (ex: nodejs avant cordova) — installées en premier,
  // silencieusement, avant même de télécharger le composant demandé.
  if (comp.dependsOn) {
    for (const depId of comp.dependsOn) {
      if (!isInstalled(depId)) {
        log(`${comp.label} nécessite ${COMPONENTS[depId]?.label || depId} — installation préalable...`);
        await installComponent(depId, onProgress);
      }
    }
  }

  if (comp.type === 'npm-global') {
    if (onProgress) onProgress({ id, status: 'downloading', pct: undefined });
    log(`📦 Installation npm global : ${comp.npmPackage}...`);
    await runNpmGlobalInstall(comp.npmPackage, (line) => log(`  ${line}`));
    if (!fs.existsSync(comp.checkFile)) {
      throw new Error(`Installation de ${comp.label} incomplète : ${comp.checkFile} introuvable après npm install.`);
    }
    const state = readState();
    state[id] = { installedAt: new Date().toISOString(), source: `npm:${comp.npmPackage}` };
    writeState(state);
    log(`✅ ${comp.label} installé.`);
    if (onProgress) onProgress({ id, status: 'done', pct: 100 });
    return;
  }

  fs.mkdirSync(comp.destDir, { recursive: true });
  const tmpFile = path.join(ROOT, `_dl_${id}${comp.type === 'file' ? path.extname(comp.destFileName || '.tmp') : '.zip'}`);

  if (onProgress) onProgress({ id, status: 'downloading', pct: 0 });

  const usedUrl = await downloadWithFallback(comp.urls, tmpFile, (pct, received, total) => {
    if (onProgress) onProgress({ id, status: 'downloading', pct: pct ?? undefined, received, total });
  });

  if (comp.type === 'file') {
    fs.renameSync(tmpFile, path.join(comp.destDir, comp.destFileName));
  } else {
    if (onProgress) onProgress({ id, status: 'extracting', pct: 100 });
    await extractZip(tmpFile, comp.destDir);
    if (comp.stripTopFolder) flattenSingleRootFolder(comp.destDir);
    fs.unlinkSync(tmpFile);
  }

  // Étape optionnelle après extraction (ex: androidSdk → sdkmanager pour
  // installer réellement platform-tools/build-tools/platforms, pas juste
  // les cmdline-tools). checkFile n'est validé qu'APRÈS cette étape,
  // puisque c'est souvent elle qui produit le fichier attendu.
  if (comp.postInstall) {
    log(`⚙ ${comp.label} : étape post-installation (sdkmanager)...`);
    await comp.postInstall(comp.destDir, onProgress, id);
  }

  if (!fs.existsSync(comp.checkFile)) {
    throw new Error(`Installation de ${comp.label} incomplète : ${comp.checkFile} introuvable après extraction.`);
  }

  const state = readState();
  state[id] = { installedAt: new Date().toISOString(), source: usedUrl };
  writeState(state);

  log(`✅ ${comp.label} installé.`);
  if (onProgress) onProgress({ id, status: 'done', pct: 100 });
}

// ---------------------------------------------------------------------
// Installation d'une liste de composants (ceux cochés par l'utilisateur)
// ---------------------------------------------------------------------
async function installComponents(ids, onProgress) {
  const results = {};
  for (const id of ids) {
    try {
      await installComponent(id, onProgress);
      results[id] = { ok: true };
    } catch (err) {
      log(`❌ Échec installation ${id} : ${err.message}`);
      results[id] = { ok: false, error: err.message };
      if (onProgress) onProgress({ id, status: 'error', error: err.message });
    }
  }
  return results;
}

function listComponents() {
  return Object.entries(COMPONENTS).map(([id, c]) => ({
    id,
    label: c.label,
    sizeApprox: c.sizeApprox,
    installed: isInstalled(id)
  }));
}

module.exports = {
  COMPONENTS,
  listComponents,
  isInstalled,
  installComponent,
  installComponents,
  // IA locale
  listLocalAiModels,
  isEngineInstalled,
  installEngine,
  installModel,
  getEnginePath,
  getModelInfo
};

// ---------------------------------------------------------------------
// Utilisation en ligne de commande directe (CMD) :
//   node setup.js jdk apktool gradle
// Installe uniquement les composants listés en argument. Sans argument,
// affiche juste la liste des composants disponibles et leur état.
// ---------------------------------------------------------------------
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.log('Composants disponibles :');
    for (const c of listComponents()) {
      console.log(`  - ${c.id.padEnd(12)} ${c.label} (${c.sizeApprox})${c.installed ? '  [déjà installé]' : ''}`);
    }
    console.log('\nUsage : node setup.js <id1> <id2> ...');
    process.exit(0);
  }
  installComponents(args, (p) => {
    if (p.status === 'downloading' && p.pct != null) {
      process.stdout.write(`\r[${p.id}] téléchargement... ${p.pct}%   `);
    } else if (p.status === 'extracting') {
      process.stdout.write(`\r[${p.id}] extraction...           \n`);
    } else if (p.status === 'done') {
      process.stdout.write(`\r[${p.id}] terminé.                \n`);
    } else if (p.status === 'error') {
      process.stdout.write(`\r[${p.id}] ERREUR : ${p.error}\n`);
    }
  }).then(() => {
    log('Terminé.');
  });
}
