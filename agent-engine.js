/* =====================================================================
   AGENT-ENGINE.JS — Assistant IA agentique pour APK Factory Pro
   =====================================================================

   Ce fichier transforme l'assistant IA (jusqu'ici un simple chat qui
   proposait du code à coller à la main, ou qui écrivait des fichiers en
   parsant des blocs "FILE: chemin" dans du texte libre) en un véritable
   AGENT à appels d'outils (tool calling / function calling), sur le
   modèle de Claude Code :

     1. Le client envoie au modèle la liste des OUTILS disponibles
        (lister l'arborescence, lire un fichier, en écrire un, chercher
        dans le projet, lancer un build, consulter le journal de bugs...).
     2. Le modèle décide LUI-MÊME quels outils appeler, dans quel ordre,
        combien de fois, jusqu'à ce que la tâche demandée soit terminée
        (créer une app, corriger un bug, ajouter une fonctionnalité,
        analyser un projet existant, produire un APK complet...).
     3. Chaque appel d'outil est exécuté ICI, contre le serveur Python
        local (127.0.0.1:7842) déjà lancé par Electron, et le résultat
        est renvoyé au modèle pour qu'il décide de la suite.
     4. Toute la boucle tourne sans clic manuel — mais chaque étape est
        affichée en clair dans le chat (nom de l'outil, résumé de ce qui
        a été fait) : l'autonomie ne veut pas dire "boîte noire".

   PÉRIMÈTRE VOLONTAIREMENT EXCLU DE L'AUTONOMIE TOTALE :
   - Upload d'un APK/zip binaire (jadx-decompile, /import, /build avec
     templateApk, /cordova-generate en sourceMode "template", etc.) :
     ça nécessite un fichier binaire réel que le client a sur SA machine,
     pas quelque chose qu'un modèle de langage peut "taper" dans un appel
     d'outil texte.
   - Création de keystore et signature avec une clé de PRODUCTION : automatisable
     désormais via signing:{mode:'release'} sur build_project, MAIS le mot de
     passe ne transite jamais par l'agent ni par ce fichier — voir main.js
     ('build-with-release-signing', keystore-setup.html/keystore-preload.js).
     Une seule interruption humaine existe encore, UNE FOIS par machine : le
     choix « générer un nouveau keystore » ou « importer un keystore existant »
     dans une fenêtre native isolée, hors de portée de l'agent.
   - Suppression de session / de fichier / test sur un vrai appareil
     Android connecté : autorisés, mais avec confirmation d'un clic par
     défaut (désactivable dans les réglages de l'agent), car ce sont des
     actions irréversibles ou qui touchent du matériel physique du client.

   Intégration : ce fichier est chargé en toute fin de builder.html, APRÈS
   le script principal. Il réutilise les variables/fonctions globales déjà
   déclarées là-bas (AI_LS_KEY, OPENROUTER_URL, aiHistory, renderAIMessage,
   aiPersistHistory, toast, aiEffectiveModel, aiSystemPrompt, loadTree,
   window._currentSessionId, startBuild...) sans les dupliquer. Il ne fait
   qu'une seule modification visible : remplacer window.aiSendMessage par
   une version qui, si le mode Agent est actif, passe par la boucle
   d'outils ci-dessous plutôt que par un simple aller-retour texte.
   ===================================================================== */

(function () {
  'use strict';

  // ───────────────────────────────────────────────────────────────────
  // 0. Sécurité d'intégration : si le script principal n'a pas encore
  //    défini les globales attendues (ordre de chargement incorrect,
  //    ancienne version de builder.html...), on abandonne proprement
  //    plutôt que de planter toute la page.
  // ───────────────────────────────────────────────────────────────────
  const REQUIRED_GLOBALS = ['renderAIMessage', 'aiHistory', 'aiPersistHistory'];
  const missing = REQUIRED_GLOBALS.filter((name) => typeof window[name] === 'undefined');
  if (missing.length) {
    console.error('[Agent IA] Globales manquantes, moteur agent désactivé :', missing);
    return;
  }

  // ───────────────────────────────────────────────────────────────────
  // 1. Configuration persistante (localStorage) du mode Agent
  // ───────────────────────────────────────────────────────────────────
  const LS_ENABLED    = 'apkfactory_agent_enabled';        // '1' | '0'
  const LS_GUARD      = 'apkfactory_agent_guard';          // '1' = confirme les actions destructives
  const LS_MAXSTEPS   = 'apkfactory_agent_maxsteps';       // nombre max d'aller-retours outils par message
  const LS_TRACE      = 'apkfactory_agent_trace_visible';  // '1' = affiche chaque étape dans le chat

  function agentEnabled()  { return (localStorage.getItem(LS_ENABLED) ?? '1') === '1'; }
  function guardOn()       { return (localStorage.getItem(LS_GUARD)   ?? '1') === '1'; }
  function traceOn()       { return (localStorage.getItem(LS_TRACE)  ?? '1') === '1'; }
  function agentLog(line) {
    if (typeof window.logConsole === 'function') window.logConsole(line);
  }

  // Après la création d'un projet par l'agent (sans passer par les boutons de
  // l'UI), on rejoue manuellement les mêmes étapes visuelles que le clic
  // "Mode Dev" humain : bascule d'onglet, masquage des panneaux de config,
  // rafraîchissement de l'arborescence/des sessions — sinon rien ne bouge à
  // l'écran alors que le projet existe bel et bien côté serveur.
  function syncProjectUiAfterCreate(session, mode) {
    try {
      window._currentSessionId = session;
      window.projectLoaded = true;
      if (typeof window.aiOnSessionChanged === 'function') window.aiOnSessionChanged(session);
      const sp = document.getElementById('scratch-panel'); if (sp) sp.style.display = 'none';
      const ti = document.getElementById('template-import'); if (ti) ti.style.display = 'none';
      if (typeof window.switchTopMode === 'function') window.switchTopMode(mode === 'native' ? 'native' : 'scratch');
      if (typeof window.updateBuildLabel === 'function') window.updateBuildLabel();
      if (typeof window.loadTree === 'function') window.loadTree();
      if (typeof window.loadSessions === 'function') window.loadSessions();
      if (typeof window.setStatus === 'function') window.setStatus('done', 'Session prête (IA)');
      agentLog(`🛠 [IA] Mode Dev ouvert automatiquement — session ${session}`);
    } catch (e) { /* pas bloquant si un élément d'UI n'existe pas dans ce contexte */ }
  }


  function maxSteps() {
    const v = parseInt(localStorage.getItem(LS_MAXSTEPS) || '28', 10);
    return Number.isFinite(v) && v > 0 ? Math.min(v, 60) : 28;
  }

  let AGENT_ABORT = null; // AbortController de la requête en cours (bouton Stop)

  // ───────────────────────────────────────────────────────────────────
  // 2. Petits utilitaires
  // ───────────────────────────────────────────────────────────────────

  function currentSid(args) {
    return (args && args.session) || window._currentSessionId || null;
  }

  async function fetchJSON(url, options) {
    const r = await fetch(url, options);
    let data;
    try { data = await r.json(); }
    catch (e) { throw new Error(`Réponse non-JSON de ${url} (HTTP ${r.status})`); }
    if (!r.ok || data?.error) {
      throw new Error(data?.error || `HTTP ${r.status} sur ${url}`);
    }
    return data;
  }

  function truncate(str, max = 4000) {
    if (typeof str !== 'string') str = JSON.stringify(str);
    if (str.length <= max) return str;
    return str.slice(0, max) + `\n… [tronqué, ${str.length - max} caractères de plus]`;
  }

  // Attend qu'une opération asynchrone du serveur (build, génération de
  // projet...) se termine, en interrogeant /status?session=<token>.
  // C'est l'équivalent, côté agent, de "rester devant la barre de
  // progression" que ferait un humain.
  async function pollStatus(token, { timeoutMs = 5 * 60 * 1000, intervalMs = 1200 } = {}) {
    const start = Date.now();
    let last = null;
    while (Date.now() - start < timeoutMs) {
      const d = await fetchJSON('/status?session=' + encodeURIComponent(token));
      last = d;
      if (d.status === 'done' || d.status === 'error') {
        return {
          status: d.status,
          session: d.session || null,
          file: d.file || null,
          logsTail: (d.logs || []).slice(-50).join('\n'),
          result: d.result || null,
        };
      }
      await new Promise((res) => setTimeout(res, intervalMs));
    }
    return {
      status: 'timeout',
      session: last?.session || null,
      logsTail: (last?.logs || []).slice(-50).join('\n'),
    };
  }

  // ───────────────────────────────────────────────────────────────────
  // 3. Confirmation d'action destructive — petite modale autonome,
  //    n'utilise aucun élément DOM propre à builder.html (créée à la
  //    volée), pour ne pas dépendre de sa structure interne.
  // ───────────────────────────────────────────────────────────────────
  function agentConfirm(message) {
    if (!guardOn()) return Promise.resolve(true);
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.style.cssText = `
        position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:99999;
        display:flex; align-items:center; justify-content:center;`;
      overlay.innerHTML = `
        <div style="background:#1b1b1f;color:#eee;max-width:420px;width:90%;
                    border-radius:10px;padding:20px;box-shadow:0 10px 40px rgba(0,0,0,.5);
                    font-family:inherit;">
          <div style="font-weight:700;font-size:15px;margin-bottom:10px;">⚠ Action à confirmer</div>
          <div style="font-size:13px;line-height:1.5;opacity:.9;margin-bottom:16px;white-space:pre-wrap;">${message}</div>
          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button data-act="cancel" style="padding:7px 14px;border-radius:6px;border:1px solid #444;background:#2a2a2f;color:#eee;cursor:pointer;">Annuler</button>
            <button data-act="ok" style="padding:7px 14px;border-radius:6px;border:none;background:#e74c3c;color:#fff;font-weight:600;cursor:pointer;">Confirmer</button>
          </div>
        </div>`;
      overlay.addEventListener('click', (e) => {
        const act = e.target?.dataset?.act;
        if (!act) return;
        document.body.removeChild(overlay);
        resolve(act === 'ok');
      });
      document.body.appendChild(overlay);
    });
  }

  function autopilotOn() { return localStorage.getItem('apkfactory_ai_autopilot') === '1'; }

  // Confirmation étape par étape (créer le projet / lancer le build) —
  // demandée seulement quand le Pilote automatique est DÉCOCHÉ. Contrairement
  // à agentConfirm (dédiée aux actions destructives, pilotée par un autre
  // réglage), celle-ci s'affiche pour les étapes normales du flux de
  // création dès que le client veut garder la main dessus.
  function agentConfirmStep(question, detail) {
    if (autopilotOn()) return Promise.resolve(true);
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.style.cssText = `
        position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:99999;
        display:flex; align-items:center; justify-content:center;`;
      overlay.innerHTML = `
        <div style="background:#1b1b1f;color:#eee;max-width:420px;width:90%;
                    border-radius:10px;padding:20px;box-shadow:0 10px 40px rgba(0,0,0,.5);
                    font-family:inherit;">
          <div style="font-weight:700;font-size:15px;margin-bottom:10px;">🤖 ${esc2(question)}</div>
          ${detail ? `<div style="font-size:13px;line-height:1.5;opacity:.8;margin-bottom:16px;white-space:pre-wrap;">${esc2(detail)}</div>` : ''}
          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button data-act="no" style="padding:7px 14px;border-radius:6px;border:1px solid #444;background:#2a2a2f;color:#eee;cursor:pointer;">Non</button>
            <button data-act="yes" style="padding:7px 14px;border-radius:6px;border:none;background:var(--accent,#3ddc84);color:#0c1f14;font-weight:700;cursor:pointer;">Oui</button>
          </div>
        </div>`;
      overlay.addEventListener('click', (e) => {
        const act = e.target?.dataset?.act;
        if (!act) return;
        document.body.removeChild(overlay);
        resolve(act === 'yes');
      });
      document.body.appendChild(overlay);
    });
  }
  function esc2(s) { const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }


  // ───────────────────────────────────────────────────────────────────
  function renderAgentStep(icon, label, detail, ok = true) {
    if (!traceOn()) return;
    const list = document.getElementById('ai-messages');
    if (!list) return;
    const row = document.createElement('div');
    row.className = 'ai-msg system-note';
    row.style.cssText = `font-size:12px;opacity:.85;border-left:3px solid ${ok ? '#3ddc84' : '#e74c3c'};padding:4px 8px;margin:3px 0;`;
    row.innerHTML = `${icon} <b>${label}</b>${detail ? ' — ' + detail : ''}`;
    list.appendChild(row);
    row.scrollIntoView({ block: 'nearest' });
  }

  function renderAgentBanner(text) {
    if (typeof window.appendAISystemNote === 'function') {
      window.appendAISystemNote(text);
      return;
    }
    renderAgentStep('🤖', text, '', true);
  }

  // Injecte un bouton "⏹ Stop" à côté du bouton d'envoi pendant qu'une
  // tâche agent tourne, pour permettre d'interrompre une boucle longue
  // (ex: un build qui boucle sur des erreurs) sans fermer l'appli.
  function showStopButton(show) {
    let btn = document.getElementById('agent-stop-btn');
    const sendBtn = document.getElementById('ai-send-btn');
    if (!sendBtn) return;
    if (show && !btn) {
      btn = document.createElement('button');
      btn.id = 'agent-stop-btn';
      btn.textContent = '⏹ Stop';
      btn.style.cssText = 'margin-left:6px;background:#e74c3c;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;';
      btn.onclick = () => { if (AGENT_ABORT) AGENT_ABORT.abort(); };
      sendBtn.parentNode.insertBefore(btn, sendBtn.nextSibling);
    } else if (!show && btn) {
      btn.remove();
    }
  }

  // ───────────────────────────────────────────────────────────────────
  // 5. Définition des OUTILS (schéma function-calling façon OpenAI,
  //    compatible avec la plupart des modèles servis par OpenRouter)
  // ───────────────────────────────────────────────────────────────────
  // Doc exhaustive des champs 'config' acceptés par le serveur (server.py,
  // do_build_native / cordova / twa / flutter / rn) pour les modes qui
  // génèrent un manifeste Android eux-mêmes. Centralisée ici pour que le
  // modèle règle TOUT lui-même (permissions comprises) sans jamais avoir
  // besoin qu'un humain coche une case dans l'interface.
  const CONFIG_FIELDS_DOC =
    "Champs supportés — packageName (ex: com.exemple.monapp) ; appName ; " +
    "versionName (ex: '1.0') ; versionCode (entier, ex: 1) ; " +
    "minSdk (défaut '23') ; targetSdk (défaut '35') ; " +
    "orientation ('portrait'|'landscape'|'unspecified') ; " +
    "mode ('url' = enveloppe un site distant [nécessite appUrl], 'html' = contenu HTML inline [nécessite htmlContent], 'sitezip' = site déjà présent dans le projet) ; " +
    "appUrl ; htmlContent ; " +
    "fullscreen (bool, cache barre de statut) ; immersive (bool, cache aussi la barre de navigation) ; lockTask (bool, verrouille l'app en plein écran type kiosque) ; " +
    "permissions (tableau de chaînes 'android.permission.XXX' à déclarer ET demander à l'exécution si sensible — INTERNET et ACCESS_NETWORK_STATE sont déjà ajoutées automatiquement, inutile de les répéter). " +
    "Choisis TOUJOURS les permissions toi-même selon ce que l'app doit faire, sans attendre que le client les précise : ex. une app qui prend des photos → CAMERA ; qui enregistre du son/fait des appels vocaux → RECORD_AUDIO ; qui géolocalise → ACCESS_FINE_LOCATION (+ ACCESS_COARSE_LOCATION, et ACCESS_BACKGROUND_LOCATION seulement si le suivi doit continuer en arrière-plan) ; qui accède aux photos/vidéos/musique → READ_MEDIA_IMAGES/READ_MEDIA_VIDEO/READ_MEDIA_AUDIO (ou READ_EXTERNAL_STORAGE sous Android ancien) ; qui envoie des notifications → POST_NOTIFICATIONS ; qui lit les contacts → READ_CONTACTS ; qui appelle → CALL_PHONE/READ_PHONE_STATE ; qui envoie/lit des SMS → SEND_SMS/READ_SMS/RECEIVE_SMS ; qui utilise le Bluetooth → BLUETOOTH_CONNECT/BLUETOOTH_SCAN. Ne demande JAMAIS une permission dont l'app n'a pas l'usage. " +
    "customPermissions (chaîne, permissions non listées ci-dessus séparées par virgule ou retour à la ligne, ex: nom de permission personnalisée d'un plugin tiers).";

  const TOOLS = [
    {
      type: 'function',
      function: {
        name: 'get_project_overview',
        description: "Vue d'ensemble complète d'une session : type de pipeline (scratch/natif/TWA/cordova/flutter/react native), arborescence des fichiers, contenu des fichiers de config clés (manifest, gradle, config.xml, pubspec.yaml, package.json...), nombre de valeurs smali modifiables, et état de santé du projet. À appeler EN PREMIER dès qu'un projet existant est concerné, avant de lire des fichiers un par un.",
        parameters: {
          type: 'object',
          properties: { session: { type: 'string', description: 'ID de session (optionnel, sinon la session active est utilisée)' } },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'list_sessions',
        description: "Liste toutes les sessions/projets existants sur cette installation (id, origine, package, date de création). Utile pour retrouver un projet dont le client parle sans donner son id exact, ou pour analyser plusieurs sessions.",
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'select_session',
        description: "Rend une session existante 'active' côté serveur (nécessaire avant build_project en mode scratch). N'efface rien, ne fait que changer le projet courant.",
        parameters: {
          type: 'object',
          properties: { session: { type: 'string', description: 'ID de la session à activer' } },
          required: ['session'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'list_tree',
        description: "Liste les fichiers et dossiers d'un projet (ou d'un sous-dossier précis).",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string', description: 'ID de session (optionnel)' },
            path: { type: 'string', description: "Sous-dossier à lister (optionnel, racine du projet par défaut)" },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'read_file',
        description: "Lit le contenu texte d'un fichier du projet (smali, XML, HTML/CSS/JS, Kotlin, Gradle, JSON...). Les fichiers binaires (images, .dex) ne sont pas lisibles ainsi.",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string', description: 'ID de session (optionnel)' },
            path: { type: 'string', description: 'Chemin relatif du fichier dans le projet' },
          },
          required: ['path'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'write_file',
        description: "Écrit (crée ou remplace intégralement) un fichier texte du projet. Toujours lire le fichier avant de le réécrire s'il existe déjà, pour ne pas perdre du contenu utile.",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string', description: 'ID de session (optionnel)' },
            path: { type: 'string', description: 'Chemin relatif du fichier à écrire' },
            content: { type: 'string', description: 'Contenu texte complet du fichier' },
          },
          required: ['path', 'content'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'replace_line',
        description: "Remplace une ligne précise d'un fichier par son numéro, en vérifiant que l'ancien contenu correspond bien (évite d'écraser un changement concurrent). Préférable à write_file pour une modification ponctuelle sur un gros fichier.",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string' },
            path: { type: 'string' },
            line: { type: 'integer', description: 'Numéro de ligne (1-indexé)' },
            oldText: { type: 'string', description: 'Contenu actuel exact de la ligne' },
            newText: { type: 'string', description: 'Nouveau contenu de la ligne' },
          },
          required: ['path', 'line', 'oldText', 'newText'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'search_project',
        description: "Cherche une chaîne ou une regex dans TOUT le contenu texte du projet (comme un 'chercher dans les fichiers' d'un IDE). Retourne fichier + ligne + extrait pour chaque correspondance. Essentiel pour localiser où modifier quelque chose dans un projet décompilé (smali) sans connaître d'avance le nom du fichier.",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string' },
            query: { type: 'string' },
            regex: { type: 'boolean', description: 'true si query est une expression régulière' },
            case_sensitive: { type: 'boolean' },
          },
          required: ['query'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'rename_path',
        description: "Renomme ou déplace un fichier/dossier dans le projet, avec option de mise à jour des références (utile pour un renommage de package/ressource).",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string' },
            path: { type: 'string' },
            newPath: { type: 'string' },
            updateRefs: { type: 'boolean' },
          },
          required: ['path', 'newPath'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'duplicate_path',
        description: "Duplique un fichier ou dossier du projet vers un nouveau chemin.",
        parameters: {
          type: 'object',
          properties: { session: { type: 'string' }, path: { type: 'string' }, newPath: { type: 'string' } },
          required: ['path', 'newPath'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'delete_path',
        description: "Supprime définitivement un fichier ou dossier du projet. Action irréversible : une confirmation est demandée au client avant exécution.",
        parameters: {
          type: 'object',
          properties: { session: { type: 'string' }, path: { type: 'string' } },
          required: ['path'],
        },
        destructive: true,
      },
    },
    {
      type: 'function',
      function: {
        name: 'get_smali_facts',
        description: "Pour un projet décompilé (APK importé), liste les valeurs modifiables détectées automatiquement dans le smali : couleurs, textes, durées, URLs — avec un id stable pour chacune. Plus fiable que d'éditer le smali brut à la main pour ces cas précis.",
        parameters: { type: 'object', properties: { session: { type: 'string' } } },
      },
    },
    {
      type: 'function',
      function: {
        name: 'apply_smali_facts',
        description: "Applique une ou plusieurs modifications de valeurs smali détectées par get_smali_facts (par id).",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string' },
            edits: {
              type: 'array',
              items: { type: 'object', properties: { id: { type: 'string' }, value: { type: 'string' } }, required: ['id', 'value'] },
            },
          },
          required: ['edits'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'create_project',
        description: "Crée un NOUVEAU projet vide/squelette (génération seule, sans compilation), prêt à être personnalisé fichier par fichier avec write_file, puis compilé avec build_project. Modes supportés ici : 'scratch' (WebView HTML/CSS/JS from scratch), 'cordova', 'flutter', 'reactnative'. Pour 'native' et 'twa', utiliser directement build_project (ces deux modes génèrent et compilent en une seule étape).",
        parameters: {
          type: 'object',
          properties: {
            mode: { type: 'string', enum: ['scratch', 'cordova', 'flutter', 'reactnative'] },
            config: {
              type: 'object',
              description: CONFIG_FIELDS_DOC,
            },
          },
          required: ['mode'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'build_project',
        description: "Compile un APK et attend le résultat (jusqu'à quelques minutes). Modes : 'scratch' (compile la session active TELLE QU'ÉDITÉE, via recompile — nécessite un projet déjà créé/sélectionné) ; 'native' et 'twa' (génèrent ET compilent en une fois à partir de 'config', one-shot, pas d'édition fichier par fichier possible pour ces deux modes) ; 'cordova'/'flutter'/'reactnative' (si 'session' est fourni, compile le projet existant tel quel avec les modifications faites via write_file ; sinon génère un nouveau projet puis compile). Retourne le statut final, la fin du journal de build, et le nom du fichier APK produit s'il y a réussite.",
        parameters: {
          type: 'object',
          properties: {
            mode: { type: 'string', enum: ['scratch', 'native', 'twa', 'cordova', 'flutter', 'reactnative'] },
            session: { type: 'string', description: "Session à compiler en place (cordova/flutter/reactnative/scratch)" },
            config: { type: 'object', description: "Requis pour native/twa/cordova/flutter/reactnative sans session existante. " + CONFIG_FIELDS_DOC },
            icon: { type: 'string', description: "Icône de l'app en base64 (PNG carré, sans préfixe 'data:image/...;base64,'). Optionnel : si le client a fourni un logo (image jointe au chat) ou que tu en as généré un, passe-le ici pour que l'icône soit posée automatiquement, sans étape manuelle de remplacement d'image." },
            outName: { type: 'string', description: "Nom du fichier de sortie (mode scratch uniquement)" },
            signing: { type: 'object', description: "{ mode: 'debug' } par défaut (signature de test, non distribuable en confiance). { mode: 'release' } signe automatiquement avec le keystore de production configuré une fois pour toutes sur cette machine — AUCUN mot de passe n'est demandé ni visible ici : si aucun keystore n'est encore configuré, une fenêtre native s'ouvre pour que le client choisisse en un clic 'générer' ou 'importer', puis le build repart automatiquement. Utilise 'release' dès que le client veut un APK à distribuer réellement (pas juste tester) ; garde 'debug' seulement pour des tests rapides explicitement demandés comme tels." },
          },
          required: ['mode'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'list_output_apks',
        description: "Liste les APK déjà générés disponibles au téléchargement, du plus récent au plus ancien.",
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'get_bug_log',
        description: "Journal persistant des erreurs/avertissements rencontrés par l'outil (build, signature, etc.) et indicateur de santé global. Utile pour diagnostiquer un problème récurrent avant de proposer un correctif.",
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'list_recent_bugs',
        description: "Liste les bugs déjà rencontrés ET corrigés récemment sur ce projet (journal persistant). À appeler AVANT de modifier du code sensible (build, signature, manifeste, encodage, ZIP) pour éviter de réintroduire un bug déjà résolu. Différent de get_bug_log : ici, on ne regarde que l'historique déjà traité, pas l'état de santé courant.",
        parameters: {
          type: 'object',
          properties: {
            limit: { type: 'number', description: 'Nombre max de bugs à retourner (défaut 15)' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'check_environment',
        description: "Vérifie les outils système disponibles (Java, apktool, Gradle, jadx, zipalign, apksigner, ADB, bundletool) et leurs versions. À appeler si un build échoue de façon inexpliquée : la cause est souvent un outil manquant ou une mauvaise version.",
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'adb_devices',
        description: "Liste les appareils Android connectés en USB/débogage visibles par ADB.",
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'run_device_test',
        description: "Installe et lance l'APK produit le plus récent sur un appareil Android connecté, pour un test rapide. Touche un appareil physique du client : confirmation demandée.",
        parameters: {
          type: 'object',
          properties: { apkPath: { type: 'string', description: "Nom de fichier APK dans le dossier de sortie (optionnel, dernier par défaut)" }, packageName: { type: 'string' }, serial: { type: 'string', description: "Numéro de série ADB si plusieurs appareils connectés" } },
        },
        destructive: true,
      },
    },
    {
      type: 'function',
      function: {
        name: 'set_agent_settings',
        description: "Change directement les réglages de l'assistant IA lui-même, quand le client le demande dans le chat (ex: « désactive les confirmations », « arrête de me demander avant de supprimer », « active le mode voix », « teste automatiquement dès qu'un câble est branché »). Ne touche à rien d'autre que la configuration de l'assistant sur cette machine.",
        parameters: {
          type: 'object',
          properties: {
            confirmDestructive: { type: 'boolean', description: "true = redemande confirmation avant suppression/appareil physique ; false = exécute directement sans demander." },
            autonomous:         { type: 'boolean', description: "true = applique chaque réponse au projet + build auto ; false = propose seulement." },
            voiceReadReplies:   { type: 'boolean', description: "true = lit les réponses à voix haute automatiquement." },
            adbAutoTestOnPlug:  { type: 'boolean', description: "true = installe/lance automatiquement le dernier APK dès qu'un appareil Android est branché en USB." },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'check_missing_components',
        description: "Liste tous les composants/outils téléchargeables gérés par l'installateur intégré (JDK, Apktool, Gradle, Android SDK, jadx, bundletool, moteur IA locale, etc.), avec pour chacun s'il est déjà installé ou non sur cette machine. Utilise ça pour savoir quoi installer AVANT de lancer un build qui en dépend, ou quand check_environment signale un outil manquant.",
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'install_components',
        description: "Télécharge et installe automatiquement, sans intervention du client, un ou plusieurs composants listés par check_missing_components (miroirs de secours automatiques si un lien est mort). N'installe QUE des outils reconnus de la chaîne de build Android/APK (JDK, SDK, apktool, gradle, jadx, zipalign, apksigner, bundletool, moteur IA locale) — jamais un logiciel arbitraire hors de cette liste.",
        parameters: {
          type: 'object',
          properties: {
            ids: { type: 'array', items: { type: 'string' }, description: "Identifiants de composants à installer, tels que renvoyés par check_missing_components (ex: ['jdk','apktool','gradle'])." },
          },
          required: ['ids'],
        },
      },
    },
  ];

  const DESTRUCTIVE = new Set(TOOLS.filter((t) => t.function.destructive).map((t) => t.function.name));

  // ───────────────────────────────────────────────────────────────────
  // 6. Exécuteurs des outils — chaque fonction retourne un objet JSON
  //    (envoyé tel quel au modèle) + peut lancer un rendu de trace.
  // ───────────────────────────────────────────────────────────────────
  const EXECUTORS = {

    async get_project_overview(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error("Aucune session active et aucun 'session' fourni.");
      return fetchJSON('/agent-overview?session=' + encodeURIComponent(sid));
    },

    async list_sessions() {
      return fetchJSON('/sessions');
    },

    async select_session(args) {
      if (!args.session) throw new Error("Paramètre 'session' manquant.");
      const r = await fetchJSON('/select-session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: args.session }),
      });
      window._currentSessionId = args.session;
      if (typeof window.loadTree === 'function') window.loadTree();
      return r;
    },

    async list_tree(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      const qp = new URLSearchParams({ session: sid });
      if (args.path) qp.set('path', args.path);
      return fetchJSON('/tree?' + qp.toString());
    },

    async read_file(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      if (!args.path) throw new Error("Paramètre 'path' manquant.");
      const qp = new URLSearchParams({ session: sid, path: args.path });
      const data = await fetchJSON('/file?' + qp.toString());
      if (data.type === 'text') return { path: args.path, type: 'text', content: truncate(data.content, 20000), size: data.size };
      return { path: args.path, type: data.type || 'binary', note: 'Fichier binaire non lisible en texte (image ou compilé).', size: data.size };
    },

    async write_file(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error("Aucune session active — utilise create_project d'abord.");
      if (!args.path) throw new Error("Paramètre 'path' manquant.");
      const r = await fetchJSON('/save-file', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid, path: args.path, content: args.content ?? '' }),
      });
      agentLog(`✅ [IA] Fichier écrit : ${args.path}`);
      if (typeof window.loadTree === 'function') window.loadTree();
      return r;
    },

    async replace_line(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      return fetchJSON('/replace-line', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid, path: args.path, line: args.line, oldText: args.oldText, newText: args.newText }),
      });
    },

    async search_project(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      const qp = new URLSearchParams({ session: sid, q: args.query || '' });
      if (args.regex) qp.set('regex', '1');
      if (args.case_sensitive) qp.set('case', '1');
      const data = await fetchJSON('/search-content?' + qp.toString());
      return { ...data, results: (data.results || []).slice(0, 100) };
    },

    async rename_path(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      const r = await fetchJSON('/rename', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid, path: args.path, newPath: args.newPath, updateRefs: !!args.updateRefs }),
      });
      agentLog(`✏️ [IA] Renommé : ${args.path} → ${args.newPath}`);
      if (typeof window.loadTree === 'function') window.loadTree();
      return r;
    },

    async duplicate_path(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      const r = await fetchJSON('/duplicate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid, path: args.path, newPath: args.newPath }),
      });
      agentLog(`📄 [IA] Dupliqué : ${args.path} → ${args.newPath}`);
      if (typeof window.loadTree === 'function') window.loadTree();
      return r;
    },

    async delete_path(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      const ok = await agentConfirm(`L'assistant IA veut SUPPRIMER définitivement :\n${args.path}\n\nContinuer ?`);
      if (!ok) return { cancelled: true, reason: 'Refusé par le client.' };
      const qp = new URLSearchParams({ session: sid, path: args.path });
      const r = await fetchJSON('/file?' + qp.toString(), { method: 'DELETE' });
      agentLog(`🗑 [IA] Supprimé : ${args.path}`);
      if (typeof window.loadTree === 'function') window.loadTree();
      return r;
    },

    async get_smali_facts(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      return fetchJSON('/smali-facts?session=' + encodeURIComponent(sid));
    },

    async apply_smali_facts(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error('Aucune session active.');
      return fetchJSON('/smali-apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid, edits: args.edits || [] }),
      });
    },

    async create_project(args) {
      const mode = args.mode;
      const endpointByMode = {
        scratch: '/create-scratch-session',
        cordova: '/cordova-generate',
        flutter: '/flutter-generate',
        reactnative: '/rn-generate',
      };
      const tokenByMode = { scratch: 'legacy', cordova: 'cordova_gen', flutter: 'flutter_gen', reactnative: 'rn_gen' };
      const endpoint = endpointByMode[mode];
      if (!endpoint) throw new Error(`create_project ne gère pas le mode '${mode}'. Utilise build_project directement pour 'native' ou 'twa'.`);

      const okStep = await agentConfirmStep('Créer le projet maintenant ?', `Mode : ${mode}`);
      if (!okStep) return { cancelled: true, reason: 'Refusé par le client.' };

      agentLog(`🚀 [IA] Création du projet (mode ${mode})…`);
      const started = await fetchJSON(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: args.config || {} }),
      });
      if (!started.started) throw new Error("Le serveur n'a pas confirmé le démarrage de la génération.");

      const result = await pollStatus(tokenByMode[mode], { timeoutMs: 120000 });
      if (result.status !== 'done') {
        agentLog(`❌ [IA] Échec de la création du projet (${result.status})`);
        throw new Error(`Échec de la génération du projet (${result.status}) :\n${result.logsTail}`);
      }
      if (result.session) {
        syncProjectUiAfterCreate(result.session, mode);
      }
      return { session: result.session, status: result.status, logsTail: truncate(result.logsTail, 2500) };
    },

    async build_project(args) {
      const mode = args.mode;
      const signing = args.signing || { mode: 'debug' };
      const sid = currentSid(args);

      let endpoint, token, body;

      if (mode === 'scratch') {
        if (!sid) throw new Error("Mode 'scratch' : aucune session active. Utilise create_project puis select_session d'abord.");
        await fetchJSON('/select-session', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session: sid }),
        });
        endpoint = '/recompile'; token = 'legacy';
        body = { signing, outName: args.outName || 'output.apk' };
      } else if (mode === 'native') {
        endpoint = '/build-native'; token = 'native';
        body = { config: { ...(args.config || {}), signing }, icon: args.icon || undefined };
      } else if (mode === 'twa') {
        endpoint = '/build-twa'; token = 'twa';
        body = { config: { ...(args.config || {}), signing }, icon: args.icon || undefined };
      } else if (mode === 'cordova' || mode === 'flutter' || mode === 'reactnative') {
        const endpointMap = { cordova: '/build-cordova', flutter: '/build-flutter', reactnative: '/build-rn' };
        const tokenMap = { cordova: 'cordova', flutter: 'flutter', reactnative: 'rn' };
        endpoint = endpointMap[mode]; token = tokenMap[mode];
        body = { config: { ...(args.config || {}), session: sid || undefined, signing }, icon: args.icon || undefined };
      } else {
        throw new Error(`Mode de build inconnu : '${mode}'.`);
      }

      let started;
      const okStep = await agentConfirmStep('Lancer la compilation (build) maintenant ?', `Mode : ${mode}`);
      if (!okStep) return { cancelled: true, reason: 'Refusé par le client.' };

      if (signing.mode === 'release') {
        if (!window.electronAPI || typeof window.electronAPI.buildWithReleaseSigning !== 'function') {
          throw new Error("Signature 'release' indisponible : cette version de l'app n'expose pas encore le pont sécurisé de signature (électronAPI.buildWithReleaseSigning manquant).");
        }
        // Ne transite JAMAIS par un fetch() du renderer : le process principal
        // (main.js) injecte lui-même les identifiants déchiffrés et fait la
        // requête HTTP, ouvrant au besoin la fenêtre de configuration unique.
        started = await window.electronAPI.buildWithReleaseSigning(endpoint, body);
        if (started.needsSetup) {
          renderAgentStep('🔐', 'Signature de production', started.cancelled ? 'configuration annulée par le client' : 'configuration requise', false);
          return {
            status: 'setup_required',
            message: started.error || "La signature de production n'est pas encore configurée sur cette machine. Une fenêtre de configuration s'est ouverte (générer ou importer un keystore, une seule fois) — relance le build une fois qu'elle est complétée.",
          };
        }
        if (started.error) throw new Error(started.error);
      } else {
        started = await fetchJSON(endpoint, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
      }
      if (!started.started) throw new Error("Le serveur n'a pas confirmé le démarrage du build.");

      agentLog(`🛠 [IA] Build lancé (mode ${mode})…`);
      renderAgentStep('🛠', `Build ${mode} lancé`, 'en cours de compilation…', true);
      const result = await pollStatus(token, { timeoutMs: 6 * 60 * 1000 });

      if (result.session) { window._currentSessionId = result.session; syncProjectUiAfterCreate(result.session, mode); }
      if (typeof window.refreshOutputList === 'function') window.refreshOutputList();
      agentLog(result.status === 'done' ? `✅ [IA] Build terminé : ${result.file || '(voir logs)'}` : `❌ [IA] Build en échec (${result.status})`);

      return {
        status: result.status,
        session: result.session,
        apkFile: result.file,
        logsTail: truncate(result.logsTail, 3000),
      };
    },

    async list_output_apks() {
      return fetchJSON('/list-output');
    },

    async get_bug_log() {
      const data = await fetchJSON('/bug-log');
      return { health: data.health, entries: (data.entries || []).slice(0, 30) };
    },

    async list_recent_bugs(args) {
      const limit = Math.max(1, Math.min(50, parseInt(args?.limit, 10) || 15));
      const data = await fetchJSON('/bug-log');
      const entries = (data.entries || [])
        .filter(e => e.severity === 'fixed' || e.resolved === true || /corrig/i.test(e.title || ''))
        .slice(0, limit)
        .map(e => ({ id: e.id, title: e.title, detail: e.detail, source: e.source }));
      return {
        count: entries.length,
        note: entries.length
          ? "Ces bugs ont déjà été corrigés — évite de réintroduire ces régressions."
          : "Aucun bug résolu répertorié pour l'instant.",
        entries,
      };
    },

    async check_environment() {
      return fetchJSON('/check');
    },

    async adb_devices() {
      return fetchJSON('/adb-devices');
    },

    async run_device_test(args) {
      const ok = await agentConfirm("L'assistant IA veut installer et lancer l'APK le plus récent sur un appareil Android connecté. Continuer ?");
      if (!ok) return { cancelled: true, reason: 'Refusé par le client.' };
      const apkName = args.apkPath || args.apkName || window._lastBuiltApk;
      const packageName = args.packageName || window._lastBuiltPackage;
      if (!apkName) return { error: "Aucun APK construit dans cette session pour l'instant — lance build_project d'abord." };
      return fetchJSON('/device-test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apkName, packageName, serial: args.serial }),
      });
    },

    async set_agent_settings(args) {
      const applied = [];
      const syncBox = (id, checked) => { const el = document.getElementById(id); if (el) el.checked = checked; };

      if (typeof args.confirmDestructive === 'boolean') {
        localStorage.setItem(LS_GUARD, args.confirmDestructive ? '1' : '0');
        syncBox('agent-toggle-guard', args.confirmDestructive);
        applied.push(`Confirmation des actions destructives : ${args.confirmDestructive ? 'activée' : 'désactivée'}`);
      }
      // AI_LS_AUTONOMOUS / AI_LS_TTS / AI_LS_ADB_AUTOTEST sont des `const` déclarées dans
      // builder.html : accessibles ici en tant qu'identifiants globaux partagés (même page,
      // script chargé après), mais PAS via window.* (les const de script top-level ne
      // s'attachent pas à window, contrairement aux fonctions déclarées avec `function`).
      try {
        if (typeof args.autonomous === 'boolean' && typeof AI_LS_AUTONOMOUS !== 'undefined') {
          localStorage.setItem(AI_LS_AUTONOMOUS, args.autonomous ? '1' : '0');
          syncBox('ai-autonomous-toggle', args.autonomous);
          if (typeof aiUpdateAutonomousHint === 'function') aiUpdateAutonomousHint();
          applied.push(`Mode autonome : ${args.autonomous ? 'activé' : 'désactivé'}`);
        }
      } catch (e) {}
      try {
        if (typeof args.voiceReadReplies === 'boolean' && typeof AI_LS_TTS !== 'undefined') {
          localStorage.setItem(AI_LS_TTS, args.voiceReadReplies ? '1' : '0');
          syncBox('ai-tts-toggle', args.voiceReadReplies);
          if (typeof aiUpdateTTSHint === 'function') aiUpdateTTSHint();
          applied.push(`Lecture vocale des réponses : ${args.voiceReadReplies ? 'activée' : 'désactivée'}`);
        }
      } catch (e) {}
      try {
        if (typeof args.adbAutoTestOnPlug === 'boolean' && typeof AI_LS_ADB_AUTOTEST !== 'undefined') {
          localStorage.setItem(AI_LS_ADB_AUTOTEST, args.adbAutoTestOnPlug ? '1' : '0');
          syncBox('ai-adb-autotest-toggle', args.adbAutoTestOnPlug);
          applied.push(`Test automatique sur branchement USB : ${args.adbAutoTestOnPlug ? 'activé' : 'désactivé'}`);
        }
      } catch (e) {}
      if (!applied.length) return { changed: false, message: "Aucun réglage valide fourni." };
      return { changed: true, applied };
    },

    async check_missing_components() {
      if (!(window.electronAPI && window.electronAPI.setupListComponents)) {
        return { available: false, reason: "Installateur indisponible (hors app desktop, ou preload non chargé)." };
      }
      const list = await window.electronAPI.setupListComponents();
      return {
        available: true,
        installed: list.filter(c => c.installed).map(c => c.id),
        missing: list.filter(c => !c.installed).map(c => ({ id: c.id, label: c.label, sizeApprox: c.sizeApprox })),
      };
    },

    async install_components(args) {
      const ids = Array.isArray(args.ids) ? args.ids.filter(Boolean) : [];
      if (!ids.length) return { error: "Aucun identifiant de composant fourni — appelle d'abord check_missing_components." };
      if (!(window.electronAPI && window.electronAPI.setupInstallComponents)) {
        return { available: false, reason: "Installateur indisponible (hors app desktop, ou preload non chargé)." };
      }
      if (typeof toast === 'function') toast(`⬇ Installation automatique : ${ids.join(', ')}…`, 'ok');
      const result = await window.electronAPI.setupInstallComponents(ids);
      if (typeof refreshComponentsList === 'function') refreshComponentsList();
      return result;
    },
  };

  // ───────────────────────────────────────────────────────────────────
  // 7. Prompt système étendu pour le mode Agent
  // ───────────────────────────────────────────────────────────────────
  function agentSystemPrompt() {
    const base = (typeof window.aiSystemPrompt === 'function') ? window.aiSystemPrompt() : '';
    const autopilot = localStorage.getItem('apkfactory_ai_autopilot') === '1';
    return [
      base,
      '',
      "── MODE AGENT ACTIF ──",
      "Tu disposes d'outils réels pour AGIR directement sur le projet du client, pas seulement en parler : lister/lire/écrire des fichiers, chercher dans tout le projet, appliquer des réglages smali guidés, créer un nouveau projet, lancer une compilation et lire le résultat, consulter l'environnement système et le journal d'erreurs.",
      "Travaille comme un développeur consciencieux : avant de modifier un projet existant, appelle get_project_overview (et list_tree/read_file/search_project si besoin) pour comprendre ce qui existe déjà — ne réécris jamais à l'aveugle un fichier que tu n'as pas lu.",
      "Pour une demande du type « crée-moi une app complète pour X » sans projet existant : choisis un mode raisonnable (scratch pour une simple appli web/webview, natif si le client demande explicitement du Kotlin natif, cordova/flutter/react native si des plugins natifs spécifiques sont nécessaires), crée le projet, écris/complète les fichiers nécessaires, puis lance build_project — sans attendre que le client te demande chaque étape séparément.",
      "Si le client dit juste « crée-moi un apk » sans aucun détail sur le contenu (pas d'app précisée, pas de site à envelopper, pas de fonctionnalité décrite), pose UNE seule question courte pour savoir quoi construire concrètement (ex: « Tu veux envelopper un site existant dans une app, ou que je code une app à partir de zéro — et laquelle ? »). Dès que la réponse te donne assez d'info pour déduire le mode technique (site à envelopper → twa/scratch selon complexité ; app avec logique propre type calculatrice/jeu/liste → scratch ou natif ; besoin de plugins caméra/GPS/notifications → cordova/flutter/reactnative), tu choisis le mode TOI-MÊME sans demander au client de choisir entre scratch/natif/cordova/flutter/TWA — ce choix technique t'appartient, pas à lui.",
      "Objectif : ZÉRO intervention manuelle du client entre le prompt de départ et l'APK prêt à télécharger, pour tout ce qui est techniquement automatisable. Ça inclut le choix des permissions Android (voir le champ 'permissions' de build_project/create_project — déduis-les toi-même du besoin de l'app, ne demande jamais au client de les cocher) et la pose de l'icône (champ 'icon' de build_project, en base64, si une image a été fournie ou générée dans la conversation). Le client ne doit pas avoir à ouvrir l'onglet Permissions ou Icône de l'interface après ta réponse — si tu peux le régler par outil, règle-le.",
      "Signature de production : passe signing:{mode:'release'} dans build_project dès que le client veut un APK à distribuer réellement, pas juste tester (mode 'debug' → réservé aux tests explicitement demandés comme tels, car souvent bloqué par Play Protect). En mode 'release', tu n'as JAMAIS à demander un mot de passe toi-même — si aucun keystore de production n'est configuré sur la machine du client, le résultat aura status:'setup_required' : dans ce cas, informe simplement le client qu'une fenêtre vient de s'ouvrir pour choisir en un clic 'générer automatiquement' ou 'importer un keystore existant' (ça ne se fait qu'une seule fois par machine), et invite-le à te redemander le build une fois cette fenêtre complétée. Ne tente jamais de contourner ça en redemandant un mot de passe dans le chat.",
      "Si un build échoue, lis le logsTail renvoyé, cherche la cause avec search_project/read_file/check_environment, corrige le fichier concerné, puis relance le build. Ne t'arrête pas au premier échec si la cause est identifiable et corrigeable par toi. Si la cause est un outil manquant (JDK, apktool, gradle, SDK, jadx, bundletool...), appelle check_missing_components puis install_components directement avec l'id concerné — ne demande pas au client d'aller cocher quoi que ce soit dans l'onglet Composants, télécharge-le toi-même puis relance le build automatiquement une fois l'installation terminée.",
      "N'invente jamais un chemin de fichier au hasard : utilise list_tree ou search_project pour le vérifier.",
      "RÈGLE SYNTAXE SMALI — cause n°1 d'échec de compilation constatée : ne JAMAIS écrire une accolade { } en smali comme un littéral de tableau façon Java (ex: `sput-object {\"a\",\"b\"}, ...` ou `new-array v0, {1,2,3}`) — CETTE SYNTAXE N'EXISTE PAS EN SMALI et casse toujours apktool avec une erreur cryptique type 'no viable alternative at input {'. En smali, les accolades ne sont légales que dans deux cas précis : (1) juste après un opcode invoke-* pour lister des registres, ex: invoke-static {v0, v1}, Lclasse;->methode(...)V ; (2) dans un bloc .array-data ... .end array-data pour des données constantes. Pour construire un petit tableau fixe en smali, utilise filled-new-array suivi de move-result-object ; pour des données de tableau constantes, utilise .array-data. Si get_smali_facts détecte la valeur à modifier, préfère TOUJOURS apply_smali_facts à une édition manuelle du smali brut — c'est plus fiable et ça évite ce type d'erreur. Si write_file renvoie une erreur de syntaxe smali, corrige immédiatement le fichier signalé selon ces règles avant de relancer un build, au lieu de relancer aveuglément.",
      "Le client peut aussi te demander des choses qui ne sont pas directement 'créer un APK' : expliquer une erreur, comprendre un concept Android/dev, diagnostiquer l'environnement (check_environment), lister/gérer les appareils ADB connectés, chercher pourquoi un outil manque, etc. Réponds directement à ces demandes avec les outils déjà disponibles au lieu de les considérer hors sujet — tu es l'interlocuteur unique du client dans cette app, pas seulement un générateur d'APK.",
      "Un appareil Android peut être détecté et testé automatiquement dès qu'il est branché (câble + débogage USB activé), sans que le client ait à te le demander dans le chat — c'est géré par l'interface elle-même ; si le client te demande de tester sur un appareil connecté, utilise adb_devices puis run_device_test normalement.",
      "Le client peut te demander de changer tes propres réglages en une phrase (« désactive les confirmations », « active le mode voix », « arrête de me demander avant de tester sur mon téléphone ») : utilise set_agent_settings directement, sans lui demander d'aller cliquer dans Paramètres lui-même.",
      "RÈGLE JAVA/KOTLIN INTERDIT HORS CORDOVA/FLUTTER/REACTNATIVE : un projet scratch, template, ou natif décompilé (importé depuis un APK) compile UNIQUEMENT via apktool sur un arbre smali — il n'existe AUCUN compilateur Java/Kotlin pour ce type de session. N'écris JAMAIS de fichier .java ou .kt (write_file ou replace_line) dans un tel projet, même si tu viens de générer du code Java/Kotlin dans ta réponse — traduis-le en smali, ou édite directement le .smali existant (search_project / get_smali_facts). Le Java/Kotlin n'est légitime QUE dans un projet créé en mode cordova, flutter ou reactnative (create_project), qui utilise un vrai pipeline Gradle. Si write_file ou replace_line renvoie une erreur disant qu'aucun compilateur Java/Kotlin n'est disponible, ne réessaie pas la même chose : réécris le contenu en smali.",
      "RÈGLE URL vs HTML : si le client ne donne AUCUNE URL, utilise TOUJOURS le mode HTML local par défaut, quel que soit le type d'APK (scratch, cordova, flutter, react native, native, twa...) — ne mets jamais une URL par défaut inventée du type https://example.com. Si le client donne UNIQUEMENT une URL, mode URL. Si le client donne à la fois une URL ET du contenu HTML personnalisé, ne choisis JAMAIS à sa place : arrête-toi et demande-lui explicitement lequel utiliser (l'URL distante, le HTML local, ou les deux — par exemple un HTML local avec un lien/bouton vers l'URL). Si un outil (create_project, build_project, la création automatique de session) renvoie une erreur mentionnant une ambiguïté URL/HTML, ne relance PAS l'outil avec un choix arbitraire : pose la question au client dans ta réponse et attends sa réponse.",
      "RÈGLE STRICTE — INTERDICTION DE RENVOYER LA BALLE AU CLIENT : tu n'es jamais dans la situation d'un assistant de code générique qui « explique quoi faire ». Cette app EXÉCUTE elle-même la compilation (Gradle, Android SDK, apktool... tous installés en local via install_components si besoin) : ne dis JAMAIS au client d'ouvrir Android Studio, d'installer un IDE, de compiler lui-même, ou une phrase du type « vous pouvez utiliser tel outil pour construire l'APK ». Si tu viens de produire du code pour une app (Kotlin, Java, XML, HTML/CSS/JS...), l'étape suivante est TOUJOURS d'appeler toi-même create_project (si aucun projet n'existe) puis write_file pour chaque fichier puis build_project — dans la MÊME réponse, sans t'arrêter à une simple description. Une réponse qui contient du code prêt pour une app mais aucun appel d'outil est un échec : n'écris jamais 'Pour construire l'APK, vous pouvez...' — construis-le toi-même.",
      "Certaines actions restent hors de ta portée par conception, quoi que demande le client : importer un APK/zip binaire, créer une clé de signature de production ou signer avec une clé réelle (mots de passe), et toute action nécessitant un fichier physique que seul le client possède. Dans ces cas, explique clairement au client ce qu'il doit faire manuellement (le bouton/la fenêtre correspondante existe déjà dans l'interface).",
      "N'appelle jamais deux fois le même outil avec exactement les mêmes arguments à la suite : si un résultat ne te convient pas, change d'approche.",
      "Quand la tâche est terminée (build réussi, fichiers créés, question répondue), termine par une réponse texte claire au client résumant ce qui a été fait — ne laisse jamais la conversation sur un simple appel d'outil sans explication.",
      autopilot
        ? "── 🚀 PILOTE AUTOMATIQUE ACTIVÉ (zéro intervention humaine) ── Le client a explicitement activé ce mode : tu n'as PLUS LE DROIT de poser la moindre question, même la question de clarification sur le type d'app décrite plus haut. Pour CHAQUE demande, choisis toi-même l'interprétation la plus raisonnable (type d'app, mode technique, permissions, nom de package, couleurs/style si non précisé) et VA JUSQU'AU BOUT dans la même série d'actions : création du projet, écriture de tous les fichiers, build, jusqu'à un APK téléchargeable. Si une information est vraiment ambiguë, choisis le défaut le plus courant/sûr et mentionne ton choix dans ta réponse finale au lieu de le demander avant d'agir. La seule exception reste les 3 actions listées comme hors de portée par conception (import de binaire, mot de passe de signature, action nécessitant un fichier physique du client) — pour tout le reste, agis sans demander."
        : "",
    ].filter(Boolean).join('\n');
  }

  // ───────────────────────────────────────────────────────────────────
  // 8. Exécution d'un appel d'outil demandé par le modèle
  // ───────────────────────────────────────────────────────────────────
  async function runToolCall(toolCall) {
    const name = toolCall.function?.name;
    let args = {};
    try { args = JSON.parse(toolCall.function?.arguments || '{}'); }
    catch (e) { return { ok: false, error: `Arguments JSON invalides pour ${name} : ${e.message}` }; }

    const executor = EXECUTORS[name];
    if (!executor) return { ok: false, error: `Outil inconnu : ${name}` };

    const argsSummary = truncate(JSON.stringify(args), 160);
    try {
      const result = await executor(args);
      const isCancelled = result && result.cancelled;
      renderAgentStep(isCancelled ? '⏸' : '✅', name, argsSummary, !isCancelled);
      return { ok: true, result };
    } catch (e) {
      renderAgentStep('❌', name, `${argsSummary} → ${e.message}`, false);
      return { ok: false, error: e.message || String(e) };
    }
  }

  // ───────────────────────────────────────────────────────────────────
  // 9. Boucle d'agent principale
  // ───────────────────────────────────────────────────────────────────
  async function runAgentLoop(userContent, { attachedImage } = null) {
    const key = localStorage.getItem(window.AI_LS_KEY || 'apkfactory_ai_apikey');
    if (!key) {
      if (typeof window.setAITab === 'function') window.setAITab('settings');
      window.toast?.("Renseigne d'abord une clé API OpenRouter", 'err');
      return;
    }
    const model = (typeof window.aiEffectiveModel === 'function') ? window.aiEffectiveModel() : undefined;
    if (!model) { window.toast?.('Aucun modèle sélectionné', 'err'); return; }

    const messages = [{ role: 'system', content: agentSystemPrompt() }];
    (window.aiHistory || []).slice(-16).forEach((m) => messages.push({ role: m.role, content: m.content }));
    if (attachedImage) {
      messages[messages.length - 1] = {
        role: 'user',
        content: [{ type: 'text', text: userContent }, { type: 'image_url', image_url: { url: attachedImage } }],
      };
    }

    AGENT_ABORT = new AbortController();
    showStopButton(true);
    renderAgentBanner('🤖 Mode Agent : analyse de la demande…');

    let toolsUnsupported = false;
    let steps = 0;
    let finalText = null;

    try {
      while (steps < maxSteps()) {
        steps++;

        let resp;
        try {
          resp = await fetch(window.OPENROUTER_URL || 'https://openrouter.ai/api/v1/chat/completions', {
            method: 'POST',
            signal: AGENT_ABORT.signal,
            headers: {
              'Content-Type': 'application/json',
              'Authorization': 'Bearer ' + key,
              'HTTP-Referer': 'https://apkfactory.local',
              'X-Title': 'APK Factory Pro — Agent',
            },
            body: JSON.stringify({ model, messages, tools: TOOLS, tool_choice: 'auto', max_tokens: 16000 }),
          });
        } catch (e) {
          if (e.name === 'AbortError') { renderAgentBanner('⏹ Agent interrompu par le client.'); return; }
          throw e;
        }

        const data = await resp.json();

        if (!resp.ok) {
          const errMsg = (data?.error?.message || data?.error || '').toString();
          // Certains modèles/fournisseurs sur OpenRouter ne supportent pas
          // encore les tools : on bascule alors sur le mode conversation
          // classique (avec le parsing FILE: existant) plutôt que d'échouer.
          if (!toolsUnsupported && /tool|function.?calling|not support/i.test(errMsg)) {
            toolsUnsupported = true;
            renderAgentBanner('⚠ Ce modèle ne supporte pas les outils — bascule en mode conversation classique pour cette réponse.');
            if (typeof window._aiSendMessageLegacy === 'function') {
              return window._aiSendMessageLegacy();
            }
          }
          throw new Error(errMsg || `HTTP ${resp.status}`);
        }

        const msg = data.choices?.[0]?.message;
        if (!msg) throw new Error('Réponse vide du modèle.');

        if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
          messages.push({ role: 'assistant', content: msg.content || null, tool_calls: msg.tool_calls });

          // On confirme UNE SEULE FOIS, groupées, les actions destructives
          // de ce tour, pour éviter une rafale de popups.
          for (const tc of msg.tool_calls) {
            const outcome = await runToolCall(tc);
            messages.push({
              role: 'tool',
              tool_call_id: tc.id,
              content: truncate(JSON.stringify(outcome.ok ? outcome.result : { error: outcome.error }), 6000),
            });
          }
          continue; // redonne la main au modèle avec les résultats d'outils
        }

        // Pas d'appel d'outil : c'est la réponse finale de ce tour.
        finalText = msg.content || '(réponse vide)';
        break;
      }

      if (finalText === null) {
        finalText = `🤖 (Limite de ${maxSteps()} étapes atteinte pour cette tâche — dis-moi si tu veux que je continue.)`;
      }

      const div = window.renderAIMessage('assistant', finalText);
      window.aiHistory.push({ role: 'assistant', content: finalText });
      window.aiPersistHistory();

      // FILET DE SÉCURITÉ : si le modèle a répondu en texte libre avec des
      // blocs FILE: au lieu d'appeler les outils (arrive avec certains
      // modèles, notamment les plus légers), on ne se contente plus
      // d'espérer qu'un projet existe déjà — on en crée un nous-mêmes si
      // besoin, on écrit tous les fichiers, PUIS on lance le build,
      // exactement comme si le modèle avait fait le travail lui-même.
      if (typeof window.aiExtractFileBlocks === 'function' && typeof window.aiSaveFileToProject === 'function') {
        const files = window.aiExtractFileBlocks(finalText);
        if (files.length) {
          try {
            if (!window._currentSessionId && typeof window.aiEnsureAutonomousSession === 'function') {
              agentLog('⏳ [IA] Aucun projet chargé — création automatique d\'une session avant écriture des fichiers…');
              await window.aiEnsureAutonomousSession();
            }
            if (window._currentSessionId) {
              let written = 0;
              for (const f of files) {
                try { await window.aiSaveFileToProject(f.path, f.code); written++; agentLog(`✅ [IA] Fichier écrit : ${f.path}`); }
                catch (e) { agentLog(`❌ [IA] Échec écriture ${f.path} : ${e.message}`); }
              }
              if (typeof window.loadTree === 'function') window.loadTree();
              if (written && typeof window.aiAutoBuildAndReport === 'function') {
                await window.aiAutoBuildAndReport();
              }
            } else {
              agentLog('❌ [IA] Impossible de créer une session automatiquement — impossible d\'appliquer les fichiers.');
            }
          } catch (e) {
            agentLog('❌ [IA] Filet de secours en échec : ' + e.message);
          }
        }
      }
      const perms = (typeof window.aiExtractPermissions === 'function') ? window.aiExtractPermissions(finalText) : [];
      if (perms.length && typeof window.aiApplyPermissions === 'function') {
        const pbox = document.createElement('div');
        pbox.className = 'ai-img-actions'; pbox.style.marginTop = '8px';
        pbox.innerHTML = `<button onclick='aiApplyPermissions(${JSON.stringify(perms)})'>🔐 Appliquer ces ${perms.length} permission(s)</button>`;
        div.appendChild(pbox);
      }
    } catch (e) {
      window.renderAIMessage('assistant', '❌ Erreur agent : ' + e.message);
    } finally {
      showStopButton(false);
      AGENT_ABORT = null;
    }
  }

  // ───────────────────────────────────────────────────────────────────
  // 10. Câblage : on prend la place de aiSendMessage, en gardant le
  //     comportement d'origine en repli (source locale llama.cpp, mode
  //     agent désactivé, génération d'image, pas de clé API...).
  // ───────────────────────────────────────────────────────────────────
  if (typeof window.aiSendMessage === 'function' && !window._aiSendMessageLegacy) {
    window._aiSendMessageLegacy = window.aiSendMessage;
  }

  window.aiSendMessage = async function agentAwareSend() {
    const box = document.getElementById('ai-input-text');
    const text = box ? box.value.trim() : '';
    if (!text) return;

    const source = (typeof window.aiGetSource === 'function') ? window.aiGetSource() : 'openrouter';
    const isImageGen = source === 'openrouter'
      && /logo|icône|icone|image|illustration/i.test(text)
      && /génère|generate|crée|create|dessine/i.test(text);

    // Le mode Agent (tool calling) ne s'applique qu'à OpenRouter, pour du
    // texte, quand il est activé. Le reste (IA locale, génération d'image,
    // agent désactivé) repart sur le comportement historique.
    if (!agentEnabled() || source !== 'openrouter' || isImageGen) {
      return window._aiSendMessageLegacy();
    }

    let userContent = text;
    let attachedImage = null;
    if (window.aiPendingFile) {
      if (window.aiPendingFile.isImage) {
        attachedImage = window.aiPendingFile.dataUrl;
        userContent += `\n\n[Image jointe : ${window.aiPendingFile.name}]`;
      } else {
        userContent += `\n\n[Fichier joint : ${window.aiPendingFile.name}]\n\`\`\`\n${window.aiPendingFile.text}\n\`\`\``;
      }
      window.aiPendingFile = null;
    }

    box.value = '';
    window.aiHistory.push({ role: 'user', content: userContent });
    window.renderAIMessage('user', userContent);
    window.aiPersistHistory();

    const sendBtn = document.getElementById('ai-send-btn');
    if (sendBtn) sendBtn.disabled = true;
    try {
      await runAgentLoop(userContent, { attachedImage });
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }
  };

  // ───────────────────────────────────────────────────────────────────
  // 11. Petit panneau de réglages injecté au-dessus du champ de saisie
  //     (aucune modification de builder.html nécessaire).
  // ───────────────────────────────────────────────────────────────────
  function injectSettingsRow() {
    const input = document.getElementById('ai-input-text');
    if (!input || document.getElementById('agent-settings-row')) return;
    const row = document.createElement('div');
    row.id = 'agent-settings-row';
    row.style.cssText = 'display:flex;gap:14px;align-items:center;font-size:12px;opacity:.85;margin:4px 0 6px;flex-wrap:wrap;';
    row.innerHTML = `
      <label style="display:flex;gap:5px;align-items:center;cursor:pointer;">
        <input type="checkbox" id="agent-toggle-enabled" ${agentEnabled() ? 'checked' : ''}>
        🤖 Mode Agent (outils réels)
      </label>
      <label style="display:flex;gap:5px;align-items:center;cursor:pointer;">
        <input type="checkbox" id="agent-toggle-guard" ${guardOn() ? 'checked' : ''}>
        Confirmer les actions destructives
      </label>
      <label style="display:flex;gap:5px;align-items:center;cursor:pointer;">
        <input type="checkbox" id="agent-toggle-trace" ${traceOn() ? 'checked' : ''}>
        Afficher chaque étape
      </label>`;
    input.parentNode.insertBefore(row, input);

    row.querySelector('#agent-toggle-enabled').onchange = (e) =>
      localStorage.setItem(LS_ENABLED, e.target.checked ? '1' : '0');
    row.querySelector('#agent-toggle-guard').onchange = (e) =>
      localStorage.setItem(LS_GUARD, e.target.checked ? '1' : '0');
    row.querySelector('#agent-toggle-trace').onchange = (e) =>
      localStorage.setItem(LS_TRACE, e.target.checked ? '1' : '0');
  }

  // Le panneau IA peut être ouvert après le chargement de ce script :
  // on tente l'injection immédiatement, puis on réessaie à l'ouverture
  // du panneau si le champ n'existait pas encore.
  injectSettingsRow();
  const _origOpenAIPanel = window.openAIPanel;
  if (typeof _origOpenAIPanel === 'function') {
    window.openAIPanel = function (...args) {
      const r = _origOpenAIPanel.apply(this, args);
      setTimeout(injectSettingsRow, 50);
      return r;
    };
  }

  console.log('[Agent IA] Moteur agentique chargé —', TOOLS.length, 'outils disponibles.');
})();
