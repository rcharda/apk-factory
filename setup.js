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

// Avec shell:true sur Windows, Node se contente de coller [file, ...args]
// avec des espaces avant de les passer à cmd.exe — sans jamais ajouter de
// guillemets automatiquement. Un chemin contenant un espace (ex: dossier
// utilisateur "John Doe", ou "APK Factory Pro" dans %APPDATA%) casserait
// donc la ligne de commande. On quote ici nous-mêmes chaque élément qui en
// a besoin avant de le donner à execFile.
function winShellQuote(arg) {
  const s = String(arg);
  return /[\s"]/.test(s) ? `"${s.replace(/"/g, '\\"')}"` : s;
}

// ---------------------------------------------------------------------
// Dossier racine où tout est installé — même logique que server.py dans
// main.js : resourcesPath/tools une fois packagé (à côté de l'exe),
// sinon ./tools à la racine du projet en dev.
// ---------------------------------------------------------------------
function resolveRoot() {
  if (process.env.APKF_TOOLS_DIR) return process.env.APKF_TOOLS_DIR;
  try {
    const { app } = require('electron');
    if (app && app.isPackaged) {
      // IMPORTANT : on n'utilise PAS resourcesPath/tools ici.
      // resourcesPath se trouve dans le dossier d'installation du
      // programme (ex: AppData\Local\Programs\... ou Program Files),
      // qui est en lecture seule pour un compte non-admin. Tout
      // téléchargement de composant (npm install, unzip...) qui tente
      // d'y écrire échoue silencieusement (ex: react-native.cmd jamais
      // créé). On redirige donc les composants TÉLÉCHARGEABLES vers un
      // dossier utilisateur toujours inscriptible, qui en plus SURVIT à
      // une désinstallation du programme (voulu : le client ne re-télécharge
      // pas 500 Mo de composants à chaque réinstallation/mise à jour).
      const userDataDir = app.getPath('userData'); // ex: %APPDATA%\apk-factory-pro
      const writableRoot = path.join(userDataDir, 'tools');
      seedFromBundledResourcesOnce(writableRoot);
      return writableRoot;
    }
  } catch { /* setup.js peut aussi tourner hors Electron (node setup.js en CLI) */ }
  return path.join(__dirname, 'tools');
}

// Copie UNE SEULE FOIS (si absent) les outils déjà embarqués dans
// l'installeur (apktool.jar, apktool.bat, debug.keystore, android-sdk
// build-tools — voir extraResources dans package.json) depuis le dossier
// programme en lecture seule vers le dossier utilisateur inscriptible.
// Sans ça, ces outils "de base" ne seraient plus trouvés du tout après
// avoir déplacé ROOT.
function seedFromBundledResourcesOnce(writableRoot) {
  try {
    const marker = path.join(writableRoot, 'apktool.jar');
    if (fs.existsSync(marker)) return; // déjà migré, rien à refaire

    const bundledDir = path.join(process.resourcesPath, 'tools');
    if (!fs.existsSync(bundledDir)) return; // rien à copier (dev, ou build sans tools embarqués)

    fs.mkdirSync(writableRoot, { recursive: true });
    copyRecursiveSync(bundledDir, writableRoot);
    log(`Outils de base copiés depuis ${bundledDir} vers ${writableRoot}`);
  } catch (e) {
    log(`[AVERT] Echec de la copie initiale des outils embarqués : ${e.message}`);
  }
}

function copyRecursiveSync(src, dest) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const entry of fs.readdirSync(src)) {
      copyRecursiveSync(path.join(src, entry), path.join(dest, entry));
    }
  } else {
    fs.copyFileSync(src, dest);
  }
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
      'https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar'
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
      'https://github.com/google/bundletool/releases/download/1.18.3/bundletool-all-1.18.3.jar',
      'https://github.com/google/bundletool/releases/download/1.18.0/bundletool-all-1.18.0.jar'
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
        // shell:true est indispensable ici : Node refuse d'exécuter un .bat
        // directement depuis Node 17+ (mesure de sécurité), ce qui provoque
        // sinon une erreur "spawn EINVAL" sur Windows.
        const child = execFile(winShellQuote(sdkmgr), args.map(winShellQuote), {
          cwd: destDir, env, windowsHide: true, shell: true, maxBuffer: 1024 * 1024 * 32, timeout: 10 * 60 * 1000
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
    // BUG CORRIGÉ : @react-native-community/cli installe un binaire nommé
    // "rnc-cli" (pas "react-native" — c'était l'ancien paquet déprécié
    // "react-native-cli" qui s'appelait ainsi). npm install réussissait donc
    // réellement, mais checkFile pointait vers un fichier qui n'existe plus
    // dans ce paquet, ce qui faisait échouer l'installation à tort après coup.
    checkFile: path.join(ROOT, 'nodejs', 'rnc-cli.cmd'),
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
  },

  // ── Wrappers WebView additionnels (bêta) ────────────────────────────
  // Même famille que cordova/reactNativeCli : CLI installés globalement
  // via npm, réutilisent le Node.js portable déjà téléchargé. Marqués
  // « bêta » car non testés avec les vrais binaires (ns/titanium) dans
  // l'environnement de dev — voir agent-engine.js pour le détail des
  // pipelines create/build correspondants.

  nativescript: {
    label: 'NativeScript CLI (WebView natif — bêta)',
    type: 'npm-global',
    npmPackage: 'nativescript',
    // `ns build android` compile un vrai projet Gradle en interne, comme
    // cordova/reactNativeCli — mêmes prérequis réels.
    dependsOn: ['nodejs', 'jdk', 'androidSdk', 'gradle'],
    checkFile: path.join(ROOT, 'nodejs', 'ns.cmd'),
    sizeApprox: '~30 Mo (+ Node.js/JDK/SDK/Gradle si pas déjà installés)'
  },

  titanium: {
    label: 'Titanium CLI (Appcelerator — WebView natif — bêta)',
    type: 'npm-global',
    npmPackage: 'titanium',
    // `titanium build -p android` a besoin d'un JDK + Android SDK déjà en
    // place (pas de Gradle : Titanium utilise son propre système de build).
    dependsOn: ['nodejs', 'jdk', 'androidSdk'],
    checkFile: path.join(ROOT, 'nodejs', 'titanium.cmd'),
    sizeApprox: '~15 Mo (+ Node.js/JDK/SDK si pas déjà installés)'
  },

  dotnetMaui: {
    label: '.NET SDK 8 + workload MAUI Android (WebView natif — bêta)',
    // Contrairement à cordova/nativescript/titanium (npm), .NET MAUI a
    // besoin du SDK .NET lui-même (pas Node.js) + d'un workload additionnel
    // installé après coup — pas disponible via un simple npm install.
    dependsOn: ['jdk', 'androidSdk'],
    destDir: path.join(ROOT, 'dotnet'),
    checkFile: path.join(ROOT, 'dotnet', 'dotnet.exe'),
    type: 'zip',
    sizeApprox: '~200 Mo (+ ~300 Mo pour le workload maui-android via postInstall)',
    urls: [
      'https://dotnetcli.azureedge.net/dotnet/Sdk/8.0.404/dotnet-sdk-8.0.404-win-x64.zip',
      'https://builds.dotnet.microsoft.com/dotnet/Sdk/8.0.404/dotnet-sdk-8.0.404-win-x64.zip'
    ],
    // Étape post-extraction : le SDK de base ne suffit pas à `dotnet new
    // maui` / `dotnet build -f net8.0-android` — il faut en plus le workload
    // maui-android, pas installé par défaut. Même logique que androidSdk
    // (sdkmanager) : checkFile (dotnet.exe) existe déjà après extraction,
    // mais on vérifie ICI en plus que le workload est bien présent, sans
    // quoi le premier `dotnet build` échouerait avec une erreur peu claire
    // ("workload not installed") beaucoup plus tard, au moment du build.
    async postInstall(destDir, onProgress, id) {
      const dotnetExe = path.join(destDir, 'dotnet.exe');
      const env = Object.assign({}, process.env, {
        DOTNET_ROOT: destDir,
        PATH: destDir + path.delimiter + (process.env.PATH || ''),
        DOTNET_CLI_TELEMETRY_OPTOUT: '1',
        DOTNET_NOLOGO: '1',
      });
      const run = (args) => new Promise((resolve, reject) => {
        // shell:true pour la même raison que sdkmanager.bat plus haut —
        // cohérence Windows/espaces dans les chemins.
        execFile(winShellQuote(dotnetExe), args.map(winShellQuote),
          { cwd: destDir, env, windowsHide: true, shell: true, maxBuffer: 1024 * 1024 * 32, timeout: 15 * 60 * 1000 },
          (err, stdout, stderr) => {
            if (err) return reject(new Error(stderr || err.message));
            resolve(stdout);
          });
      });

      if (onProgress) onProgress({ id, status: 'downloading', pct: undefined, message: '⏳ Installation du workload maui-android (peut prendre plusieurs minutes)...' });
      log('📦 Installation du workload .NET MAUI (Android)...');
      await run(['workload', 'install', 'maui-android', '--skip-manifest-update']);

      const listOut = await run(['workload', 'list']);
      if (!/maui-android/i.test(listOut)) {
        throw new Error('Le workload maui-android ne semble pas installé après `dotnet workload install` (voir logs ci-dessus).');
      }
      log('✅ Workload maui-android installé et vérifié.');
    }
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
  await downloadWithFallback(urlsToTry, tmpFile, (pct, received, total, message) => {
    if (onProgress) onProgress({ id, status: 'downloading', pct: pct ?? undefined, received, total, message });
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

  // notifyMsg() : remonte un message texte "temps réel" à l'UI (via le même
  // canal onProgress que la progression numérique), en plus du log console.
  // Sans ça, l'utilisateur ne voit rien bouger tant que TOUS les miroirs
  // n'ont pas échoué — il ne sait jamais quel miroir est essayé ni pourquoi
  // il a échoué avant l'erreur finale.
  const notifyMsg = (msg) => {
    log(msg);
    if (onProgress) onProgress(undefined, undefined, undefined, msg);
  };

  // ── Tentatives via Node.js (avec suivi de progression) ──────────────
  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    try {
      notifyMsg(`Miroir ${i + 1}/${urls.length} : ${url}`);
      await downloadOne(url, destPath, onProgress);
      const stat = fs.statSync(destPath);
      if (stat.size < 1024) throw new Error('Fichier téléchargé anormalement petit (lien probablement mort)');
      notifyMsg(`OK — ${(stat.size / 1024 / 1024).toFixed(1)} Mo`);
      return url;
    } catch (err) {
      notifyMsg(`❌ Échec miroir ${i + 1}/${urls.length} : ${err.message}`);
      lastErr = err;
      try { fs.unlinkSync(destPath); } catch {}
    }
  }

  // ── Fallback système : curl ──────────────────────────────────────────
  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    try {
      notifyMsg(`Tentative curl (${i + 1}/${urls.length})...`);
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
      notifyMsg(`OK via curl — ${(stat.size / 1024 / 1024).toFixed(1)} Mo`);
      return url;
    } catch (err) {
      notifyMsg(`❌ curl échoué (${i + 1}/${urls.length}) : ${err.message}`);
      lastErr = err;
      try { fs.unlinkSync(destPath); } catch {}
    }
  }

  // ── Fallback système : PowerShell ────────────────────────────────────
  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    try {
      notifyMsg(`Tentative PowerShell (${i + 1}/${urls.length})...`);
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
      notifyMsg(`OK via PowerShell — ${(stat.size / 1024 / 1024).toFixed(1)} Mo`);
      return url;
    } catch (err) {
      notifyMsg(`❌ PowerShell échoué (${i + 1}/${urls.length}) : ${err.message}`);
      lastErr = err;
      try { fs.unlinkSync(destPath); } catch {}
    }
  }

  throw new Error(`Tous les téléchargements ont échoué (${urls.length} miroir(s) × 3 méthodes). Dernière erreur : ${lastErr?.message}`);
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
// Registres npm essayés dans l'ordre — si le registre officiel est
// inaccessible (réseau/pare-feu/région), on retente automatiquement sur un
// miroir avant d'abandonner, exactement comme pour les téléchargements zip.
const NPM_REGISTRIES = [
  'https://registry.npmjs.org/',
  'https://registry.npmmirror.com/',
];

function runNpmGlobalInstallOnce(pkgName, registry, onLine) {
  return new Promise((resolve, reject) => {
    const nodeDir = COMPONENTS.nodejs.destDir;
    const npmCmd = path.join(nodeDir, 'npm.cmd');
    if (!fs.existsSync(npmCmd)) {
      return reject(new Error('npm.cmd introuvable — Node.js doit être installé avant ce composant.'));
    }
    // BUG CORRIGÉ : sans forcer explicitement le préfixe global, npm peut
    // écrire les shims (.cmd) ailleurs que nodeDir (ex: %APPDATA%\npm) si
    // un .npmrc préexistant sur la machine cliente définit déjà un autre
    // "prefix". npm install -g réussissait alors (exit code 0) mais
    // checkFile (nodeDir/react-native.cmd) restait introuvable.
    //
    // BUG CORRIGÉ (2) : userconfig et globalconfig pointaient vers EXACTEMENT
    // le même fichier. npm charge ces deux réglages comme deux configs
    // distinctes ("user" puis "global") et refuse ensuite de charger un
    // fichier déjà chargé sous un autre "type" — erreur immédiate
    // "double-loading config ... as global, previously loaded as user",
    // avant même que l'installation ne démarre. Chaque config vide doit
    // donc être un fichier séparé.
    const env = Object.assign({}, process.env, {
      PATH: nodeDir + path.delimiter + (process.env.PATH || ''),
      npm_config_prefix: nodeDir,
      npm_config_userconfig: path.join(nodeDir, '.npmrc-apkfactory-empty-user'),
      npm_config_globalconfig: path.join(nodeDir, '.npmrc-apkfactory-empty-global'),
    });
    const args = ['install', '-g', pkgName, '--prefix', nodeDir, '--no-fund', '--no-audit', '--registry', registry];
    const child = execFile(winShellQuote(npmCmd), args.map(winShellQuote),
      { cwd: nodeDir, env, windowsHide: true, shell: true, maxBuffer: 1024 * 1024 * 32, timeout: 10 * 60 * 1000 },
      (err, stdout, stderr) => {
        if (onLine && stdout) stdout.split('\n').forEach(l => l.trim() && onLine(l));
        if (onLine && stderr) stderr.split('\n').forEach(l => l.trim() && onLine(l));
        if (err) return reject(new Error(stderr || err.message));
        resolve();
      });
  });
}

async function runNpmGlobalInstall(pkgName, onLine) {
  let lastErr = null;
  for (let i = 0; i < NPM_REGISTRIES.length; i++) {
    const registry = NPM_REGISTRIES[i];
    try {
      if (onLine) onLine(`Registre npm ${i + 1}/${NPM_REGISTRIES.length} : ${registry}`);
      await runNpmGlobalInstallOnce(pkgName, registry, onLine);
      if (onLine) onLine(`OK via ${registry}`);
      return;
    } catch (err) {
      if (onLine) onLine(`❌ Échec registre ${registry} : ${err.message}`);
      lastErr = err;
    }
  }
  throw new Error(`Installation npm de ${pkgName} échouée sur tous les registres (${NPM_REGISTRIES.length}). Dernière erreur : ${lastErr?.message}`);
}

// ---------------------------------------------------------------------
// Installation d'un composant unique — résout d'abord ses dépendances
// (ex: cordova/reactNativeCli dépendent de nodejs) puis installe le
// composant lui-même, quel que soit son type (zip / file / npm-global).
// ---------------------------------------------------------------------
async function installComponent(id, onProgress, opts) {
  const force = !!(opts && opts.force);
  const comp = COMPONENTS[id];
  if (!comp) throw new Error(`Composant inconnu : ${id}`);

  if (isInstalled(id) && !force) {
    log(`${comp.label} déjà installé, on passe.`);
    if (onProgress) onProgress({ id, status: 'already-installed', pct: 100 });
    return;
  }

  // Dépendances (ex: nodejs avant cordova) — installées en premier,
  // silencieusement, avant même de télécharger le composant demandé.
  if (comp.dependsOn) {
    for (const depId of comp.dependsOn) {
      if (!isInstalled(depId)) {
        const depLabel = COMPONENTS[depId]?.label || depId;
        log(`${comp.label} nécessite ${depLabel} — installation préalable...`);
        if (onProgress) onProgress({ id, status: 'downloading', message: `⏳ Dépendance requise : ${depLabel} — installation en cours...` });
        await installComponent(depId, onProgress);
        if (onProgress) onProgress({ id, status: 'downloading', message: `✅ ${depLabel} prêt — poursuite de ${comp.label}...` });
      }
    }
  }

  if (comp.type === 'npm-global') {
    if (onProgress) onProgress({ id, status: 'downloading', pct: undefined, message: `Installation npm : ${comp.npmPackage}...` });
    log(`📦 Installation npm global : ${comp.npmPackage}...`);
    await runNpmGlobalInstall(comp.npmPackage, (line) => {
      log(`  ${line}`);
      if (onProgress) onProgress({ id, status: 'downloading', pct: undefined, message: line });
    });
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

  const usedUrl = await downloadWithFallback(comp.urls, tmpFile, (pct, received, total, message) => {
    if (onProgress) onProgress({ id, status: 'downloading', pct: pct ?? undefined, received, total, message });
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
async function installComponents(ids, onProgress, opts) {
  const results = {};
  for (const id of ids) {
    try {
      await installComponent(id, onProgress, opts);
      results[id] = { ok: true };
    } catch (err) {
      log(`❌ Échec installation ${id} : ${err.message}`);
      results[id] = { ok: false, error: err.message };
      if (onProgress) onProgress({ id, status: 'error', error: err.message });
    }
  }
  return results;
}

// Réinstalle un composant déjà présent par-dessus l'existant (utilisé par
// "Vérifier les mises à jour" quand une version plus récente a été trouvée
// sur GitHub). On vide d'abord son dossier pour éviter qu'un vieux fichier
// de l'ancienne version ne traîne à côté du nouveau.
async function updateComponent(id, onProgress) {
  const comp = COMPONENTS[id];
  if (!comp) throw new Error(`Composant inconnu : ${id}`);
  if (comp.destDir && fs.existsSync(comp.destDir) && comp.type !== 'npm-global') {
    fs.rmSync(comp.destDir, { recursive: true, force: true });
  }
  await installComponent(id, onProgress, { force: true });
}

// =======================================================================
// VÉRIFICATION DES MISES À JOUR DE COMPOSANTS (pas du logiciel lui-même) —
// compare, pour chaque composant dont l'URL pointe vers une release GitHub
// versionnée en dur (gradle, jadx...), la version actuellement codée dans
// COMPONENTS avec la dernière release publiée sur GitHub. Les composants
// dont l'URL utilise déjà "latest/download" (jdk, apktool, bundletool)
// sont toujours à jour à l'installation et ne sont pas listés ici.
// =======================================================================
const COMPONENT_UPDATE_SOURCES = {
  gradle: { repo: 'gradle/gradle', versionRegex: /gradle-([\d.]+)-bin\.zip/ },
  jadx:   { repo: 'skylot/jadx',   versionRegex: /v([\d.]+)\/jadx-[\d.]+\.zip/ },
};

function _compareVersions(a, b) {
  const pa = String(a).replace(/^v/i, '').split('.').map(n => parseInt(n, 10) || 0);
  const pb = String(b).replace(/^v/i, '').split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d !== 0) return d < 0 ? -1 : 1;
  }
  return 0;
}

async function checkComponentUpdates() {
  const results = [];
  for (const [id, src] of Object.entries(COMPONENT_UPDATE_SOURCES)) {
    const comp = COMPONENTS[id];
    if (!comp || !isInstalled(id)) continue; // pas installé → rien à mettre à jour
    const currentUrl = comp.urls.find(u => src.versionRegex.test(u)) || comp.urls[0];
    const currentMatch = currentUrl.match(src.versionRegex);
    const currentVersion = currentMatch ? currentMatch[1] : null;
    try {
      const release = await _httpsGetJson(`https://api.github.com/repos/${src.repo}/releases/latest`);
      const latestVersion = String(release.tag_name || '').replace(/^v/i, '');
      const updateAvailable = currentVersion ? _compareVersions(currentVersion, latestVersion) < 0 : false;
      results.push({
        id, label: comp.label,
        currentVersion: currentVersion || '?',
        latestVersion: latestVersion || '?',
        updateAvailable,
        releaseUrl: release.html_url || `https://github.com/${src.repo}/releases`,
      });
    } catch (err) {
      results.push({ id, label: comp.label, currentVersion: currentVersion || '?', latestVersion: null, updateAvailable: false, error: err.message });
    }
  }
  // Persisté pour que builder.html puisse afficher le dernier état connu
  // dès l'ouverture, sans attendre un nouveau check réseau (utile hors
  // ligne ou au tout premier rendu avant que le check async ne revienne).
  const state = readState();
  state.lastUpdateCheck = { checkedAt: new Date().toISOString(), results };
  writeState(state);
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

// =======================================================================
// COMPOSANTS TROUVÉS PAR L'IA (hors registre COMPONENTS ci-dessus)
// -----------------------------------------------------------------------
// Le client a demandé : si un outil requis n'est pas dans la liste connue,
// que l'IA le cherche sur GitHub et l'affiche comme téléchargeable. Ces
// deux fonctions ne font QUE ça — chercher, puis installer SEULEMENT sur
// action explicite du client dans l'UI (jamais depuis install_components,
// qui reste volontairement limité au registre COMPONENTS ci-dessus).
// =======================================================================
const DYNAMIC_ROOT = path.join(ROOT, 'ai-components');

// Domaines dont on accepte les liens de téléchargement. Historiquement
// limité aux domaines GitHub, mais le prompt IA (aiSuggestComponents() dans
// builder.html) demande une source "de préférence" GitHub — pas
// exclusivement — donc l'IA peut légitimement renvoyer un lien SourceForge,
// GitLab, ou le site officiel d'un outil connu. Avec la whitelist GitHub-only,
// TOUTE suggestion non-GitHub échouait systématiquement à l'installation
// ("Source refusée"), ce qui donnait l'impression que "rien ne se passe"
// côté client. On élargit donc aux hébergeurs de confiance les plus
// courants pour ce type d'outils, tout en gardant un principe de liste
// blanche stricte (jamais un domaine arbitraire).
const ALLOWED_DYNAMIC_HOSTS = [
  'github.com',
  'objects.githubusercontent.com',
  'raw.githubusercontent.com',
  'codeload.github.com',
  'gitlab.com',
  'sourceforge.net',
  'downloads.sourceforge.net',
  'bitbucket.org',
  'apache.org',
  'downloads.apache.org',
  'maven.apache.org',
];

function _isAllowedDynamicUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && ALLOWED_DYNAMIC_HOSTS.some(h => u.hostname === h || u.hostname.endsWith('.' + h));
  } catch { return false; }
}

// Interroge l'API publique GitHub (aucune authentification, lecture seule)
// pour trouver le dépôt le plus pertinent pour `query`, puis regarde sa
// dernière release pour proposer un asset téléchargeable. Ne télécharge
// rien : renvoie juste un candidat que l'humain devra valider dans l'UI.
function _httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      headers: { 'User-Agent': 'APKFactoryPro-Setup', 'Accept': 'application/vnd.github+json' }
    }, (res) => {
      if (res.statusCode !== 200) { res.resume(); return reject(new Error(`HTTP ${res.statusCode} sur ${url}`)); }
      let data = '';
      res.on('data', (c) => data += c);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.setTimeout(15000, () => req.destroy(new Error('Timeout GitHub API')));
  });
}

async function searchGithubReleaseAsset(query) {
  const q = String(query || '').trim();
  if (!q) return { ok: false, error: 'Recherche vide.' };
  log(`🔎 Recherche GitHub pour « ${q} »...`);
  try {
    const search = await _httpsGetJson(
      'https://api.github.com/search/repositories?sort=stars&order=desc&per_page=5&q=' + encodeURIComponent(q)
    );
    const repos = (search.items || []);
    if (!repos.length) return { ok: false, error: `Aucun dépôt GitHub trouvé pour « ${q} ».` };

    for (const repo of repos) {
      let release;
      try {
        release = await _httpsGetJson(`https://api.github.com/repos/${repo.full_name}/releases/latest`);
      } catch { continue; } // pas de release publiée sur ce dépôt, on essaie le suivant

      const assets = release.assets || [];
      // Priorité : archive/exécutable Windows explicite, sinon n'importe
      // quel .zip/.jar/.exe (beaucoup d'outils CLI publient un seul asset
      // multi-plateforme), jamais le tarball source auto-généré seul.
      const pick = assets.find(a => /win|windows/i.test(a.name) && /\.(zip|exe)$/i.test(a.name))
        || assets.find(a => /\.(zip|jar|exe)$/i.test(a.name));
      if (!pick) continue;
      if (!_isAllowedDynamicUrl(pick.browser_download_url)) continue;

      const suggestion = {
        ok: true,
        id: 'ai_' + repo.name.toLowerCase().replace(/[^a-z0-9]+/g, '_'),
        label: `${repo.name} (${pick.name})`,
        url: pick.browser_download_url,
        repoUrl: repo.html_url,
        sizeApprox: pick.size ? `${(pick.size / 1024 / 1024).toFixed(1)} Mo` : '',
        type: /\.zip$/i.test(pick.name) ? 'zip' : 'file',
        assetName: pick.name,
      };
      saveAiSuggestion(suggestion); // persisté : visible même après redémarrage de l'app
      return suggestion;
    }
    return { ok: false, error: `Dépôts trouvés pour « ${q} » mais aucun n'a de release Windows exploitable.` };
  } catch (err) {
    return { ok: false, error: `Recherche GitHub échouée : ${err.message}` };
  }
}

// ---------------------------------------------------------------------
// Suggestions IA en attente — persistées dans STATE_FILE (clé
// 'aiSuggestions') pour que le client ne les perde PAS s'il ferme
// l'app avant d'avoir coché/installé le composant proposé. Sans ça,
// une suggestion trouvée par l'IA (searchGithubReleaseAsset) ne vivait
// qu'en mémoire côté renderer (builder.html) : redémarrage = perdue,
// obligeant l'IA à re-chercher sur GitHub à chaque session.
// ---------------------------------------------------------------------
function saveAiSuggestion(def) {
  if (!def || !def.id) return;
  const state = readState();
  state.aiSuggestions = state.aiSuggestions || {};
  state.aiSuggestions[def.id] = { ...def, foundAt: new Date().toISOString() };
  writeState(state);
}

function listAiSuggestions() {
  const state = readState();
  return Object.values(state.aiSuggestions || {});
}

function removeAiSuggestion(id) {
  const state = readState();
  if (state.aiSuggestions && state.aiSuggestions[id]) {
    delete state.aiSuggestions[id];
    writeState(state);
  }
}

// Installe un composant décrit par l'IA (voir def ci-dessus) — même pipeline
// de téléchargement que les composants officiels (miroirs/curl/PowerShell en
// secours), mais toujours appelé uniquement après confirmation explicite du
// client dans l'UI (voir startComponentsInstall() dans builder.html).
async function installDynamicComponent(def, onProgress) {
  const url = def && (def.url || def.downloadUrl);
  if (!def || !def.id || !url) throw new Error('Définition de composant invalide.');
  def = { ...def, url };
  if (!_isAllowedDynamicUrl(def.url)) {
    throw new Error(`Source refusée : seuls les liens GitHub officiels sont acceptés (reçu : ${def.url}).`);
  }
  const id = def.id;
  const destDir = path.join(DYNAMIC_ROOT, id);
  fs.mkdirSync(destDir, { recursive: true });
  const isZip = def.type === 'zip';
  const destFileName = def.assetName || (isZip ? 'package.zip' : path.basename(new URL(def.url).pathname) || 'component.bin');
  const tmpFile = path.join(DYNAMIC_ROOT, `_dl_${id}${path.extname(destFileName) || '.tmp'}`);

  if (onProgress) onProgress({ id, status: 'downloading', pct: 0 });
  await downloadWithFallback([def.url], tmpFile, (pct, received, total, message) => {
    if (onProgress) onProgress({ id, status: 'downloading', pct: pct ?? undefined, received, total, message });
  });

  let checkFile;
  if (isZip) {
    if (onProgress) onProgress({ id, status: 'extracting', pct: 100 });
    await extractZip(tmpFile, destDir);
    fs.unlinkSync(tmpFile);
    checkFile = destDir; // pas de fichier unique connu à l'avance dans une archive tierce
  } else {
    checkFile = path.join(destDir, destFileName);
    fs.renameSync(tmpFile, checkFile);
  }

  const state = readState();
  state['dyn:' + id] = { installedAt: new Date().toISOString(), source: def.url, ai: true };
  writeState(state);
  removeAiSuggestion(id); // une fois installée, ce n'est plus une suggestion "en attente"

  log(`✅ ${def.label || id} (trouvé par IA) installé dans ${destDir}.`);
  if (onProgress) onProgress({ id, status: 'done', pct: 100 });
  return { ok: true, destDir, checkFile };
}

function getLastUpdateCheck() {
  const state = readState();
  return state.lastUpdateCheck || null;
}

module.exports = {
  ROOT, // exposé pour que main.js transmette le même dossier (APKF_TOOLS_DIR) au process Python.
  COMPONENTS,
  listComponents,
  isInstalled,
  installComponent,
  installComponents,
  updateComponent,
  checkComponentUpdates,
  getLastUpdateCheck,
  // Composants trouvés par l'IA (hors registre, jamais auto-installés)
  searchGithubReleaseAsset,
  installDynamicComponent,
  saveAiSuggestion,
  listAiSuggestions,
  removeAiSuggestion,
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
