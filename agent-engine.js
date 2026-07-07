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
  function syncProjectUiAfterCreate(session, mode, config) {
    try {
      window._currentSessionId = session;
      window._currentSessionOrigin = mode || 'scratch';
      // CORRECTIF : window.projectLoaded seul était une écriture morte (voir
      // commentaire dans builder.html) — on passe par le vrai pont explicite
      // pour que le verrou anti-mélange de framework (tabs/boutons désactivés)
      // s'active bien aussi pour une session créée par l'IA, pas seulement
      // via un clic manuel.
      if (typeof window.setProjectLoaded === 'function') window.setProjectLoaded(true);
      else window.projectLoaded = true; // repli si builder.html pas encore chargé/à jour
      // CORRECTIF : mémorise tout de suite l'origine réelle (mode) de cette
      // session dans le cache local partagé avec builder.html. Le serveur ne
      // renvoie pas forcément un `origin` fiable via /sessions pour un projet
      // créé par l'IA ; sans ce cache, un clic manuel ultérieur sur cette
      // session dans la liste "SESSIONS" retombait sur origin='' et ne
      // rebasculait jamais l'onglet hors de 'scratch' (voir resolveSessionOrigin
      // / rememberSessionOrigin dans builder.html).
      if (typeof window.rememberSessionOrigin === 'function') window.rememberSessionOrigin(session, mode);
      // CORRECTIF : c'est switchTopMode() qui bascule réellement l'onglet
      // de gauche (Scratch/Flutter/Cordova/...), affiche le bon panneau et
      // le bon état vide — sans cet appel, seul le panneau flottant IA
      // s'ouvrait et l'explorateur restait visuellement bloqué sur
      // 'Scratch' tant que le client ne cliquait pas lui-même sur l'onglet.
      if (typeof window.switchTopMode === 'function') window.switchTopMode(mode);
      // Puis on charge le vrai contenu du projet (arborescence de fichiers)
      // dans cet onglet désormais actif — sinon l'onglet change mais reste
      // vide jusqu'au prochain clic manuel.
      if (typeof window.loadTree === 'function') window.loadTree();
      if (typeof window.aiOnSessionChanged === 'function') window.aiOnSessionChanged(session);
      // Reflète dans le panneau "Identité" (nom, package, permissions...) la
      // config réellement utilisée par l'IA — sinon le panneau continue
      // d'afficher les valeurs par défaut du formulaire et un futur clic
      // manuel sur "Appliquer"/"Recompiler" écraserait silencieusement ce
      // que l'IA vient de faire.
      if (config && typeof window.aiSyncIdentityFields === 'function') window.aiSyncIdentityFields(config);
      const sp = document.getElementById('scratch-panel'); if (sp) sp.style.display = 'none';
      const ti = document.getElementById('template-import'); if (ti) ti.style.display = 'none';
      // RÈGLE : ne JAMAIS rabattre le mode réel sur 'scratch'. Un projet
      // cordova/flutter/reactnative créé par l'agent doit rester identifié
      // comme tel dans l'UI (onglet, bandeau, panneau workspace) — le
      // mélanger avec 'scratch' était la cause du dysfonctionnement décrit
      // par le client (page blanche : l'IA croyait éditer un espace
      // scratch alors qu'un autre pipeline était réellement actif).
      if (typeof window.aiWorkspaceShow === 'function') window.aiWorkspaceShow(mode, session);
      if (typeof window.updateBuildLabel === 'function') window.updateBuildLabel();
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
  let CURRENT_USER_TEXT = ''; // dernier message du client, utilisé pour déduire nom/package si le modèle ne le fait pas

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
  // Déduction minimale (nom d'app + package) à partir du texte du client —
  // utilisée à la fois par le rempart de confirmation d'identité et par le
  // filet de sécurité (réponse en texte libre sans appel d'outil). Ce n'est
  // qu'un repli grossier — mieux vaut un nom déduit imparfaitement que le
  // défaut générique "MonApp".
  function inferAppIdentityFromText(text) {
    const STOP = new Set(['une','un','de','pour','application','app','apk','crée','cree','créer','creer','moi','le','la','les','des','du','au','aux','avec','fais','fait','faire','génère','genere','construis','build']);
    const words = (text || '')
      .normalize('NFD').replace(/[\u0300-\u036f]/g, '') // accents → ascii
      .replace(/[^a-zA-Z0-9\s]/g, ' ')
      .split(/\s+/)
      .filter(w => w && w.length > 2 && !STOP.has(w.toLowerCase()));
    if (!words.length) return {};
    const core = words.slice(0, 3);
    const appName = core.map(w => w[0].toUpperCase() + w.slice(1).toLowerCase()).join(' ');
    const slug = core.join('').toLowerCase();
    return slug ? { appName, packageName: `com.${slug}.app` } : {};
  }

  // Détecte, dans le texte brut du client, quel TYPE d'espace de travail a
  // été explicitement demandé (cordova / flutter / reactnative / twa).
  // Sert à empêcher le modèle de retomber sur 'scratch' par réflexe alors
  // que le client a nommé un type précis. Volontairement STRICT : ne
  // matche que des mots-clés techniques non ambigus (cordova, flutter,
  // "react native") — PAS "natif"/"native" seuls, car ce sont des mots
  // courants pouvant apparaître dans un nom d'app choisi par le client
  // (ex: une app de test nommée "Natif" alors qu'elle est en mode
  // scratch) : les bloquer sur un simple mot causait des faux positifs
  // qui cassaient la création et empêchaient la proposition d'identité
  // de s'afficher. Retourne null si rien de non ambigu n'est détecté —
  // dans ce cas scratch reste un choix légitime, aucun blocage.
  // Distance de Levenshtein minimale, juste assez pour comparer des mots
  // courts (noms de frameworks) — pas besoin d'une lib externe pour ça.
  function _levenshtein(a, b) {
    if (a === b) return 0;
    const al = a.length, bl = b.length;
    if (al === 0) return bl;
    if (bl === 0) return al;
    let prev = Array.from({ length: bl + 1 }, (_, i) => i);
    for (let i = 1; i <= al; i++) {
      const cur = [i];
      for (let j = 1; j <= bl; j++) {
        cur[j] = a[i - 1] === b[j - 1]
          ? prev[j - 1]
          : 1 + Math.min(prev[j - 1], prev[j], cur[j - 1]);
      }
      prev = cur;
    }
    return prev[bl];
  }

  // BUG CORRIGÉ : inferApkTypeFromText ne matchait QUE l'orthographe exacte
  // du framework ("flutter", "cordova"...) via regex \b...\b. Une simple
  // faute de frappe du client (ex: "futter" au lieu de "flutter") faisait
  // échouer silencieusement toute la détection : demanded devenait null,
  // enforceApkTypeMatch ne bloquait donc plus rien, et l'IA (qui elle
  // comprend la faute de frappe sémantiquement) pouvait alors écrire un
  // vrai projet Flutter dans une session scratch sans qu'aucun garde-fou
  // code ne s'active — exactement le bug constaté en pratique. On tolère
  // maintenant les fautes de frappe proches (distance de Levenshtein ≤ 2
  // pour un mot d'au moins 5 lettres, ≤ 1 sinon) sur CHAQUE mot du texte,
  // en plus du match exact existant. "react native" reste géré séparément
  // (deux mots, tolérance appliquée sur leur concatenation).
  function _fuzzyWordMatches(word, keyword) {
    if (!word) return false;
    if (word === keyword) return true;
    if (Math.abs(word.length - keyword.length) > 2) return false; // écart trop grand : pas une simple faute de frappe
    const maxDist = keyword.length >= 5 ? 2 : 1;
    return _levenshtein(word, keyword) <= maxDist;
  }
  function inferApkTypeFromText(text) {
    const t = (text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    // 1) Match exact strict (chemin rapide, comportement historique inchangé).
    if (/\bcordova\b/.test(t)) return 'cordova';
    if (/\bflutter\b/.test(t)) return 'flutter';
    if (/react[\s-]?native\b/.test(t)) return 'reactnative';
    if (/\btwa\b/.test(t)) return 'twa';
    // 2) Tolérance fautes de frappe : on teste chaque mot isolé du texte
    //    contre 'cordova'/'flutter'/'twa', et chaque paire de mots contigus
    //    contre 'react native'.
    const words = t.replace(/[^a-z\s]/g, ' ').split(/\s+/).filter(Boolean);
    for (const w of words) {
      if (w.length < 3) continue; // trop court pour une comparaison fiable
      if (_fuzzyWordMatches(w, 'cordova')) return 'cordova';
      if (_fuzzyWordMatches(w, 'flutter')) return 'flutter';
      if (_fuzzyWordMatches(w, 'twa')) return 'twa';
    }
    for (let i = 0; i < words.length - 1; i++) {
      const pair = words[i] + words[i + 1];
      if (_fuzzyWordMatches(pair, 'reactnative')) return 'reactnative';
    }
    return null; // 'native'/'natif' seuls volontairement exclus : trop ambigus (voir commentaire ci-dessus)
  }

  // Historique cumulé du texte du client sur TOUTE la conversation (pas
  // juste le dernier message) — évite de perdre la demande explicite
  // ("flutter", "cordova"...) dès que le client enchaîne avec un message
  // qui ne répète pas le mot-clé ("continue", "ajoute une page"...).
  // Alimenté à chaque tour par runAgentLoop, jamais réinitialisé pendant
  // une session de chat.
  let ALL_USER_TEXT = '';

  // Jeton mémorisant, PAR SESSION, le type déjà validé/ouvert. Sert
  // uniquement à savoir de quel type une session EXISTANTE est déjà —
  // PAS à désactiver la vérification : si le client réclame explicitement
  // un autre type que celui de la session en cours, le garde-fou doit
  // continuer à se déclencher (avant, une session partie par erreur en
  // 'scratch' restait bloquée sur ce mode pour toujours).
  const sessionApkType = new Map(); // session -> mode

  // CORRECTIF : sessionApkType est un Map en mémoire, vidé à chaque
  // rechargement de page — il ne connaissait donc le mode d'une session que
  // si elle avait été créée PENDANT la session de chat en cours. Dès que
  // l'IA reprenait un projet existant via select_session (créé plus tôt,
  // ou par un clic manuel du client), sessionApkType.get(sid) renvoyait
  // undefined : le garde-fou de type (enforceApkTypeMatch) ET le garde-fou
  // de chemin d'injection (enforceApkPathMatch) se désactivaient tous les
  // deux silencieusement. On complète donc systématiquement par le cache
  // localStorage partagé avec builder.html (rememberSessionOrigin/
  // resolveSessionOrigin, alimenté aussi par les clics manuels sur la liste
  // "SESSIONS"), qui lui survit aux rechargements.
  function getSessionMode(sid) {
    if (!sid) return null;
    const known = sessionApkType.get(sid);
    if (known) return known;
    const cached = (typeof window.resolveSessionOrigin === 'function') ? window.resolveSessionOrigin(sid) : null;
    if (cached) sessionApkType.set(sid, cached);
    return cached || null;
  }

  // Garde-fou CODE (pas juste prompt) : bloque create_project/build_project
  // si le client a explicitement nommé un type (cordova/flutter/reactnative/
  // twa) — n'importe où dans la conversation — et que le modèle tente
  // quand même un autre mode (typiquement 'scratch' par réflexe).
  // Le client peut aussi choisir le type d'APK à la main via le sélecteur
  // au-dessus du champ de chat (builder.html) plutôt que de compter sur la
  // détection par mots-clés dans le texte. Ce choix manuel, s'il est actif,
  // prime toujours sur inferApkTypeFromText — un select explicite est un
  // signal encore plus sûr qu'un mot-clé dans une phrase.
  function forcedApkType() {
    return (typeof window.aiForcedApkType === 'function') ? window.aiForcedApkType() : null;
  }

  function enforceApkTypeMatch(requestedMode, sid) {
    const demanded = forcedApkType() || inferApkTypeFromText(ALL_USER_TEXT || CURRENT_USER_TEXT);
    if (!demanded) return null; // rien d'explicite : scratch reste un choix légitime
    const known = getSessionMode(sid);
    if (known === demanded) return null; // session déjà du bon type : rien à faire
    if (requestedMode !== demanded) {
      return {
        blocked: true,
        reason:
          `Le client a explicitement demandé un projet de type '${demanded}', pas '${requestedMode}'. ` +
          `Annule cet appel et relance immédiatement avec mode:'${demanded}' (create_project pour cordova/flutter/reactnative, ` +
          `ou directement build_project pour twa) — n'ouvre JAMAIS un autre espace de travail (notamment scratch) pour une demande ${demanded}.`,
      };
    }
    return null;
  }

  // Composants requis PAR TYPE d'espace de travail — sert à bloquer
  // create_project/build_project AVANT même d'appeler le serveur si un
  // outil nécessaire n'est pas installé, au lieu de laisser l'appel
  // échouer en cours de route (ou pire, tourner en boucle). Les
  // mots-clés sont testés contre l'id ET le label renvoyés par
  // check_missing_components (setupListComponents), pas contre une
  // liste d'ids figée — plus robuste si l'installateur renomme un id.
  // Composants requis PAR TYPE d'espace de travail — ids EXACTS tels que
  // définis dans setup.js (COMPONENTS = { python, jdk, apktool, bundletool,
  // androidSdk, gradle, jadx, nodejs, flutter, cordova, reactNativeCli,
  // bubblewrap }). zipalign/apksigner ne sont PAS des composants à part :
  // ils font partie des build-tools installés avec 'androidSdk' (voir
  // find_tool('zipalign')/find_tool('apksigner') côté server.py) — donc pas
  // de clé séparée ici, seule 'androidSdk' est vérifiée.
  // dependsOn de setup.js déjà inclus explicitement pour chaque mode, pour
  // que le message de blocage liste bien TOUT ce qui manque en une fois
  // (androidSdk dépend lui-même de jdk, cordova/reactNativeCli/bubblewrap
  // dépendent de nodejs+jdk+androidSdk(+gradle) — cf. dependsOn dans setup.js).
  const REQUIRED_COMPONENT_KEYWORDS_BY_MODE = {
    scratch: [['apktool'], ['jdk'], ['androidSdk']],
    native: [['jdk'], ['androidSdk'], ['gradle']],
    twa: [['nodejs'], ['bubblewrap'], ['jdk'], ['androidSdk']],
    cordova: [['nodejs'], ['cordova'], ['jdk'], ['androidSdk'], ['gradle']],
    flutter: [['flutter'], ['jdk'], ['androidSdk']],
    reactnative: [['nodejs'], ['reactNativeCli'], ['jdk'], ['androidSdk'], ['gradle']],
    // BETA — ces 3 modes ne sont pas encore forcément dans le registre de
    // setup.js (nativescript/dotnet/titanium) : si aucun composant ne
    // correspond à ces mots-clés, enforceComponentsForMode ne bloquera pas
    // à tort, mais ne préviendra pas non plus avant le build — l'échec
    // remonterait alors depuis do_build_* côté serveur (find_nativescript()
    // etc. absent). Mets à jour cette liste dès que ces composants seront
    // ajoutés à setup.js pour retrouver la vérification proactive.
    nativescript: [['nodejs'], ['nativescript'], ['jdk'], ['androidSdk'], ['gradle']],
    maui: [['dotnet'], ['maui']],
    titanium: [['nodejs'], ['titanium'], ['jdk'], ['androidSdk']],
  };

  // Garde-fou CODE : avant de lancer create_project ou build_project pour
  // un mode donné, vérifie que les composants qu'IL nécessite sont bien
  // installés. Si l'installateur n'est pas accessible (hors app desktop),
  // on ne peut pas vérifier : on laisse passer plutôt que de bloquer à
  // tort. Si un ou plusieurs composants requis manquent, on bloque avec
  // la raison précise (nom exact affiché dans l'UI) — jamais de
  // tentative silencieuse qui échouerait plus tard avec un message
  // cryptique côté serveur.
  async function enforceComponentsForMode(mode) {
    const required = REQUIRED_COMPONENT_KEYWORDS_BY_MODE[mode];
    if (!required) return null; // mode inconnu de cette table : rien à vérifier ici
    if (!(window.electronAPI && window.electronAPI.setupListComponents)) return null; // pas vérifiable dans ce contexte
    let list;
    try {
      list = await window.electronAPI.setupListComponents();
    } catch (e) {
      return null; // en cas d'erreur de l'installateur lui-même, ne pas bloquer à tort — le build révélera l'éventuel manque
    }
    const missingAll = list.filter(c => !c.installed);
    const missingForMode = [];
    for (const keywords of required) {
      const hit = missingAll.find(c => {
        const hay = `${c.id || ''} ${c.label || ''}`.toLowerCase();
        return keywords.some(k => hay.includes(k.toLowerCase()));
      });
      if (hit) missingForMode.push(hit.label || hit.id);
    }
    if (!missingForMode.length) return null;
    const uniqueMissing = [...new Set(missingForMode)];
    return {
      blocked: true,
      missing: uniqueMissing,
      reason:
        `Le type d'APK '${mode}' nécessite ${uniqueMissing.length > 1 ? 'les composants suivants, non installés' : 'le composant suivant, non installé'} : ` +
        `${uniqueMissing.join(', ')}. N'appelle PAS create_project/build_project pour ce mode tant que ce n'est pas résolu. ` +
        `Appelle install_components avec le(s) id(s) correspondant(s) (vus via check_missing_components) pour tenter une installation automatique ; ` +
        `si l'installation échoue, dis au client en clair quel(s) composant(s) précis manquent et invite-le à ouvrir l'onglet Composants & plateformes pour les installer manuellement — ne relance jamais l'appel bloqué en boucle.`,
    };
  }

  // Racine de fichiers attendue par type — sert de garde-fou secondaire
  // dans write_file/rename/duplicate : si une session est connue comme
  // 'cordova' (par ex.) mais qu'on tente d'y écrire un chemin qui ressemble
  // à la structure 'scratch' (assets/app.js, index.html/style.css à la
  // racine), c'est le signe d'une session active désynchronisée (ex: le
  // global window._currentSessionId pointait encore sur l'ancienne session
  // scratch) — on bloque plutôt que d'écrire silencieusement au mauvais
  // endroit.
  const WS_ROOT_BY_MODE = {
    scratch: 'assets/',
    cordova: 'www/',
    flutter: 'assets/www/',
    reactnative: 'android/app/src/main/assets/www/',
    nativescript: 'app/assets/www/',
    maui: 'Resources/Raw/www/',
    titanium: 'Resources/www/',
  };
  // BUG CORRIGÉ (cause du bug "Flutter écrit dans une session scratch") :
  // WS_ROOT_ALLOWLIST était une liste UNIQUE, partagée par tous les modes.
  // Résultat : 'pubspec.yaml' (marqueur Flutter) passait le garde-fou même
  // dans une session 'scratch', simplement parce qu'il figurait dans la
  // liste blanche "générique". Chaque fichier racine n'est légitime que
  // pour SON mode — la liste est donc maintenant PAR MODE.
  const WS_ROOT_ALLOWLIST_BY_MODE = {
    scratch:     [],
    cordova:     ['config.xml', 'AndroidManifest.xml', 'build.gradle'],
    flutter:     ['pubspec.yaml', 'AndroidManifest.xml', 'build.gradle'],
    reactnative: ['package.json', 'app.json', 'AndroidManifest.xml', 'build.gradle'],
    nativescript: ['package.json', 'nativescript.config.ts', 'AndroidManifest.xml', 'build.gradle'],
    maui:        ['MainPage.xaml', 'MainPage.xaml.cs', 'MauiProgram.cs'],
    titanium:    ['tiapp.xml', 'app.js'],
  };
  // Marqueurs de framework à détecter n'IMPORTE OÙ dans l'arbre (pas
  // seulement à la racine) pour repérer une session désynchronisée même
  // quand le chemin ne ressemble pas à un webroot connu — c'est précisément
  // ce qui manquait pour 'lib/main.dart', 'lib/models/note.dart', etc.
  // (aucun de ces chemins ne "ressemblait" à assets/www/, donc passaient
  // inaperçus avant ce correctif).
  const WS_FRAMEWORK_MARKERS = {
    flutter:     (p) => p === 'pubspec.yaml' || p.endsWith('/pubspec.yaml') || p.endsWith('.dart'),
    cordova:     (p) => p === 'config.xml' || p.endsWith('/config.xml'),
    reactnative: (p) => p === 'package.json' || p.endsWith('/package.json') ||
                        /(^|\/)App\.(js|tsx|jsx)$/.test(p),
    nativescript: (p) => p === 'nativescript.config.ts' || p.endsWith('/nativescript.config.ts') ||
                         /(^|\/)main-page\.xml$/.test(p),
    maui:        (p) => p.endsWith('.csproj') || /(^|\/)MauiProgram\.cs$/.test(p) || /(^|\/)MainPage\.xaml$/.test(p),
    titanium:    (p) => p === 'tiapp.xml' || p.endsWith('/tiapp.xml'),
  };

  function enforceApkPathMatch(sid, path) {
    const mode = getSessionMode(sid);
    const cleanPath = String(path || '').replace(/^\/+/, '');

    // Vérif marqueurs de framework : indépendante de expectedRoot, donc
    // s'applique même si le mode courant n'a pas de racine dédiée (ex:
    // 'native') ou si le mode est totalement inconnu (session jamais
    // taguée) — un fichier .dart/pubspec.yaml/config.xml n'a JAMAIS sa
    // place hors d'une session de son propre framework.
    for (const [fwMode, isMarker] of Object.entries(WS_FRAMEWORK_MARKERS)) {
      if (fwMode !== mode && isMarker(cleanPath)) {
        return {
          blocked: true,
          reason:
            `Chemin refusé : '${path}' est un fichier propre au framework '${fwMode}', mais la session active est ` +
            `de type '${mode || 'inconnu'}'. Écrire ce fichier ici produirait un projet hybride incohérent qui ne compilera pas. ` +
            `Appelle d'abord create_project(mode:'${fwMode}', ...) pour créer une VRAIE session ${fwMode}, ` +
            `puis réécris ce fichier (et tous les autres fichiers ${fwMode} déjà prévus) dans cette nouvelle session.`,
        };
      }
    }

    if (!mode) return null; // type de session inconnu (ex: projet natif/décompilé) : pas de vérification de racine web
    const expectedRoot = WS_ROOT_BY_MODE[mode];
    if (!expectedRoot) return null; // mode sans racine dédiée (native/twa) : pas de vérification
    if (cleanPath.startsWith(expectedRoot)) return null;
    const allowlist = WS_ROOT_ALLOWLIST_BY_MODE[mode] || [];
    if (allowlist.some(f => cleanPath === f || cleanPath.endsWith('/' + f))) return null;
    // Chemin qui correspond à la racine d'UN AUTRE mode connu : signe quasi
    // certain d'une session désynchronisée plutôt que d'un fichier de config
    // légitime qu'on n'aurait pas listé.
    const looksLikeOtherMode = Object.entries(WS_ROOT_BY_MODE).some(
      ([m, root]) => m !== mode && (cleanPath.startsWith(root) || (m === 'scratch' && /^(index\.html|style\.css|app\.js)$/.test(cleanPath)))
    );
    if (looksLikeOtherMode) {
      return {
        blocked: true,
        reason:
          `La session active est de type '${mode}' (racine attendue : '${expectedRoot}'), mais le chemin '${path}' correspond à la structure d'un AUTRE type de projet. ` +
          `C'est le signe que la session active n'est plus la bonne — appelle select_session avec l'identifiant de la session '${mode}' réellement voulue, ou passe explicitement 'session' dans cet appel, avant de réessayer avec un chemin sous '${expectedRoot}'.`,
      };
    }
    return null; // chemin inconnu mais pas manifestement d'un autre mode : on laisse passer
  }

  // Mémorise, PAR PACKAGE, qu'une identité (nom+package) a été validée par
  // le client — pour ne plus jamais réafficher la carte de confirmation
  // pour la même app, et pour permettre au CODE (pas juste au prompt) de
  // bloquer la création tant que ce n'est pas fait, quel que soit le
  // modèle utilisé et sa capacité à suivre l'instruction "appelle
  // propose_identity en premier".
  // Confirmation d'identité à USAGE UNIQUE, en mémoire (jamais en
  // localStorage) : la carte "nom d'app / package" doit s'afficher à
  // CHAQUE nouvelle création, sans exception — y compris en pilote
  // automatique, et même si un package identique a déjà été confirmé pour
  // une app précédente. Le clic "✅ Continuer" ne fait que débloquer
  // l'appel create_project/build_project qui suit immédiatement dans la
  // même série d'actions ; le jeton est consommé dès qu'il est utilisé
  // (voir enforceIdentityConfirmation), donc la prochaine création reposera
  // la question, même pour le même package.
  const pendingIdentityConfirmations = new Map(); // clé package -> true (jeton à usage unique)
  function identityConfirmedFor(pkg) { return pendingIdentityConfirmations.has(pkg || 'default'); }
  function markIdentityConfirmed(pkg) { pendingIdentityConfirmations.set(pkg || 'default', true); }
  function consumeIdentityConfirmation(pkg) { pendingIdentityConfirmations.delete(pkg || 'default'); }

  // Widget de confirmation d'IDENTITÉ (nom + package) affiché DIRECTEMENT
  // dans le fil de chat — pas une modale plein écran, pas une question
  // ouverte : deux boutons, comme un choix à cases proposé par Claude. Le
  // clic renvoie automatiquement un message dans le chat, ce qui relance
  // normalement la boucle agent (donc pas de code spécial de "pause").
  function sendCannedChatMessage(text) {
    const input = document.getElementById('ai-input-text');
    if (!input) return;
    input.value = text;
    if (typeof window.aiSendMessage === 'function') window.aiSendMessage();
  }
  // Assistant en 3 CARTES SUCCESSIVES : identité → permissions →
  // icône/splash/manifest. Chaque étape a "✅ Continuer" / "✏️ Changer",
  // plus un lien "⏭ Ignorer tout le reste" (visible dès l'étape 1) qui
  // saute directement à la finalisation avec les valeurs par défaut sur
  // les étapes restantes — le client n'a pas besoin de cliquer 3 fois
  // "Continuer" s'il n'a rien à changer.
  function renderSkipAllLink(container, onSkip) {
    const a = document.createElement('a');
    a.href = '#';
    a.style.cssText = 'font-size:12px;opacity:.75;margin-left:4px;text-decoration:underline;cursor:pointer;';
    a.textContent = '⏭ Ignorer tout le reste (valeurs par défaut)';
    a.onclick = (e) => { e.preventDefault(); onSkip(); };
    container.appendChild(a);
  }

  // Permissions Android les plus courantes, proposées comme cases à
  // cocher à l'étape 2 (en plus d'un champ libre pour tout le reste).
  const COMMON_ANDROID_PERMS = [
    'CAMERA', 'RECORD_AUDIO', 'ACCESS_FINE_LOCATION', 'ACCESS_COARSE_LOCATION',
    'READ_EXTERNAL_STORAGE', 'WRITE_EXTERNAL_STORAGE', 'READ_CONTACTS',
    'READ_MEDIA_IMAGES', 'CALL_PHONE', 'READ_PHONE_STATE', 'POST_NOTIFICATIONS',
    'INTERNET', 'VIBRATE', 'BLUETOOTH',
  ];

  function shortPerm(p) { return String(p).replace('android.permission.', ''); }
  function fullPerm(p) { const s = String(p).trim(); return s.startsWith('android.permission.') ? s : `android.permission.${s.replace(/\s+/g, '_').toUpperCase()}`; }

  function styledInput() {
    const input = document.createElement('input');
    input.type = 'text';
    input.style.cssText = 'padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.06);color:inherit;font:inherit;min-width:220px;';
    return input;
  }

  function renderIdentityWizard(proposal) {
    const state = {
      appName: proposal.appName,
      packageName: proposal.packageName,
      permissions: (proposal.permissions || []).map(fullPerm),
      assetPlan: proposal.assetPlan || "icône/splash générés automatiquement (IA, ou icône vectorielle de secours si crédits insuffisants), permissions déclarées dans le manifest",
      selectedIconSvg: null,
      selectedIconName: null,
    };

    function finalize() {
      markIdentityConfirmed(state.packageName);
      let msg = `Confirmé : nom "${state.appName}", package "${state.packageName}", permissions [${state.permissions.join(', ') || 'aucune'}], asset/manifest : ${state.assetPlan}.`;
      if (state.selectedIconSvg) {
        msg += `\n\nUtilise EXACTEMENT ce SVG comme fichier assets/icon.svg (ne le régénère pas, écris-le tel quel, sans le modifier) :\n\`\`\`svg\n${state.selectedIconSvg}\n\`\`\``;
      }
      msg += `\nContinue le travail jusqu'au bout.`;
      sendCannedChatMessage(msg);
    }

    function stepIdentity() {
      const div = window.renderAIMessage('assistant',
        `📛 Étape 1/3 — Identité : nom d'app **"${state.appName}"**, package **"${state.packageName}"**${proposal.reason ? ` (${proposal.reason})` : ''}.`);
      const box = document.createElement('div');
      box.className = 'ai-img-actions'; box.style.cssText = 'margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;';
      box.innerHTML = `<button data-act="ok">✅ Continuer</button><button data-act="edit">✏️ Changer le nom/package</button>`;
      box.querySelector('[data-act="ok"]').onclick = () => { div.remove(); stepPermissions(); };
      box.querySelector('[data-act="edit"]').onclick = () => {
        box.innerHTML = '';
        box.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:6px;align-items:flex-start;';

        const nameLabel = document.createElement('label'); nameLabel.textContent = "Nom de l'app"; nameLabel.style.cssText = 'font-size:12px;opacity:.75;';
        const nameInput = styledInput(); nameInput.value = state.appName;

        const pkgLabel = document.createElement('label'); pkgLabel.textContent = 'Package (ex : com.exemple.monapp)'; pkgLabel.style.cssText = 'font-size:12px;opacity:.75;margin-top:4px;';
        const pkgInput = styledInput(); pkgInput.value = state.packageName;

        const btnRow = document.createElement('div'); btnRow.style.cssText = 'display:flex;gap:8px;margin-top:6px;';
        const okBtn = document.createElement('button'); okBtn.textContent = '✅ Valider';
        const cancelBtn = document.createElement('button'); cancelBtn.textContent = '✖ Annuler';
        btnRow.appendChild(okBtn); btnRow.appendChild(cancelBtn);

        box.appendChild(nameLabel); box.appendChild(nameInput);
        box.appendChild(pkgLabel); box.appendChild(pkgInput);
        box.appendChild(btnRow);

        okBtn.onclick = () => {
          state.appName = nameInput.value.trim() || state.appName;
          state.packageName = pkgInput.value.trim() || state.packageName;
          div.remove();
          stepPermissions();
        };
        cancelBtn.onclick = () => { div.remove(); stepIdentity(); };
        nameInput.focus();
      };
      renderSkipAllLink(box, () => { div.remove(); finalize(); });
      div.appendChild(box);
    }

    function stepPermissions() {
      const list = state.permissions.length ? state.permissions.map(shortPerm).join(', ') : '(aucune permission sensible détectée)';
      const div = window.renderAIMessage('assistant', `🔐 Étape 2/3 — Permissions proposées : **${list}**.`);
      const box = document.createElement('div');
      box.className = 'ai-img-actions'; box.style.cssText = 'margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;';
      box.innerHTML = `<button data-act="ok">✅ Continuer</button><button data-act="edit">✏️ Changer les permissions</button>`;
      box.querySelector('[data-act="ok"]').onclick = () => { div.remove(); stepAsset(); };
      box.querySelector('[data-act="edit"]').onclick = () => {
        box.innerHTML = '';
        box.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:8px;align-items:flex-start;max-width:420px;';

        const grid = document.createElement('div');
        grid.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px 14px;';
        const checkboxes = COMMON_ANDROID_PERMS.map(p => {
          const lbl = document.createElement('label');
          lbl.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:13px;cursor:pointer;';
          const cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = p;
          cb.checked = state.permissions.includes(fullPerm(p));
          lbl.appendChild(cb); lbl.appendChild(document.createTextNode(p));
          grid.appendChild(lbl);
          return cb;
        });

        const extraLabel = document.createElement('label');
        extraLabel.textContent = 'Autres permissions (séparées par des virgules)';
        extraLabel.style.cssText = 'font-size:12px;opacity:.75;';
        const extraInput = styledInput(); extraInput.style.minWidth = '320px';
        const knownFull = COMMON_ANDROID_PERMS.map(fullPerm);
        extraInput.value = state.permissions.filter(p => !knownFull.includes(p)).map(shortPerm).join(', ');

        const btnRow = document.createElement('div'); btnRow.style.cssText = 'display:flex;gap:8px;';
        const okBtn = document.createElement('button'); okBtn.textContent = '✅ Valider';
        const cancelBtn = document.createElement('button'); cancelBtn.textContent = '✖ Annuler';
        btnRow.appendChild(okBtn); btnRow.appendChild(cancelBtn);

        box.appendChild(grid); box.appendChild(extraLabel); box.appendChild(extraInput); box.appendChild(btnRow);

        okBtn.onclick = () => {
          const checked = checkboxes.filter(cb => cb.checked).map(cb => fullPerm(cb.value));
          const extra = extraInput.value.split(',').map(s => s.trim()).filter(Boolean).map(fullPerm);
          state.permissions = Array.from(new Set([...checked, ...extra]));
          div.remove();
          stepAsset();
        };
        cancelBtn.onclick = () => { div.remove(); stepPermissions(); };
      };
      renderSkipAllLink(box, () => { div.remove(); finalize(); });
      div.appendChild(box);
    }

    function stepAsset() {
      const div = window.renderAIMessage('assistant', `🎨 Étape 3/3 — Icône / splash / manifest : ${state.selectedIconName ? `icône choisie : ${state.selectedIconName}` : state.assetPlan}.`);
      const box = document.createElement('div');
      box.className = 'ai-img-actions'; box.style.cssText = 'margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;';
      box.innerHTML = `<button data-act="ok">✅ Continuer</button><button data-act="edit">✏️ Changer l'icône</button>`;
      box.querySelector('[data-act="ok"]').onclick = () => { div.remove(); finalize(); };
      box.querySelector('[data-act="edit"]').onclick = () => renderIconPicker(box, div, state, finalize, stepAsset);
      div.appendChild(box);
    }

    stepIdentity();
  }

  // Sélecteur d'icônes vectorielles cliquables (Iconify, aucune clé
  // requise) : champ de recherche + grille de résultats en SVG ; un clic
  // sélectionne l'icône (mise en évidence), un second bouton confirme et
  // récupère le SVG complet pour l'attacher à l'identité de l'app.
  function renderIconPicker(box, msgDiv, state, finalize, backToStepAsset) {
    box.innerHTML = '';
    box.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:8px;max-width:460px;';

    const searchRow = document.createElement('div'); searchRow.style.cssText = 'display:flex;gap:6px;';
    const searchInput = styledInput(); searchInput.value = state.appName || ''; searchInput.placeholder = 'ex : café, sport, musique…';
    const searchBtn = document.createElement('button'); searchBtn.textContent = '🔎 Rechercher';
    searchRow.appendChild(searchInput); searchRow.appendChild(searchBtn);

    const statusLine = document.createElement('div'); statusLine.style.cssText = 'font-size:12px;opacity:.75;';
    const grid = document.createElement('div');
    grid.style.cssText = 'display:grid;grid-template-columns:repeat(6,1fr);gap:6px;max-height:220px;overflow-y:auto;';

    const bottomRow = document.createElement('div'); bottomRow.style.cssText = 'display:flex;gap:8px;align-items:center;';
    const confirmBtn = document.createElement('button'); confirmBtn.textContent = '✅ Confirmer cette icône'; confirmBtn.disabled = true;
    const cancelBtn = document.createElement('button'); cancelBtn.textContent = '✖ Annuler';
    bottomRow.appendChild(confirmBtn); bottomRow.appendChild(cancelBtn);

    box.appendChild(searchRow); box.appendChild(statusLine); box.appendChild(grid); box.appendChild(bottomRow);

    let selectedIconId = null;

    async function runSearch() {
      const query = searchInput.value.trim();
      if (!query) { statusLine.textContent = 'Tape un mot-clé pour chercher une icône.'; return; }
      grid.innerHTML = ''; confirmBtn.disabled = true; selectedIconId = null;
      statusLine.textContent = '⏳ Recherche en cours…';
      try {
        const resp = await fetch('https://api.iconify.design/search?query=' + encodeURIComponent(query) + '&limit=24');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const icons = data?.icons || [];
        if (!icons.length) { statusLine.textContent = `Aucune icône trouvée pour "${query}". Essaie un autre mot-clé.`; return; }
        statusLine.textContent = `${icons.length} icône(s) trouvée(s) pour "${query}" — clique pour choisir.`;
        icons.forEach(iconId => {
          const cell = document.createElement('div');
          cell.style.cssText = 'border:1px solid rgba(255,255,255,.15);border-radius:8px;padding:6px;display:flex;align-items:center;justify-content:center;cursor:pointer;background:rgba(255,255,255,.04);';
          cell.title = iconId;
          const img = document.createElement('img');
          img.src = `https://api.iconify.design/${iconId.replace(':', '/')}.svg?color=%23ffffff`;
          img.width = 28; img.height = 28; img.loading = 'lazy';
          img.onerror = () => { cell.style.opacity = '.3'; cell.style.cursor = 'default'; cell.onclick = null; };
          cell.appendChild(img);
          cell.onclick = () => {
            grid.querySelectorAll('[data-selected="1"]').forEach(c => { c.dataset.selected = '0'; c.style.borderColor = 'rgba(255,255,255,.15)'; });
            cell.dataset.selected = '1'; cell.style.borderColor = '#4caf50';
            selectedIconId = iconId;
            confirmBtn.disabled = false;
          };
          grid.appendChild(cell);
        });
      } catch (e) {
        statusLine.textContent = `⚠ Recherche impossible (${e.message}). Vérifie ta connexion et réessaie.`;
      }
    }

    searchBtn.onclick = runSearch;
    searchInput.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); runSearch(); } };

    confirmBtn.onclick = async () => {
      if (!selectedIconId) return;
      confirmBtn.disabled = true; confirmBtn.textContent = '⏳ Récupération…';
      try {
        const [prefix, name] = selectedIconId.split(':');
        const svgResp = await fetch(`https://api.iconify.design/${prefix}/${name}.svg`);
        if (!svgResp.ok) throw new Error(`HTTP ${svgResp.status}`);
        const svg = await svgResp.text();
        state.selectedIconSvg = svg;
        state.selectedIconName = selectedIconId;
        msgDiv.remove();
        backToStepAsset();
      } catch (e) {
        statusLine.textContent = `⚠ Impossible de récupérer ce SVG (${e.message}). Choisis une autre icône.`;
        confirmBtn.disabled = false; confirmBtn.textContent = '✅ Confirmer cette icône';
      }
    };

    cancelBtn.onclick = () => { msgDiv.remove(); backToStepAsset(); };

    if (searchInput.value) runSearch();
  }

  // Rempart CÔTÉ CODE (pas seulement côté prompt) : si le modèle appelle
  // directement create_project/build_project sans être passé par
  // propose_identity ET sans confirmation déjà connue pour ce package, on
  // affiche nous-mêmes la carte et on empêche la création — quel que soit
  // le modèle utilisé et sa discipline à suivre les instructions.
  function enforceIdentityConfirmation(config, fallbackKey) {
    const explicitPkg = config && config.packageName;
    const inferred = inferAppIdentityFromText(CURRENT_USER_TEXT);
    const appName = (config && config.appName) || inferred.appName || 'App';
    const packageName = explicitPkg || inferred.packageName || fallbackKey || 'default';
    if (identityConfirmedFor(packageName)) {
      consumeIdentityConfirmation(packageName); // jeton à usage unique : consommé immédiatement
      return null; // validé pour CETTE création précise, on laisse passer une fois
    }
    renderIdentityWizard({ appName, packageName, reason: 'proposition automatique avant création', permissions: (config && config.permissions) || [], assetPlan: (config && config.assetPlan) || undefined });
    return {
      blocked: true,
      appName, packageName,
      note: "Carte de confirmation affichée dans le chat — la création n'a PAS été lancée. Termine ta réponse ici sans appeler d'autre outil ; attends la réponse du client (bouton ou message).",
    };
  }

  function esc2(s) { const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }

  // Choix à plusieurs options (pas juste oui/non), ex: "clé existante" vs
  // "nouvelle signature dédiée". Retourne la valeur (option.value) choisie,
  // ou null si le client ferme sans choisir.
  function agentChoice(question, detail, options) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.style.cssText = `
        position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:99999;
        display:flex; align-items:center; justify-content:center;`;
      const btns = options.map((o, i) =>
        `<button data-idx="${i}" style="padding:9px 14px;border-radius:6px;border:none;cursor:pointer;font-weight:600;
          background:${i === options.length - 1 ? 'var(--accent,#3ddc84)' : '#2a2a2f'};
          color:${i === options.length - 1 ? '#0c1f14' : '#eee'};
          ${i === options.length - 1 ? '' : 'border:1px solid #444;'}">${esc2(o.label)}</button>`
      ).join('');
      overlay.innerHTML = `
        <div style="background:#1b1b1f;color:#eee;max-width:440px;width:90%;
                    border-radius:10px;padding:20px;box-shadow:0 10px 40px rgba(0,0,0,.5);
                    font-family:inherit;">
          <div style="font-weight:700;font-size:15px;margin-bottom:10px;">🔐 ${esc2(question)}</div>
          ${detail ? `<div style="font-size:13px;line-height:1.5;opacity:.8;margin-bottom:16px;white-space:pre-wrap;">${esc2(detail)}</div>` : ''}
          <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;">${btns}</div>
        </div>`;
      overlay.addEventListener('click', (e) => {
        const idx = e.target?.dataset?.idx;
        if (idx === undefined) return;
        document.body.removeChild(overlay);
        resolve(options[parseInt(idx, 10)].value);
      });
      document.body.appendChild(overlay);
    });
  }

  // Mémorise, PAR PACKAGE (donc par app/template), quelle clé de signature
  // utiliser une fois pour toutes — pour ne plus jamais reposer la question
  // sur les builds/modifications suivants du même projet.
  const LS_SIGNING_MAP = 'apkfactory_signing_key_by_package';
  function readSigningMap() { try { return JSON.parse(localStorage.getItem(LS_SIGNING_MAP) || '{}'); } catch (e) { return {}; } }
  function getSigningKeyFor(pkg) { return readSigningMap()[pkg || 'default'] || null; }
  function setSigningKeyFor(pkg, key) {
    const m = readSigningMap(); m[pkg || 'default'] = key; localStorage.setItem(LS_SIGNING_MAP, JSON.stringify(m));
  }

  // Détermine (en demandant UNE SEULE fois par app, jamais plus) quelle clé
  // de signature de production utiliser pour ce packageName, puis mémorise
  // le choix pour que les prochains builds/modifs de la même app se signent
  // automatiquement, sans aucune fenêtre ni question.
  async function resolveSigningKey(packageName) {
    const pkg = packageName || 'default';
    const known = getSigningKeyFor(pkg);
    if (known) return known; // déjà décidé pour cette app — silence total désormais

    let existingKeys = [];
    try {
      if (window.electronAPI && typeof window.electronAPI.releaseKeystoreList === 'function') {
        existingKeys = await window.electronAPI.releaseKeystoreList();
      }
    } catch (e) { /* pas bloquant */ }

    if (!existingKeys.length) {
      // Toute première signature de production sur cette machine : rien à
      // choisir, on configure directement (fenêtre native une seule fois).
      setSigningKeyFor(pkg, 'default');
      return 'default';
    }

    // Au moins une autre app a déjà une signature configurée sur cette
    // machine : on demande UNE FOIS, dans le chat, laquelle utiliser pour
    // CETTE app avant de continuer — ensuite ce sera automatique.
    const labelList = existingKeys.map(k => k.alias || k.key).join(', ');
    const choice = await agentChoice(
      'Signature de production pour cette app',
      `Package : ${pkg}\nUne ou plusieurs clés de signature existent déjà sur cette machine (${labelList}).\nVeux-tu réutiliser une clé existante, ou créer une signature dédiée à cette app ?`,
      [
        { label: '🆕 Nouvelle signature dédiée', value: pkg },
        { label: '♻️ Réutiliser la clé existante', value: existingKeys[0].key },
      ]
    );
    const finalKey = choice || pkg;
    setSigningKeyFor(pkg, finalKey);
    return finalKey;
  }


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
    "customPermissions (chaîne, permissions non listées ci-dessus séparées par virgule ou retour à la ligne, ex: nom de permission personnalisée d'un plugin tiers). " +
    "packageName : pour une NOUVELLE app, si le client n'en donne aucun, déduis-en un cohérent toi-même (ex: com.<société ou mot court sans espace/accent>.<nomapp>) sans demander — jamais de valeur placeholder du type com.exemple.monapp. Pour un projet EXISTANT que tu modifies, NE CHANGE JAMAIS le packageName sauf si le client le demande explicitement : le changer casse la continuité des mises à jour Play Store et l'association avec la clé de signature déjà utilisée. " +
    "appName : OBLIGATOIRE et jamais laissé à une valeur générique par défaut ('MonApp'/'MyApp') — déduis toujours un vrai nom depuis le contenu/objectif de l'app décrit par le client (ex: une app de livraison → 'Allô Livreur', une app de recettes de cuisine béninoise → un nom évocateur en rapport). Si le client donne un nom explicite, utilise-le tel quel.";

  const TOOLS = [
    {
      type: 'function',
      function: {
        name: 'get_project_overview',
        description: "Vue d'ensemble complète d'une session : type de pipeline (scratch/natif/TWA/cordova/flutter/react native), arborescence des fichiers, contenu des fichiers de config clés (manifest, gradle, config.xml, pubspec.yaml, package.json...), nombre de valeurs smali modifiables, état de santé du projet, ET champ 'entrypoint' ({activeIndexPath, contentMissing}) qui dit si le point d'entrée web réel de CE projet a déjà un vrai contenu. À appeler EN PREMIER dès qu'un projet existant est concerné, avant de lire des fichiers un par un — et à relire après chaque write_file important sur le point d'entrée : si entrypoint.contentMissing est true, écris le vrai contenu dans entrypoint.activeIndexPath AVANT d'appeler build_project (sinon build_project refusera de lancer la compilation).",
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
            mode: { type: 'string', enum: ['scratch', 'cordova', 'flutter', 'reactnative', 'nativescript', 'maui', 'titanium'] },
            config: {
              type: 'object',
              description: CONFIG_FIELDS_DOC,
            },
            icon: { type: 'string', description: "Icône en base64 brut (sans préfixe data:...), optionnelle dès la création. Si le client n'a rien fourni, génère-en une avec generate_app_image(purpose:'icon') avant d'appeler create_project." },
            splash: { type: 'string', description: "Splash en base64 brut (sans préfixe data:...), optionnel dès la création. Si le client n'a rien fourni, génère-en un avec generate_app_image(purpose:'splash') avant d'appeler create_project." },
          },
          required: ['mode'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'cleanup_mismatched_files',
        description: "Répare une session déjà polluée par des fichiers d'un AUTRE framework (ex: pubspec.yaml/*.dart écrits par erreur dans une session scratch/cordova/reactnative, ou config.xml dans une session flutter). Supprime UNIQUEMENT ces fichiers marqueurs incohérents — jamais les fichiers légitimes du vrai type de la session. À appeler dès que get_project_overview ou une erreur d'écriture révèle un mélange de frameworks dans la session active, AVANT de continuer à écrire quoi que ce soit dedans. Si la session entière devait en réalité être d'un autre type, n'utilise PAS cet outil : appelle plutôt create_project avec le bon mode pour repartir sur une session saine, et abandonne l'ancienne.",
        parameters: {
          type: 'object',
          properties: {
            session: { type: 'string', description: "Session à nettoyer (défaut : session active)." },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'build_project',
        description: "Compile un APK et attend le résultat (jusqu'à quelques minutes). Modes : 'scratch' (compile la session active TELLE QU'ÉDITÉE, via recompile — nécessite un projet déjà créé/sélectionné) ; 'native' et 'twa' (génèrent ET compilent en une fois à partir de 'config', one-shot, pas d'édition fichier par fichier possible pour ces deux modes) ; 'cordova'/'flutter'/'reactnative' (si 'session' est fourni, compile le projet existant tel quel avec les modifications faites via write_file ; sinon génère un nouveau projet puis compile) ; 'nativescript'/'maui'/'titanium' (même logique que cordova/flutter/reactnative — wrapper WebView site→app — mais PIPELINES BETA, non éprouvés en conditions réelles : préviens le client que ces 3 modes sont plus expérimentaux que cordova/flutter/reactnative/native/twa, et si le build échoue de façon inhabituelle, ne boucle pas dessus, rapporte le message d'erreur exact). Retourne le statut final, la fin du journal de build, et le nom du fichier APK produit s'il y a réussite.",
        parameters: {
          type: 'object',
          properties: {
            mode: { type: 'string', enum: ['scratch', 'native', 'twa', 'cordova', 'flutter', 'reactnative', 'nativescript', 'maui', 'titanium'] },
            session: { type: 'string', description: "Session à compiler en place (cordova/flutter/reactnative/scratch)" },
            config: { type: 'object', description: "Requis pour native/twa/cordova/flutter/reactnative sans session existante. " + CONFIG_FIELDS_DOC },
            icon: { type: 'string', description: "Icône de l'app en base64 (PNG carré, sans préfixe 'data:image/...;base64,'). OBLIGATOIRE en pratique pour native/twa/cordova/flutter/reactnative dès qu'aucune icône n'existe déjà : si le client n'a fourni aucune image, appelle d'abord generate_app_image(purpose:'icon') et passe le résultat ici — ne laisse jamais l'icône par défaut du framework sans avoir au moins essayé de la remplacer." },
            splash: { type: 'string', description: "Écran de démarrage (splash) en base64 (PNG, sans préfixe data:...). Même logique que 'icon' : si le client n'a rien fourni, génère-en un toi-même via generate_app_image(purpose:'splash') avant de lancer le build." },
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
        name: 'export_package',
        description: "Convertit un fichier DÉJÀ compilé (présent dans la liste des sorties, voir list-output) vers un format d'export alternatif — à utiliser APRÈS build_project, jamais à la place. Deux formats réellement supportés, chacun avec sa vraie limite technique (pas de conversion magique) : " +
          "'xapk' — empaquette un .apk existant (n'importe quel mode : scratch/native/cordova/flutter/reactnative/twa) dans le format .xapk (conteneur ZIP + manifest.json + icône, compatible APKPure/SAI). L'APK à l'intérieur n'est PAS re-signé ni modifié, c'est un simple emballage de distribution. " +
          "'split' — génère de VRAIS split APK (base + splits par ABI/densité/langue) via bundletool, mais UNIQUEMENT à partir d'un vrai fichier .aab source. Un .apk déjà fusionné (le cas normal ici, apktool/gradle assembleRelease) NE PEUT PAS être redécoupé après coup — si le client demande des split APK et que le projet ne produit qu'un .apk, dis-le clairement au lieu d'appeler cet outil, il renverra une erreur explicite plutôt qu'un faux résultat. " +
          "IMPORTANT — ce qui N'EST PAS un format d'export ici et ne doit jamais être présenté comme tel au client : 'AAB' n'est pas généré par cet outil (il faudrait changer la chaîne de build Gradle elle-même, pas juste emballer une sortie existante) ; 'APK système' et 'App instantanée' ne sont pas des formats de fichier différents (system = même APK signé, installé manuellement dans /system/priv-app par le client sur un appareil rooté ; instant app = nécessite une architecture de manifeste/taille totalement différente dès la conception) — n'invente jamais un export pour ces cas, explique la limite.",
        parameters: {
          type: 'object',
          properties: {
            file: { type: 'string', description: "Nom exact du fichier source déjà présent dans les sorties (ex: celui renvoyé par build_project dans 'apkFile'). Doit être un .apk pour format 'xapk', un .aab pour format 'split'." },
            format: { type: 'string', enum: ['xapk', 'split'] },
            signing: { type: 'object', description: "Utilisé seulement pour 'split' (signature des splits générés). Même format que build_project.signing ; { mode: 'debug' } par défaut." },
          },
          required: ['file', 'format'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'propose_identity',
        description: "Lance dans le chat un ASSISTANT EN 3 CARTES SUCCESSIVES (identité → permissions → icône/splash/manifest) avant de créer un nouveau projet ou de renommer un projet existant. Chaque carte a deux boutons : ✅ Continuer (garde la proposition) et ✏️ Changer (le client précise ce qu'il veut) ; il y a aussi un lien '⏭ Ignorer tout le reste' visible dès la 1ère carte qui saute directement à la fin avec les valeurs par défaut sur toutes les étapes restantes. Obligatoire comme TOUT PREMIER outil appelé pour toute demande de création/renommage d'app, SAUF si le pilote automatique est activé (dans ce cas, ne l'appelle pas, décide et continue directement). Après l'avoir appelé, termine ta réponse par un court message et ARRÊTE-TOI : n'appelle PAS create_project/build_project/apply_project_settings dans le même tour — attends la confirmation du client (son clic renverra un message de confirmation dans le chat, ou il te redira lui-même ce qu'il veut changer).",
        parameters: {
          type: 'object',
          properties: {
            appName: { type: 'string', description: "Nom d'app déduit du contenu/objectif de la demande." },
            packageName: { type: 'string', description: "Package déduit, ex: com.calculatrice.app." },
            reason: { type: 'string', description: "Courte justification du choix (ex: 'déduit de : app calculatrice')." },
            permissions: { type: 'array', items: { type: 'string' }, description: "Permissions android.permission.XXX proposées par défaut pour cette app (déduites du besoin), affichées à l'étape 2." },
            assetPlan: { type: 'string', description: "Court résumé de ce que tu comptes faire pour l'icône/splash/manifest à l'étape 3 (ex: 'icône générée par IA, splash assorti, permissions déclarées dans le manifest')." },
          },
          required: ['appName', 'packageName'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'search_vector_icon',
        description: "Filet de secours pour l'icône/splash quand generate_app_image échoue par manque de crédits (erreur mentionnant 'credit'/'insufficient'/HTTP 402) : cherche une icône vectorielle correspondant au projet, D'ABORD dans la bibliothèque locale déjà téléchargée dans assets, PUIS en ligne (Iconify) si rien de pertinent n'est trouvé localement. Retourne le SVG trouvé (jamais du base64 PNG généré) — à écrire directement comme fichier asset (ex: assets/icon.svg), pas dans le champ 'icon' de create_project qui attend un PNG.",
        parameters: {
          type: 'object',
          properties: {
            query: { type: 'string', description: "Mots-clés décrivant le sujet de l'icône (ex: 'calculatrice', 'chat message bulle')." },
          },
          required: ['query'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'download_icon_pack',
        description: "Télécharge à la demande, dans assets/icons/ du projet courant, un petit pack d'icônes vectorielles correspondant aux mots-clés du projet en cours (PAS un pack fixe générique) — et écrit un manifest listant les icônes obtenues (assets/icons/manifest.json) pour que le projet puisse les référencer. Utile pour proposer un choix d'icônes au client à l'étape 3 de propose_identity, ou quand plusieurs icônes liées au thème de l'app sont nécessaires (pas seulement le logo principal).",
        parameters: {
          type: 'object',
          properties: {
            keyword: { type: 'string', description: "Mot-clé du projet en cours (ex: 'calculatrice')." },
            count: { type: 'integer', description: "Nombre d'icônes à récupérer (défaut 8, max 24)." },
          },
          required: ['keyword'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'generate_app_image',
        description: "Génère toi-même une image (logo/icône carrée OU écran de démarrage/splash) via le modèle d'image d'OpenRouter, SANS que le client ait besoin de fournir une image ni de formuler une demande explicite de génération. Utilise cet outil systématiquement dès qu'une icône ou un splash sont nécessaires pour un build et qu'aucune image n'a été fournie par le client dans la conversation. Retourne l'image en base64 brut (sans préfixe data:), directement réutilisable dans le champ 'icon' ou 'splash' de create_project/build_project/apply_project_settings.",
        parameters: {
          type: 'object',
          properties: {
            purpose: { type: 'string', enum: ['icon', 'splash'], description: "'icon' = logo carré simple, fond uni, pas de texte long ; 'splash' = écran de démarrage, peut inclure le logo centré sur fond de marque." },
            prompt: { type: 'string', description: "Description visuelle précise déduite du nom/objectif de l'app (style, couleurs, symbole). Toujours la déduire toi-même du contexte de la conversation, ne jamais demander au client de la rédiger." },
          },
          required: ['purpose', 'prompt'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'apply_project_settings',
        description: "Applique nom de l'app / icône / splash / autres champs de config SUR LA SESSION ACTIVE DÉJÀ CRÉÉE (utile en mode 'scratch', ou pour corriger un projet existant sans repartir de zéro), sans passer par build_project. À utiliser dès que le client (ou toi-même, par déduction du contenu) veut changer le nom de l'app, poser un logo ou un splash sur un projet qui existe déjà — puis termine par build_project(mode:'scratch') pour recompiler et voir l'effet dans l'APK.",
        parameters: {
          type: 'object',
          properties: {
            config: { type: 'object', description: "Champs à mettre à jour, ex: { appName: 'Allô Livreur' }. " + CONFIG_FIELDS_DOC },
            icon: { type: 'string', description: "Icône en base64 brut (sans préfixe data:), optionnel." },
            splash: { type: 'string', description: "Splash en base64 brut (sans préfixe data:), optionnel." },
          },
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
    {
      type: 'function',
      function: {
        name: 'search_missing_component',
        description: "À utiliser UNIQUEMENT quand check_missing_components/check_environment révèle qu'un outil requis n'existe PAS dans le registre connu (donc install_components ne peut pas le gérer). Cherche sur GitHub un exécutable/archive officiel correspondant, et l'AJOUTE à la liste affichée dans le modal Composants avec un badge « IA » et sa source — mais NE L'INSTALLE JAMAIS toi-même : le client doit voir la source, cocher la case et cliquer Installer lui-même. N'invente jamais de lien : si aucun candidat fiable n'est trouvé, dis-le au client au lieu de proposer un lien inventé.",
        parameters: {
          type: 'object',
          properties: {
            toolName: { type: 'string', description: "Nom de l'outil manquant à chercher (ex: 'ninja build', 'aapt2 standalone', 'kotlinc')." },
            reason: { type: 'string', description: "Pourquoi cet outil est nécessaire (contexte affiché au client)." },
          },
          required: ['toolName'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'search_public_library',
        description: "Cherche une VRAIE dépendance/librairie sur un registre public officiel — à préférer à web_search dès que le besoin est une lib Android/Cordova/Flutter précise, car ça renvoie un nom et une version exacts et à jour (pas une supposition). Sources : 'maven' (Maven Central / Google Maven — libs Android/Gradle natives, ex: retrofit, okhttp, androidx.*, com.google.android.material) ; 'npm' (registre npm — plugins Cordova du type cordova-plugin-*, et paquets React Native) ; 'pub' (pub.dev — packages Flutter/Dart, ex: http, shared_preferences, sqflite) ; 'fdroid' (F-Droid — catalogue d'apps Android open source, à utiliser SEULEMENT comme référence d'implémentation/fonctionnalité, jamais pour copier du code tel quel sans vérifier sa licence, souvent copyleft GPL/AGPL incompatible avec une app cliente propriétaire). Chaque résultat inclut de quoi écrire la ligne exacte à insérer (gradleLine pour maven, pubspecLine pour pub, id+version pour npm/fdroid) — utilise ensuite write_file/replace_line toi-même pour l'ajouter dans build.gradle / pubspec.yaml / config.xml selon le type de projet, ne l'ajoute jamais si le type de projet ne correspond pas à la source (ex: jamais de dépendance npm dans un projet scratch/apktool qui n'a aucun gestionnaire de paquets).",
        parameters: {
          type: 'object',
          properties: {
            source: { type: 'string', enum: ['maven', 'npm', 'pub', 'fdroid'], description: "Registre à interroger." },
            query: { type: 'string', description: "Termes de recherche (nom de lib, mot-clé de fonctionnalité, ex: 'http client', 'camera plugin', 'qr code scanner')." },
            limit: { type: 'integer', description: 'Nombre max de résultats (défaut 8, max 20).' },
          },
          required: ['source', 'query'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'repair_system',
        description: "Diagnostique ET corrige automatiquement les pannes SYSTÈME connues (JDK/keytool manquant ou corrompu, apktool cassé, Android SDK/build-tools incomplet, cache Gradle corrompu, debug.keystore corrompu) à partir du journal d'erreur d'un build_project/décompilation/signature qui vient d'échouer. À appeler IMMÉDIATEMENT après tout échec de build_project, decompile_apk, ou toute opération de signature, EN PASSANT le message d'erreur complet reçu — avant de redemander quoi que ce soit au client et avant de conclure que c'est un problème de code. Si la réparation corrige au moins une panne (canRetry:true dans le résultat), relance immédiatement l'opération qui avait échoué (même appel, mêmes arguments) SANS redemander confirmation au client — dis-lui juste ce qui a été corrigé. Si rien n'est auto-réparable (ex: mauvais mot de passe de keystore de production, fichier fourni par le client corrompu, panne inconnue), le résultat l'indique clairement ET contient un champ `externalAiReport` (texte déjà rédigé, contexte projet + environnement + diagnostic + journal inclus) : dans ce cas, explique au client ce qu'il doit faire lui-même si l'action est connue (ex: redonner le bon mot de passe), et SYSTÉMATIQUEMENT propose-lui aussi `externalAiReport` tel quel dans un bloc de code, en lui disant qu'il peut le coller dans une autre IA (ChatGPT, Claude, etc.) pour obtenir un second avis ou un script correctif — ne le résume jamais, ne le réécris jamais, colle-le intégralement. Ne tente PAS de deviner ou d'inventer une action risquée à la place du client.",
        parameters: {
          type: 'object',
          properties: {
            logText: { type: 'string', description: "Texte brut complet du journal d'erreur renvoyé par l'opération qui a échoué (build_project, decompile_apk, création/utilisation de keystore...)." },
          },
          required: ['logText'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'web_search',
        description: "Recherche en ligne (moteur DuckDuckGo, pas de clé API requise) pour toute information que tu ne connais pas avec certitude ou qui peut avoir changé après ta date de connaissance : nom exact et dernière version d'un plugin/package (Cordova, npm, pub.dev Flutter, Gradle...), message d'erreur de build rare ou spécifique à copier-coller tel quel, syntaxe d'une API Android récente, documentation d'un service tiers (FedaPay, FeeXPay, Supabase, OSRM, Leaflet...), actualité ou info dépendant du moment présent. Retourne une liste de résultats {title, url, snippet}. UTILISE CET OUTIL PLUTÔT QUE DE DEVINER dès qu'une tâche est complexe, inhabituelle, ou touche à une techno/version que tu ne maîtrises pas avec certitude — mieux vaut une recherche de plus qu'une réponse fausse ou obsolète.",
        parameters: {
          type: 'object',
          properties: {
            query: { type: 'string', description: 'Termes de recherche courts et précis (2-6 mots), en français ou dans la langue la plus pertinente pour le sujet (ex: termes techniques anglais pour une erreur Gradle).' },
          },
          required: ['query'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'fetch_url',
        description: "Récupère et extrait le contenu texte d'une page web précise (résultat renvoyé par web_search, documentation officielle, page GitHub, changelog...). Utilise-la après web_search pour lire le détail d'une page prometteuse plutôt que de te fier au seul extrait (snippet), notamment pour une doc d'API, un changelog de version, ou une page d'erreur GitHub Issues.",
        parameters: {
          type: 'object',
          properties: {
            url: { type: 'string', description: 'URL complète (https://...) à récupérer, normalement issue d’un résultat de web_search.' },
          },
          required: ['url'],
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

    async search_public_library(args) {
      const source = String(args?.source || '').trim();
      const query = String(args?.query || '').trim();
      if (!['maven', 'npm', 'pub', 'fdroid'].includes(source)) {
        throw new Error("Paramètre 'source' invalide — utilise 'maven', 'npm', 'pub' ou 'fdroid'.");
      }
      if (!query) throw new Error("Paramètre 'query' manquant ou vide.");
      if (!(window.electronAPI && typeof window.electronAPI.searchPublicLibrary === 'function')) {
        throw new Error("Recherche indisponible : cette version de l'app n'expose pas encore le pont searchPublicLibrary.");
      }
      const limit = Math.min(Math.max(parseInt(args?.limit || 8, 10) || 8, 1), 20);
      const res = await window.electronAPI.searchPublicLibrary(source, query, limit);
      if (!res || !res.ok) throw new Error((res && res.error) || `Recherche '${source}' échouée.`);
      return { source, query, results: res.results || [] };
    },

    async repair_system(args) {
      const logText = String(args?.logText || '').trim();
      if (!logText) throw new Error("Paramètre 'logText' manquant ou vide — passe le message d'erreur complet de l'opération qui a échoué.");
      if (!(window.electronAPI && typeof window.electronAPI.iaRepairRun === 'function')) {
        throw new Error("Réparation automatique indisponible : cette version de l'app n'expose pas encore le pont iaRepairRun.");
      }
      const res = await window.electronAPI.iaRepairRun(logText, {});
      if (!res || !res.ok) throw new Error((res && res.error) || 'Réparation automatique échouée.');
      return {
        matched: res.matched || [],
        fixedCount: res.fixedCount || 0,
        actions: res.actions || [],
        canRetry: !!res.canRetry,
        summary: res.summary || '',
        // Présent uniquement s'il reste au moins une panne non résolue —
        // texte prêt-à-coller (contexte projet + environnement + diagnostic
        // + journal) pour qu'un client puisse demander un second avis à une
        // autre IA. Voir iareparateur.js → buildExternalAiPrompt().
        externalAiReport: res.externalAiReport || null,
      };
    },

    async web_search(args) {
      const q = String(args?.query || '').trim();
      if (!q) throw new Error("Paramètre 'query' manquant ou vide.");
      // DuckDuckGo HTML (pas d'API key nécessaire) — on parse la page de
      // résultats directement, en se limitant volontairement à un extrait
      // texte propre par résultat (pas de HTML brut renvoyé au modèle).
      const resp = await fetch('https://html.duckduckgo.com/html/?q=' + encodeURIComponent(q), {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
      });
      if (!resp.ok) throw new Error(`Recherche web échouée (HTTP ${resp.status}).`);
      const html = await resp.text();
      const results = [];
      const blockRe = /<div class="result results_links[^"]*">([\s\S]*?)<\/div>\s*<\/div>\s*<\/div>/g;
      const linkRe = /<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/;
      const snippetRe = /<a[^>]*class="result__snippet"[^>]*>([\s\S]*?)<\/a>/;
      const stripTags = (s) => s.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim();
      let m;
      while ((m = blockRe.exec(html)) && results.length < 8) {
        const block = m[1];
        const lm = linkRe.exec(block);
        const sm = snippetRe.exec(block);
        if (!lm) continue;
        let url = lm[1];
        // DuckDuckGo renvoie souvent une redirection //duckduckgo.com/l/?uddg=<url encodée>
        const uddg = /[?&]uddg=([^&]+)/.exec(url);
        if (uddg) { try { url = decodeURIComponent(uddg[1]); } catch (e) {} }
        results.push({
          title: stripTags(lm[2]),
          url,
          snippet: sm ? stripTags(sm[1]) : '',
        });
      }
      if (!results.length) {
        return { ok: true, query: q, results: [], note: "Aucun résultat structuré détecté — reformule la requête avec des mots-clés plus spécifiques." };
      }
      return { ok: true, query: q, results };
    },

    async fetch_url(args) {
      const url = String(args?.url || '').trim();
      if (!/^https?:\/\//i.test(url)) throw new Error("URL invalide : doit commencer par http:// ou https://.");
      const resp = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' } });
      if (!resp.ok) throw new Error(`Récupération de la page échouée (HTTP ${resp.status}).`);
      let text = await resp.text();
      // Extraction texte basique : retire scripts/styles/balises, condense les
      // espaces, tronque pour éviter de saturer le contexte du modèle.
      text = text.replace(/<script[\s\S]*?<\/script>/gi, ' ')
                 .replace(/<style[\s\S]*?<\/style>/gi, ' ')
                 .replace(/<[^>]+>/g, ' ')
                 .replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&nbsp;/g, ' ')
                 .replace(/\s+/g, ' ').trim();
      const MAX = 6000;
      const truncated = text.length > MAX;
      return { ok: true, url, contentPreview: text.slice(0, MAX), truncated };
    },

    async get_project_overview(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error("Aucune session active et aucun 'session' fourni.");
      return fetchJSON('/agent-overview?session=' + encodeURIComponent(sid));
    },

    async cleanup_mismatched_files(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error("Aucune session active — utilise create_project ou select_session d'abord.");
      return fetchJSON('/cleanup-mismatched-files', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid }),
      });
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
      // CORRECTIF : jusqu'ici select_session ne faisait QUE changer la
      // session active côté serveur — aucune synchronisation de l'UI
      // (onglet de gauche, arborescence, panneau IA) ni du garde-fou de
      // type/chemin, contrairement à create_project. Résultat : quand l'IA
      // reprenait un projet déjà existant (pas fraîchement créé dans le même
      // tour), l'écran restait sur l'ancien onglet (souvent Scratch) — même
      // symptôme que le bug déjà corrigé côté clic manuel, mais côté IA.
      let mode = getSessionMode(args.session);
      if (!mode) {
        try {
          const list = await fetchJSON('/sessions');
          const found = (list.sessions || []).find((s) => s.session === args.session);
          if (found && found.origin) mode = found.origin;
        } catch (e) { /* pas bloquant si /sessions échoue */ }
      }
      if (mode) {
        sessionApkType.set(args.session, mode);
        if (typeof window.rememberSessionOrigin === 'function') window.rememberSessionOrigin(args.session, mode);
        syncProjectUiAfterCreate(args.session, mode, null);
      }
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
      const pathGuard = enforceApkPathMatch(sid, args.path);
      if (pathGuard) return pathGuard;

      // CORRECTIF : le garde-fou ci-dessus (enforceApkPathMatch) devine la
      // racine attendue à partir d'une table statique PAR MODE — trop
      // grossier pour les cas où plusieurs sous-structures sont valides
      // pour le MÊME mode (scratch : HTML local dans assets/, site zippé
      // dans assets/www/, ou aucun fichier en mode URL distante ; flutter :
      // WebView dans assets/www/ MAIS un vrai projet Flutter natif n'a
      // AUCUN webroot). Résultat observé : l'IA écrit parfois le bon nom de
      // fichier (index.html/style.css/app.js/script.js) dans le MAUVAIS
      // sous-dossier pour CE projet précis — passe le garde-fou statique
      // (le chemin "ressemble" au bon mode) mais atterrit hors du dossier
      // réellement chargé par la WebView, laissant celle-ci vide/orpheline.
      // On consulte donc ici GET /entrypoint — la même fonction qui fait
      // autorité au moment du build — pour connaître le VRAI chemin actif
      // de CE projet précis, et bloquer avant écriture si ça ne correspond
      // pas, avec le chemin exact à utiliser à la place.
      const cleanPath = String(args.path).replace(/^\/+/, '');
      const baseName = cleanPath.split('/').pop();
      if (['index.html', 'style.css', 'app.js', 'script.js'].includes(baseName)) {
        try {
          const ep = await fetchJSON('/entrypoint?session=' + encodeURIComponent(sid));
          if (ep && ep.activeIndexPath) {
            const activeDir = ep.activeIndexPath.split('/').slice(0, -1).join('/'); // '' pour racine
            const writeDir = cleanPath.split('/').slice(0, -1).join('/');
            if (activeDir !== writeDir) {
              return {
                blocked: true,
                reason:
                  `Chemin refusé : '${cleanPath}' n'est pas le dossier racine web réellement actif pour CE projet. ` +
                  `Le seul chemin que la WebView charge ici est '${ep.activeIndexPath}' — écris ${baseName} à cet emplacement exact ` +
                  `(même nom de fichier, dossier '${activeDir || '(racine)'}'), sinon ton contenu restera invisible et orphelin.`,
              };
            }
          } else if (ep && ep.activeIndexPath === null) {
            // Aucun webroot actif pour ce projet (ex: Flutter/natif sans
            // WebView, ou mode URL distante) : écrire un de ces 4 noms ici
            // ne servirait jamais à rien, donc mieux vaut prévenir tout de
            // suite plutôt que laisser l'IA croire que ça sera chargé.
            return {
              blocked: true,
              reason:
                `Chemin refusé : ce projet n'a AUCUN dossier racine web actif (app Flutter/native sans WebView, ou site en mode URL distante) — ` +
                `'${cleanPath}' ne sera jamais chargé par l'app. Si tu codes une vraie fonctionnalité, écris-la dans les vrais fichiers natifs du projet ` +
                `(lib/*.dart, .kt, .java, .smali selon le pipeline) et pas dans un fichier HTML.`,
            };
          }
        } catch (e) {
          // /entrypoint indisponible pour une raison annexe (session pas
          // encore matérialisée) : on ne bloque pas sur ce contrôle
          // facultatif, write_file reste possible comme avant.
        }
      }

      const r = await fetchJSON('/save-file', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid, path: args.path, content: args.content ?? '' }),
      });
      agentLog(`✅ [IA] Fichier écrit : ${args.path}`);
      if (typeof window.aiWorkspaceAddFile === 'function') window.aiWorkspaceAddFile(args.path);
      if (typeof window.aiWorkspaceLog === 'function') window.aiWorkspaceLog(`Fichier écrit : ${args.path}`, 'ok');
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
        nativescript: '/nativescript-generate',
        maui: '/maui-generate',
        titanium: '/titanium-generate',
      };
      const tokenByMode = { scratch: 'legacy', cordova: 'cordova_gen', flutter: 'flutter_gen', reactnative: 'rn_gen', nativescript: 'nativescript_gen', maui: 'maui_gen', titanium: 'titanium_gen' };
      const endpoint = endpointByMode[mode];
      if (!endpoint) {
        if (mode === 'twa') {
          throw new Error(
            "create_project ne gère pas le mode 'twa' — et c'est normal : TWA n'a pas d'espace de travail fichiers. " +
            "Un TWA n'embarque aucun HTML/CSS/JS local, il pointe juste vers un site distant déjà en ligne via Digital " +
            "Asset Links (assetlinks.json) — il n'y a donc rien à insérer dans un dossier. Appelle build_project(mode:'twa', config:{...}) " +
            "directement avec l'URL du site ; dis clairement au client que TWA est une simple config (identité + URL + vérification " +
            "du domaine), pas un projet de fichiers à éditer."
          );
        }
        throw new Error(`create_project ne gère pas le mode '${mode}'. Utilise build_project directement pour 'native' ou 'twa'.`);
      }

      // Rempart côté code : bloque la création tant que l'identité (nom +
      // package) n'a pas été confirmée pour cette app, même si le modèle a
      // sauté l'étape propose_identity.
      const guard = enforceIdentityConfirmation(args.config || {});
      if (guard) return guard;
      args.config = { ...(args.config || {}), appName: (args.config || {}).appName || inferAppIdentityFromText(CURRENT_USER_TEXT).appName, packageName: (args.config || {}).packageName || inferAppIdentityFromText(CURRENT_USER_TEXT).packageName };

      const okStep = await agentConfirmStep('Créer le projet maintenant ?', `Mode : ${mode}`);
      if (!okStep) return { cancelled: true, reason: 'Refusé par le client.' };

      const typeGuard = enforceApkTypeMatch(mode, null);
      if (typeGuard) return typeGuard;

      const compGuard = await enforceComponentsForMode(mode);
      if (compGuard) return compGuard;

      agentLog(`🚀 [IA] Création du projet (mode ${mode})…`);
      if (typeof window.aiWorkspaceReset === 'function') window.aiWorkspaceReset(mode);
      if (typeof window.aiWorkspaceShow === 'function') window.aiWorkspaceShow(mode);
      if (typeof window.aiWorkspaceLog === 'function') window.aiWorkspaceLog(`Création du projet — copie du squelette templates/${mode}/webroot/…`, 'info');
      const started = await fetchJSON(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: args.config || {}, icon: args.icon || undefined, splash: args.splash || undefined }),
      });
      if (!started.started) throw new Error("Le serveur n'a pas confirmé le démarrage de la génération.");

      const result = await pollStatus(tokenByMode[mode], { timeoutMs: 120000 });
      if (result.status !== 'done') {
        agentLog(`❌ [IA] Échec de la création du projet (${result.status})`);
        throw new Error(`Échec de la génération du projet (${result.status}) :\n${result.logsTail}`);
      }
      if (result.session) {
        sessionApkType.set(result.session, mode);
        window._currentSessionId = result.session;
        syncProjectUiAfterCreate(result.session, mode, args.config || {});
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
        body = { config: { ...(args.config || {}), signing }, icon: args.icon || undefined, splash: args.splash || undefined };
      } else if (mode === 'twa') {
        endpoint = '/build-twa'; token = 'twa';
        body = { config: { ...(args.config || {}), signing }, icon: args.icon || undefined, splash: args.splash || undefined };
      } else if (mode === 'cordova' || mode === 'flutter' || mode === 'reactnative' || mode === 'nativescript' || mode === 'maui' || mode === 'titanium') {
        const endpointMap = { cordova: '/build-cordova', flutter: '/build-flutter', reactnative: '/build-rn', nativescript: '/build-nativescript', maui: '/build-maui', titanium: '/build-titanium' };
        const tokenMap = { cordova: 'cordova', flutter: 'flutter', reactnative: 'rn', nativescript: 'nativescript', maui: 'maui', titanium: 'titanium' };
        endpoint = endpointMap[mode]; token = tokenMap[mode];
        body = { config: { ...(args.config || {}), session: sid || undefined, signing }, icon: args.icon || undefined, splash: args.splash || undefined };
      } else {
        throw new Error(`Mode de build inconnu : '${mode}'.`);
      }

      // Rempart côté code appliqué à TOUS les types d'APK (scratch, natif,
      // twa, cordova, flutter, react native — session existante ou pas) :
      // tant que le nom/package de cette app n'ont pas été confirmés, on
      // bloque et on affiche la carte de confirmation dans le chat.
      const guard = enforceIdentityConfirmation(args.config || {}, sid);
      if (guard) return guard;
      {
        const inferred = inferAppIdentityFromText(CURRENT_USER_TEXT);
        const appName = (args.config || {}).appName || inferred.appName;
        const packageName = (args.config || {}).packageName || inferred.packageName;
        if (args.config) { args.config.appName = args.config.appName || appName; args.config.packageName = args.config.packageName || packageName; }
        if (body && body.config) body.config = { ...body.config, appName: body.config.appName || appName, packageName: body.config.packageName || packageName };
      }

      let started;
      const okStep = await agentConfirmStep('Lancer la compilation (build) maintenant ?', `Mode : ${mode}`);
      if (!okStep) return { cancelled: true, reason: 'Refusé par le client.' };

      const typeGuard = enforceApkTypeMatch(mode, sid);
      if (typeGuard) return typeGuard;

      const compGuard = await enforceComponentsForMode(mode);
      if (compGuard) return compGuard;

      // CORRECTIF PERFORMANCE IA : avant, un index.html vide/jamais rempli
      // n'était détecté qu'APRÈS un build_project complet (plusieurs
      // minutes de compilation Gradle/apktool), via une erreur remontée
      // tout en bas des logs ("Compilation annulée : ... est vide") —
      // souvent noyée dans une trace Python complète, donc ratée par
      // l'agent qui relançait parfois le même build en boucle. On
      // interroge maintenant GET /entrypoint (même fonction faisant
      // autorité que celle utilisée juste avant la compilation côté
      // serveur) AVANT de démarrer le build : si le contenu réel manque,
      // on échoue en une fraction de seconde avec un message qui dit
      // explicitement quoi écrire, au lieu de gaspiller un cycle de build
      // entier pour arriver au même constat.
      if (sid) {
        try {
          const ep = await fetchJSON('/entrypoint?session=' + encodeURIComponent(sid));
          if (ep && ep.contentMissing) {
            const path_hint = ep.activeIndexPath || "le fichier d'entrée du projet";
            throw new Error(
              `Build non lancé : ${path_hint} est vide ou n'a jamais été rempli avec le vrai contenu ` +
              `de l'app. Appelle write_file sur ${path_hint} (contenu HTML réel, pas un squelette) ` +
              `PUIS relance build_project — ne relance jamais build_project tel quel sans avoir écrit ce fichier d'abord.`
            );
          }
        } catch (e) {
          // Si /entrypoint échoue pour une raison annexe (session pas encore
          // matérialisée côté serveur, etc.), on ne bloque pas le build sur
          // ce contrôle facultatif — sauf si c'est bien NOTRE erreur ci-dessus.
          if (e && /Build non lancé/.test(e.message || '')) throw e;
        }
      }

      if (signing.mode === 'release') {
        if (!window.electronAPI || typeof window.electronAPI.buildWithReleaseSigning !== 'function') {
          throw new Error("Signature 'release' indisponible : cette version de l'app n'expose pas encore le pont sécurisé de signature (électronAPI.buildWithReleaseSigning manquant).");
        }
        // Une seule question posée dans le chat, UNE FOIS par app (package),
        // pour savoir si on réutilise une clé de signature déjà configurée
        // ou si on en crée une dédiée à cette app — ensuite c'est mémorisé
        // et silencieux pour tous les builds/modifs suivants du même projet.
        const packageName = (args.config && args.config.packageName) || sid || 'default';
        const keystoreKey = await resolveSigningKey(packageName);
        // Ne transite JAMAIS par un fetch() du renderer : le process principal
        // (main.js) injecte lui-même les identifiants déchiffrés et fait la
        // requête HTTP, ouvrant au besoin la fenêtre de configuration unique.
        started = await window.electronAPI.buildWithReleaseSigning(endpoint, body, keystoreKey);
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
      if (typeof window.aiWorkspaceShow === 'function') window.aiWorkspaceShow(mode, sid || undefined);
      if (typeof window.aiWorkspaceLog === 'function') window.aiWorkspaceLog(`Compilation lancée (mode ${mode})…`, 'info', mode);
      renderAgentStep('🛠', `Build ${mode} lancé`, 'en cours de compilation…', true);
      const result = await pollStatus(token, { timeoutMs: 6 * 60 * 1000 });

      if (result.session) { window._currentSessionId = result.session; sessionApkType.set(result.session, mode); syncProjectUiAfterCreate(result.session, mode, args.config || {}); }
      if (typeof window.refreshOutputList === 'function') window.refreshOutputList();
      agentLog(result.status === 'done' ? `✅ [IA] Build terminé : ${result.file || '(voir logs)'}` : `❌ [IA] Build en échec (${result.status})`);
      if (typeof window.aiWorkspaceLog === 'function') {
        window.aiWorkspaceLog(
          result.status === 'done' ? `Build terminé : ${result.file || '(voir logs)'}` : `Build en échec (${result.status})`,
          result.status === 'done' ? 'ok' : 'err'
        );
      }

      return {
        status: result.status,
        session: result.session,
        apkFile: result.file,
        logsTail: truncate(result.logsTail, 3000),
      };
    },

    async export_package(args) {
      const file = args.file;
      const format = args.format;
      if (!file || !['xapk', 'split'].includes(format)) {
        throw new Error("export_package: 'file' et 'format' ('xapk'|'split') sont requis.");
      }
      agentLog(`📦 [IA] Export ${format.toUpperCase()} de ${file}…`);
      renderAgentStep('📦', `Export ${format.toUpperCase()}`, file, true);
      const result = await fetchJSON('/export-package', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file, format, signing: args.signing || { mode: 'debug' } }),
      });
      if (typeof window.refreshOutputList === 'function') window.refreshOutputList();
      agentLog(result.file ? `✅ [IA] Export terminé : ${result.file}` : '❌ [IA] Export en échec');
      return {
        status: result.file ? 'done' : 'error',
        outputFile: result.file,
        logsTail: truncate(result.logsTail, 2000),
      };
    },

    async propose_identity(args) {
      if (!args.appName || !args.packageName) throw new Error("appName et packageName sont requis.");
      renderIdentityWizard({ appName: args.appName, packageName: args.packageName, reason: args.reason, permissions: args.permissions || [], assetPlan: args.assetPlan });
      return { shown: true, note: "Assistant 3 étapes affiché dans le chat (identité → permissions → asset/manifest) — arrête-toi ici, n'appelle pas encore create_project/build_project, attends la réponse du client." };
    },

    // Génère un logo (icône) ou un splash tout seul, déclenchable par
    // l'agent lui-même sans mot magique tapé par le client. Utilise PAR
    // DÉFAUT le générateur d'images gratuit sans clé (même moteur que le
    // chat classique, voir aiGenerateFreeImages dans builder.html) — donc
    // ça marche même sans aucun compte OpenRouter configuré. Ne repasse
    // sur OpenRouter que si le client a explicitement choisi ce mode dans
    // Paramètres. Renvoie du base64 brut prêt pour icon/splash.
    async generate_app_image(args) {
      const purpose = args.purpose === 'splash' ? 'splash' : 'icon';
      const styleHint = purpose === 'icon'
        ? "Icône d'application Android carrée, design plat et moderne, fond uni ou dégradé simple, symbole central unique, SANS texte écrit, bords nets, look professionnel."
        : "Écran de démarrage (splash screen) d'application mobile, format portrait, fond de marque cohérent avec le logo, composition centrée sobre, SANS texte écrit.";
      const prompt = `${styleHint} Sujet : ${args.prompt}`;
      agentLog(`🎨 [IA] Génération d'un ${purpose === 'icon' ? 'logo' : 'splash screen'}…`);

      const useFree = (typeof window.aiGetImageSource !== 'function') || window.aiGetImageSource() === 'free';
      if (useFree && typeof window.aiGenerateFreeImages === 'function') {
        try {
          const images = await window.aiGenerateFreeImages(prompt, 1);
          const dataUrl = images[0]?.image_url?.url;
          if (!dataUrl) throw new Error('Aucune image reçue du générateur gratuit.');
          const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
          agentLog(`✅ [IA] ${purpose === 'icon' ? 'Logo' : 'Splash'} généré (générateur gratuit).`);
          return { purpose, base64: b64, previewDataUrl: dataUrl };
        } catch (e) {
          agentLog(`⚠️ [IA] Générateur gratuit indisponible (${e.message}) — bascule sur OpenRouter si une clé est configurée…`);
          // on continue plus bas sur le chemin OpenRouter classique
        }
      }

      const key = localStorage.getItem(window.AI_LS_KEY || 'apkfactory_ai_apikey');
      if (!key) throw new Error("Générateur gratuit indisponible et aucune clé API OpenRouter configurée — impossible de générer une image.");
      const imgModel = localStorage.getItem(window.AI_LS_IMG || 'apkfactory_ai_image_model')
        || document.getElementById('ai-image-model-select')?.value;
      if (!imgModel) throw new Error("Aucun modèle d'image configuré (onglet Réglages IA).");

      let resp, data;
      try {
        resp = await fetch(window.OPENROUTER_URL || 'https://openrouter.ai/api/v1/chat/completions', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + key,
            'HTTP-Referer': 'https://apkfactory.local',
            'X-Title': 'APK Factory Pro — Agent',
          },
          body: JSON.stringify({
            model: imgModel,
            messages: [{ role: 'user', content: prompt }],
            modalities: ['image', 'text'],
            max_tokens: 4000,
          }),
        });
        data = await resp.json();
      } catch (e) {
        throw new Error(`Échec réseau lors de la génération d'image : ${e.message}`);
      }

      if (!resp.ok || data?.error) {
        const msg = data?.error?.message || `HTTP ${resp.status}`;
        // Crédits insuffisants (402, ou message évoquant explicitement le
        // crédit) : bascule d'abord sur le compte OpenRouter suivant s'il
        // y en a un (comme le mode chat classique), sinon sur le filet de
        // secours vectoriel plutôt que de laisser l'agent sans icône/splash.
        const creditIssue = resp.status === 401 || resp.status === 402 || /credit|insufficient|quota/i.test(msg);
        if (creditIssue) {
          const rotated = typeof window.aiAutoRotateKey === 'function' ? window.aiAutoRotateKey() : null;
          if (rotated) {
            agentLog(`🔄 [IA] Compte OpenRouter à court de crédit — bascule automatique et nouvelle tentative…`);
            return EXECUTORS.generate_app_image(args);
          }
          agentLog(`⚠️ [IA] Crédits d'image insuffisants (${msg}) — bascule sur la recherche d'icône vectorielle…`);
          const fallback = await EXECUTORS.search_vector_icon({ query: args.prompt });
          return { purpose, ...fallback, note: "Crédits d'image insuffisants : icône vectorielle de secours renvoyée à la place (SVG, pas un PNG généré). Écris-la comme fichier asset (ex: assets/icon.svg), ne la passe pas dans le champ 'icon' base64." };
        }
        throw new Error(msg);
      }
      const images = data.choices?.[0]?.message?.images || [];
      if (!images.length) throw new Error("Le modèle n'a renvoyé aucune image.");
      const dataUrl = images[0].image_url?.url || images[0].url;
      if (!dataUrl) throw new Error("Image générée mais URL introuvable dans la réponse.");
      const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
      agentLog(`✅ [IA] ${purpose === 'icon' ? 'Logo' : 'Splash'} généré.`);
      return { purpose, base64: b64, previewDataUrl: dataUrl };
    },

    // Filet de secours vectoriel : bibliothèque locale (assets déjà
    // téléchargés pour CE projet) en priorité, puis Iconify (API publique,
    // sans clé) si rien de pertinent n'est trouvé localement. Ne renvoie
    // jamais de PNG base64 — uniquement du SVG, à écrire comme fichier.
    async search_vector_icon(args) {
      const query = (args.query || '').trim();
      if (!query) throw new Error("query manquant pour search_vector_icon.");

      // 1) Bibliothèque locale déjà téléchargée pour ce projet (si le
      //    serveur expose cet endpoint — voir download_icon_pack).
      const sid = window._currentSessionId;
      if (sid) {
        try {
          const local = await fetchJSON('/icon-library/search?session=' + encodeURIComponent(sid) + '&q=' + encodeURIComponent(query));
          if (local && local.svg) {
            agentLog(`📦 [IA] Icône trouvée dans la bibliothèque locale pour "${query}".`);
            return { source: 'local', name: local.name || query, svg: local.svg };
          }
        } catch (e) { /* pas de bibliothèque locale ou rien trouvé — on continue en ligne */ }
      }

      // 2) En ligne, via Iconify (icon-sets.iconify.design) : API publique,
      //    sans authentification, qui indexe des dizaines de bibliothèques
      //    d'icônes libres (Material, Tabler, Feather, etc.).
      try {
        const searchResp = await fetch('https://api.iconify.design/search?query=' + encodeURIComponent(query) + '&limit=1');
        const searchData = await searchResp.json();
        const iconId = searchData?.icons?.[0]; // format "prefix:name"
        if (!iconId) throw new Error(`Aucune icône vectorielle trouvée en ligne pour "${query}".`);
        const [prefix, name] = iconId.split(':');
        const svgResp = await fetch(`https://api.iconify.design/${prefix}/${name}.svg`);
        if (!svgResp.ok) throw new Error(`Échec de récupération du SVG (HTTP ${svgResp.status}).`);
        const svg = await svgResp.text();
        agentLog(`🌐 [IA] Icône vectorielle trouvée en ligne (${iconId}) pour "${query}".`);
        return { source: 'online', name: iconId, svg };
      } catch (e) {
        throw new Error(`Aucune icône trouvée (local et en ligne) pour "${query}" : ${e.message}`);
      }
    },

    // Télécharge à la demande, pour LE PROJET COURANT uniquement (pas un
    // pack générique fixe), un petit lot d'icônes correspondant au
    // mot-clé, via le serveur local (qui doit exposer /icon-library/download
    // — voir note ci-dessous si l'endpoint n'existe pas encore côté serveur
    // Python). Écrit aussi un manifest listant les icônes obtenues.
    async download_icon_pack(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error("Aucune session active — crée le projet avant de télécharger un pack d'icônes.");
      const keyword = (args.keyword || '').trim();
      if (!keyword) throw new Error("keyword manquant pour download_icon_pack.");
      const count = Math.max(1, Math.min(24, parseInt(args.count, 10) || 8));
      agentLog(`📦 [IA] Téléchargement d'un pack d'icônes "${keyword}" (${count}) dans assets/icons/…`);
      try {
        const result = await fetchJSON('/icon-library/download', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session: sid, keyword, count }),
        });
        agentLog(`✅ [IA] ${result.icons?.length || 0} icône(s) téléchargée(s) — manifest écrit dans assets/icons/manifest.json.`);
        return result;
      } catch (e) {
        throw new Error(`download_icon_pack nécessite l'endpoint serveur '/icon-library/download' (session, keyword, count) qui écrit les SVG dans assets/icons/ et un manifest.json listant {name, path} — endpoint absent ou en échec : ${e.message}`);
      }
    },

    // Applique nom d'app / icône / splash sur la session déjà active, via
    // l'endpoint /apply déjà supporté côté serveur (utilisé jusqu'ici
    // uniquement par des boutons manuels de l'UI, jamais par l'agent).
    async apply_project_settings(args) {
      const sid = currentSid(args);
      if (!sid) throw new Error("Aucune session active — crée ou sélectionne un projet d'abord.");
      await fetchJSON('/select-session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sid }),
      });
      const body = { config: args.config || {}, icon: args.icon || undefined, splash: args.splash || undefined };
      agentLog(`🎯 [IA] Application des réglages (nom/icône/splash) sur la session ${sid}…`);
      const started = await fetchJSON('/apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (!started.started) throw new Error("Le serveur n'a pas confirmé le démarrage de l'application des réglages.");
      const result = await pollStatus('legacy', { timeoutMs: 60000 });
      if (result.status !== 'done') throw new Error(`Échec de l'application des réglages (${result.status}) :\n${result.logsTail}`);
      if (typeof window.aiSyncIdentityFields === 'function') window.aiSyncIdentityFields(args.config || {});
      agentLog('✅ [IA] Réglages appliqués — un build (build_project) est nécessaire pour les voir dans un APK.');
      return { status: result.status, session: sid, logsTail: truncate(result.logsTail, 1500) };
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

    // Ne fait QUE chercher + afficher un candidat trouvé sur GitHub — ne
    // télécharge ni n'exécute jamais rien. L'installation reste un geste
    // manuel du client (checkbox + bouton "Installer"), avec confirmation
    // explicite affichant la source, exactement comme un composant officiel
    // sauf que la source n'a pas été pré-vérifiée par nous.
    async search_missing_component(args) {
      const toolName = (args.toolName || '').trim();
      if (!toolName) return { error: "toolName manquant." };
      if (!(window.electronAPI && window.electronAPI.setupSearchGithubComponent)) {
        return { available: false, reason: "Recherche de composant indisponible (hors app desktop, ou preload non chargé)." };
      }
      if (typeof toast === 'function') toast(`🔎 Recherche en ligne de « ${toolName} »…`, 'info');
      const candidate = await window.electronAPI.setupSearchGithubComponent(toolName);
      if (!candidate || !candidate.ok || !candidate.url) {
        return { found: false, message: candidate?.error || "Aucun candidat fiable trouvé sur GitHub pour cet outil." };
      }
      const def = {
        id: candidate.id || ('ai_' + toolName.toLowerCase().replace(/[^a-z0-9]+/g, '_')).slice(0, 60),
        label: candidate.label || toolName,
        url: candidate.url,
        sourceUrl: candidate.repoUrl || candidate.url,
        sizeApprox: candidate.sizeApprox || '',
        type: candidate.type || 'file',
      };
      if (typeof window.aiRegisterDynamicComponent === 'function') {
        window.aiRegisterDynamicComponent(def);
      }
      return {
        found: true,
        message: `Candidat trouvé et ajouté à la liste « ⚙ Composants » avec le badge IA : ${def.label} (source : ${def.sourceUrl}). Le client doit ouvrir le modal, vérifier la source, cocher, puis installer lui-même — précise-lui bien ça, ne prétends jamais l'avoir déjà installé.`,
        component: def,
      };
    },
  };

  // ───────────────────────────────────────────────────────────────────
  // 7. Prompt système étendu pour le mode Agent
  // ───────────────────────────────────────────────────────────────────
  // ───────────────────────────────────────────────────────────────────
  // 7bis. "Skills" par type de tâche/espace de travail — une fiche de
  // méthode dédiée par mode (scratch/cordova/flutter/reactnative/native/
  // twa), sur le même principe qu'une fiche de bonnes pratiques : ce
  // n'est PAS juste "un mode = un dossier", c'est une checklist concrète
  // rappelant l'arborescence attendue, les pièges classiques de ce mode
  // précis, et le niveau de complétude minimum attendu avant de considérer
  // la tâche terminée. Injectée dans le prompt système SEULEMENT pour le
  // mode réellement actif/demandé (pas tout d'un coup, pour ne pas noyer
  // le modèle dans du contexte hors-sujet).
  const TASK_SKILLS = {
    scratch: [
      "SKILL — SCRATCH (WebView HTML/CSS/JS local ou distant) — RÉFÉRENCE COMPLÈTE",
      "",
      "1) CHEMIN D'INJECTION RÉEL (vérifié côté serveur, ne pas dévier) :",
      "   • Dossier racine actif UNIQUE : assets/ à la racine du projet (jamais un autre dossier en parallèle).",
      "   • Fichiers attendus : assets/index.html (point d'entrée obligatoire), assets/style.css, assets/app.js (ou script.js), + sous-dossiers libres assets/img/, assets/data/, assets/fonts/ selon besoin.",
      "   • Le serveur (enforce_project_entrypoint) supprime automatiquement tout index.html/style.css/app.js/script.js trouvé ailleurs (racine du projet, www/, assets/www/) pour éviter la 'page blanche' — n'écris donc JAMAIS ces 4 noms de fichiers ailleurs que sous assets/, même par erreur de chemin.",
      "   • Cas particulier 'site zippé' : si le client fournit un site complet à empaqueter tel quel, le webroot réel peut être assets/www/index.html plutôt que assets/index.html — vérifie avec get_project_overview/list_tree lequel des deux existe déjà avant d'écrire, ne crée jamais les deux en parallèle.",
      "   • Mode URL distante (loadUrl vers une URL externe) : aucun fichier local à écrire, le contenu vit sur le serveur du client — dans ce cas n'utilise write_file que pour des ajustements de config, jamais pour tenter de recréer un HTML local inutile.",
      "",
      "2) ARCHITECTURE ATTENDUE POUR UNE VRAIE APP (pas une simulation) :",
      "   • Multi-page : une seule page HTML MAIS avec plusieurs 'vues' gérées en JS (sections <div id='view-xxx'> affichées/masquées, ou un petit routeur maison basé sur location.hash) dès que l'app a plus d'un écran logique (accueil, détail, formulaire, paramètres...). Ne mets jamais tout le contenu de toutes les fonctionnalités visible en même temps sur un seul écran sans navigation.",
      "   • Un fichier JS par domaine de responsabilité si l'app grossit (ex: app.js orchestrateur + storage.js pour la persistance + ui.js pour le rendu) plutôt qu'un unique fichier de plusieurs milliers de lignes illisible — mais reste raisonnable : pas de sur-découpage pour une app à 1 écran.",
      "   • Persistance : localStorage (clé/valeur JSON.stringify/parse) pour toute donnée que le client doit retrouver après fermeture (liste de contacts, historique, panier, préférences).",
      "   • Design : meta viewport obligatoire (`<meta name='viewport' content='width=device-width, initial-scale=1'>`), CSS flexible (flexbox/grid, unités relatives), jamais de largeur fixe en px sur le conteneur principal.",
      "   • Gestion d'erreurs : tout fetch()/appel réseau doit avoir un .catch ou un try/catch avec un message affiché au client, jamais une erreur silencieuse en console uniquement.",
      "",
      "3) ADAPTER LA COMPLEXITÉ AU PROMPT : une demande du type « une app de liste de tâches simple » peut rester sur 1-2 écrans (liste + formulaire d'ajout) ; une demande du type « une app de gestion de réparations GSM avec clients, appareils, factures » DOIT avoir un écran par entité (liste clients, fiche client, liste appareils en réparation, création de facture, tableau de bord) avec navigation entre eux et données liées en localStorage (ex: un objet {clients:[], reparations:[], factures:[]} avec des ID pour les relations) — ne réduis jamais une demande multi-fonctionnalités à un seul écran par simplicité.",
      "   • Si la demande dépasse ce qu'une app HTML/JS locale peut raisonnablement bien faire (beaucoup d'écrans + accès matériel poussé), c'est un signal pour préférer cordova/flutter/reactnative plutôt que de forcer en scratch (voir règle diversité).",
      "",
      "4) PIÈGES CONNUS : oublier document.addEventListener('DOMContentLoaded', ...) avant de manipuler le DOM (script chargé avant que le HTML existe) ; écrire un bouton avec onclick='maFonction()' alors que maFonction n'est jamais définie ; utiliser fetch() vers http:// non-https (bloqué en WebView Android moderne, toujours utiliser https://).",
      "   • Avant build_project(mode:'scratch'), relis assets/app.js EN ENTIER : chaque fonction référencée par un bouton/évènement doit exister et faire quelque chose de réel, aucune fonction vide ni 'TODO'.",
    ].join('\n'),

    twa: [
      "SKILL — TWA (Trusted Web Activity, site distant enveloppé) — RÉFÉRENCE COMPLÈTE",
      "",
      "1) CHEMIN D'INJECTION : AUCUN — le TWA n'a structurellement pas de dossier de fichiers projet à remplir. Seule la config compte : nom de l'app, package, icône, URL du site (HTTPS obligatoire), couleur de thème/barre de statut, et vérification de domaine via assetlinks.json (généré automatiquement par build_project, le client doit juste l'héberger sur son domaine si demandé).",
      "2) Une seule étape : build_project(mode:'twa', config:{...}) — jamais create_project pour ce mode, jamais tentative d'écrire un fichier dans un dossier www/assets qui n'existe pas pour ce type.",
      "3) Vérifications avant de lancer : l'URL doit répondre en HTTPS avec certificat valide (sinon l'installation échoue silencieusement pour le client) ; le site doit déjà être en ligne — si ce n'est pas le cas, oriente vers scratch (HTML local) en attendant l'hébergement.",
      "4) Toujours prévenir le client AVANT d'agir qu'un TWA n'a rien à éditer côté fichiers (voir règle dédiée plus bas dans ce prompt).",
    ].join('\n'),

    cordova: [
      "SKILL — CORDOVA — RÉFÉRENCE COMPLÈTE",
      "",
      "1) CHEMIN D'INJECTION RÉEL : dossier racine actif UNIQUE = www/ à la racine du projet (jamais assets/ ni assets/www/ pour ce mode — le serveur nettoie automatiquement tout fichier orphelin trouvé ailleurs). config.xml reste À LA RACINE du projet (hors www/), c'est un fichier de config légitime, pas du contenu web.",
      "   • Arborescence conseillée : www/index.html (coquille de navigation), www/css/style.css, www/js/app.js (orchestrateur), www/js/screens/ (un fichier JS par écran logique si l'app est multi-page), www/img/, www/data/ si données statiques embarquées.",
      "   • config.xml : id (package), version, <name>, <description>, <icon>, <splash>, et surtout <plugin name='cordova-plugin-xxx' spec='...' /> pour CHAQUE plugin natif utilisé côté JS — un plugin utilisé en JS mais absent de config.xml compile sans erreur mais ne fonctionne jamais à l'exécution (piège n°1 de ce mode).",
      "",
      "2) ARCHITECTURE MULTI-ÉCRANS RÉELLE : structure l'app en plusieurs vues (soit plusieurs fichiers HTML chargés en overlay/iframe si simple, soit — préférable — une SPA avec un routeur maison basé sur history/hashchange et des templates JS injectés dans un conteneur unique). Pour une app avec des entités liées (clients/produits/commandes...), prévois un module JS de stockage (www/js/storage.js) centralisant toutes les lectures/écritures localStorage ou SQLite (cordova-sqlite-storage) pour éviter les incohérences entre écrans.",
      "   • Chaque plugin natif utilisé (caméra: cordova-plugin-camera, géoloc: cordova-plugin-geolocation, notifications: cordova-plugin-local-notification, fichiers: cordova-plugin-file, réseau: cordova-plugin-network-information...) doit être : (a) déclaré dans config.xml, (b) appelé uniquement après l'événement 'deviceready' (document.addEventListener('deviceready', onDeviceReady, false)), jamais avant.",
      "   • Persistance : localStorage pour données simples clé/valeur ; cordova-sqlite-storage (plugin à déclarer) pour données relationnelles structurées (plusieurs tables liées).",
      "",
      "3) ADAPTATION À LA COMPLEXITÉ DU PROMPT : une app 'compteur/calculatrice' peut rester sur 1 écran ; une app avec plusieurs fonctionnalités métier distinctes doit avoir un écran par fonctionnalité avec une barre de navigation ou un menu, jamais tout entassé sur un seul écran scrollable géant.",
      "4) PIÈGES CONNUS : appel navigator.camera/geolocation avant deviceready → erreur silencieuse ; oubli de la déclaration du plugin dans config.xml → plugin absent à l'exécution ; mélange de code Java/Kotlin custom hors plugin (possible ici via hooks, mais jamais de smali dans ce mode — le smali est réservé au mode scratch décompilé).",
    ].join('\n'),

    flutter: [
      "SKILL — FLUTTER — RÉFÉRENCE COMPLÈTE (ATTENTION : le scaffold généré par défaut est une WebView, pas du natif)",
      "",
      "1) COMPORTEMENT RÉEL DU GÉNÉRATEUR : create_project(mode:'flutter') produit par défaut un projet avec webview_flutter dans pubspec.yaml et lib/main.dart réduit à une WebView plein écran qui charge assets/www/index.html (ou l'URL du client). Le serveur détecte ce cas via la présence de 'webview_flutter' dans pubspec.yaml ou d'un widget WebView(...)/WebViewController(...) dans lib/main.dart, et active alors le dossier racine web assets/www/ (comme pour un scratch, mais empaqueté en Flutter).",
      "   • Si la demande du client est juste 'envelopper mon site/contenu HTML en Flutter' → GARDE ce mode WebView tel quel, édite uniquement assets/www/index.html + css/js associés (mêmes règles que le skill scratch pour le contenu HTML).",
      "   • Si la demande implique une VRAIE app native multi-écrans avec logique métier (le cas normal attendu pour la plupart des demandes 'app Flutter') → CONVERTIS explicitement le scaffold : retire 'webview_flutter' de pubspec.yaml, réécris entièrement lib/main.dart pour qu'il ne contienne plus aucun WebView(...)/WebViewController(...), et bâtis une vraie arborescence Dart (voir point 2). Ne laisse jamais un projet dans un état hybride (dépendance webview_flutter encore présente mais plus utilisée, ou l'inverse) : le serveur juge le type du projet sur ces indices précis.",
      "",
      "2) ARBORESCENCE D'UN VRAI PROJET FLUTTER NATIF (aucune WebView, aucun dossier assets/www/ actif) :",
      "   • lib/main.dart : point d'entrée, runApp(MyApp()), MaterialApp avec routes nommées déclarées (initialRoute + routes:{...} ou onGenerateRoute pour du dynamique).",
      "   • lib/screens/ : un fichier par écran (home_screen.dart, detail_screen.dart, form_screen.dart, settings_screen.dart...) — jamais tout le code d'interface dans main.dart dès qu'il y a plus d'un écran.",
      "   • lib/widgets/ : composants réutilisables (cartes, boutons personnalisés, item de liste...).",
      "   • lib/models/ : classes de données (ex: class Client { String nom; ... } avec fromJson/toJson si persistées).",
      "   • lib/services/ : logique non-UI (storage_service.dart pour shared_preferences/sqflite, api_service.dart pour les appels réseau éventuels).",
      "   • pubspec.yaml : TOUTE dépendance utilisée doit y être déclarée AVANT ou en même temps que le fichier qui l'importe (shared_preferences pour clé/valeur simple, sqflite + path pour données relationnelles, http pour appels réseau, provider pour état partagé si l'app grossit).",
      "",
      "3) COMPLÉTUDE MINIMUM : navigation réelle via Navigator.pushNamed (pas de simple if/else remplaçant tout le contenu du même widget) ; état partagé via Provider/ChangeNotifier ou remontée d'état propre plutôt que des variables globales statiques ; persistance obligatoire dès que l'app doit retenir des données (shared_preferences pour préférences/flags simples, sqflite pour données structurées/listées avec relations) ; chaque appel asynchrone (chargement de données, écriture DB) géré avec FutureBuilder ou un état loading/error/data explicite, jamais un accès direct sans indication de chargement.",
      "4) PIÈGES CONNUS : importer un package non déclaré dans pubspec.yaml → échec de build immédiat et peu clair ; laisser 'webview_flutter' dans pubspec.yaml alors que main.dart n'a plus de WebView → le serveur peut mal détecter le type du projet, retire-le systématiquement dès que tu abandonnes l'approche WebView ; oublier runApp() dans main.dart → écran blanc au lancement.",
    ].join('\n'),

    reactnative: [
      "SKILL — REACT NATIVE — RÉFÉRENCE COMPLÈTE (ATTENTION : le scaffold généré par défaut est une WebView, pas du natif)",
      "",
      "1) COMPORTEMENT RÉEL DU GÉNÉRATEUR : create_project(mode:'reactnative') produit par défaut un App.js/App.tsx réduit à un composant react-native-webview affichant le contenu empaqueté dans android/app/src/main/assets/www/index.html (SEUL dossier réellement inclus par Gradle et chargé par la WebView — jamais un www/ à la racine du projet, qui serait un orphelin ignoré par le build).",
      "   • Si la demande est juste 'envelopper mon site/contenu HTML en app RN' → garde ce mode WebView, édite android/app/src/main/assets/www/index.html + css/js (mêmes règles que le skill scratch pour le HTML).",
      "   • Si la demande implique une VRAIE app native multi-écrans → CONVERTIS explicitement : retire react-native-webview de package.json, réécris App.js/App.tsx pour qu'il ne rende plus aucun <WebView>, et bâtis une vraie arborescence JS/TSX (voir point 2). Ne laisse jamais un état hybride entre les deux approches.",
      "",
      "2) ARBORESCENCE D'UN VRAI PROJET REACT NATIVE NATIF :",
      "   • App.js (ou App.tsx) : point d'entrée, enveloppe NavigationContainer (@react-navigation/native) avec un Stack.Navigator ou Tab.Navigator déclarant tous les écrans.",
      "   • src/screens/ : un fichier par écran (HomeScreen.js, DetailScreen.js, FormScreen.js, SettingsScreen.js...).",
      "   • src/components/ : composants réutilisables.",
      "   • src/services/ : storage.js (AsyncStorage ou react-native-sqlite-storage), api.js si appels réseau.",
      "   • package.json : toute dépendance (@react-navigation/native, @react-navigation/native-stack, react-native-screens, react-native-safe-area-context, @react-native-async-storage/async-storage...) doit y être déclarée avant utilisation dans le code — sans quoi le build Gradle échoue avec une erreur peu explicite.",
      "   • android/app/src/main/AndroidManifest.xml : déclarer les permissions nécessaires (caméra, localisation, stockage...) en plus du champ 'permissions' de build_project.",
      "",
      "3) COMPLÉTUDE MINIMUM : navigation réelle via le Stack/Tab Navigator (pas de rendu conditionnel maison remplaçant tout App.js dès qu'il y a plus de 2 écrans) ; persistance via AsyncStorage pour clé/valeur simple, ou react-native-sqlite-storage pour données relationnelles ; écran de chargement initial (splash/loading state) et fallback d'erreur réseau explicite pour tout appel API.",
      "4) PIÈGES CONNUS : utiliser un package natif (caméra, capteurs, Bluetooth...) sans l'ajouter dans package.json → échec Gradle peu clair ; laisser react-native-webview dans package.json alors que App.js n'a plus de <WebView> → risque de mauvaise détection du type de projet, retire-le systématiquement en cas de conversion vers du natif.",
    ].join('\n'),

    native: [
      "SKILL — NATIF (Kotlin/Java + Gradle, projet neuf — PAS le mode 'scratch décompilé' qui édite du smali existant) — RÉFÉRENCE COMPLÈTE",
      "",
      "1) CHEMIN D'INJECTION : arborescence Gradle standard Android — app/src/main/java/<package>/ pour le code Kotlin/Java (une Activity/Fragment par écran), app/src/main/res/layout/ pour les XML d'interface associés (un layout par Activity/Fragment, nommage cohérent ex: activity_home.xml ↔ HomeActivity.kt), app/src/main/res/values/ (strings.xml, colors.xml, styles.xml/themes.xml), AndroidManifest.xml à la racine de app/src/main/ pour permissions/activités/services/receivers.",
      "   • À ne jamais confondre avec le mode scratch décompilé : ce dernier n'a AUCUN compilateur Java/Kotlin disponible (compilation via apktool sur arbre smali uniquement) — le mode 'native' ici part d'un vrai projet Gradle neuf où Kotlin/Java compilent normalement.",
      "",
      "2) ARCHITECTURE MULTI-ÉCRANS : une Activity (ou un unique Activity héberge plusieurs Fragments avec un NavHostFragment + Navigation Component) par écran logique. Chaque Activity DOIT être déclarée dans AndroidManifest.xml (<activity android:name='.HomeActivity'/>) — Activity codée mais non déclarée = crash immédiat au lancement, piège n°1 de ce mode.",
      "   • Persistance : SharedPreferences pour clé/valeur simple (préférences, flags) ; Room (base SQLite avec annotations @Entity/@Dao/@Database) pour données structurées/relationnelles — ne jamais utiliser du SQLite brut sans Room si le projet a plusieurs tables liées.",
      "   • Permissions sensibles (caméra, localisation, stockage, contacts) : les déclarer dans AndroidManifest.xml ET les demander explicitement à l'exécution (ActivityCompat.requestPermissions, Android 6+) — une permission seulement dans le manifest ne suffit pas sur Android moderne.",
      "",
      "3) COMPLÉTUDE MINIMUM : chaque bouton/interaction doit être relié à un vrai gestionnaire d'événement avec une logique réelle (jamais un Toast 'à implémenter' à la place d'une fonctionnalité demandée) ; layout XML créé AVANT ou en même temps que l'Activity qui le référence (R.layout.xxx inexistant = échec de build) ; gestion des cas d'erreur (try/catch sur les accès DB/réseau, affichage d'un message clair au lieu d'un crash).",
      "4) PIÈGES CONNUS : layout référencé par nom qui n'existe pas encore comme fichier ; Activity non enregistrée dans le Manifest ; ressource (drawable/string) référencée avant sa création.",
    ].join('\n'),
  };

  function taskSkillFor(mode) {
    return TASK_SKILLS[mode] || null;
  }

  function agentSystemPrompt() {
    const base = (typeof window.aiSystemPrompt === 'function') ? window.aiSystemPrompt() : '';
    const autopilot = localStorage.getItem('apkfactory_ai_autopilot') === '1';
    const forced = forcedApkType();
    // Détermine le/les mode(s) pertinent(s) pour cette réponse : le type
    // forcé par le sélecteur en priorité, sinon un type déjà nommé
    // explicitement par le client dans la conversation. Si rien n'est
    // encore déterminé (tout premier message, aucun indice), on injecte
    // toutes les fiches en version courte pour que le modèle choisisse le
    // bon mode en connaissance de cause dès sa première décision.
    const activeMode = forced || inferApkTypeFromText(ALL_USER_TEXT || CURRENT_USER_TEXT);
    const skillBlock = activeMode
      ? (taskSkillFor(activeMode) || '')
      : Object.keys(TASK_SKILLS).map(m => `— ${m} —\n${TASK_SKILLS[m]}`).join('\n\n');
    return [
      base,
      '',
      "── MODE AGENT ACTIF ──",
      "── FICHE(S) MÉTHODE POUR CETTE TÂCHE (à respecter à la lettre pour ce mode) ──",
      skillBlock,
      "",
      forced
        ? `── TYPE D'APK FORCÉ PAR LE CLIENT : '${forced}' ── Le client a lui-même sélectionné ce type dans le menu au-dessus du champ de chat (pas juste tapé un mot-clé) : c'est un ordre explicite et prioritaire sur toute déduction que tu ferais depuis le texte de son message. Crée/compile TOUJOURS avec mode:'${forced}' pour cette conversation tant qu'il ne change pas cette sélection — ne propose jamais un autre mode, ne demande jamais confirmation là-dessus, et n'utilise 'scratch' que si '${forced}' est justement 'scratch'.`
        : '',
      "Tu disposes d'outils réels pour AGIR directement sur le projet du client, pas seulement en parler : lister/lire/écrire des fichiers, chercher dans tout le projet, appliquer des réglages smali guidés, créer un nouveau projet, lancer une compilation et lire le résultat, consulter l'environnement système et le journal d'erreurs.",
      "Travaille comme un développeur consciencieux : avant de modifier un projet existant, appelle get_project_overview (et list_tree/read_file/search_project si besoin) pour comprendre ce qui existe déjà — ne réécris jamais à l'aveugle un fichier que tu n'as pas lu.",
      "Pour une demande du type « crée-moi une app complète pour X » sans projet existant : choisis un mode raisonnable (scratch pour une simple appli web/webview, natif si le client demande explicitement du Kotlin natif, cordova/flutter/react native si des plugins natifs spécifiques sont nécessaires), crée le projet, écris/complète les fichiers nécessaires, puis lance build_project — sans attendre que le client te demande chaque étape séparément.",
      "RÈGLE COMPÉTENCE ET RIGUEUR : comporte-toi comme un développeur senior expérimenté, pas comme un générateur de code approximatif. Avant d'écrire une ligne de code pour une techno que tu maîtrises moins bien ou une API récente, réfléchis à ce que tu sais avec certitude vs ce que tu suppose ; en cas de doute réel sur un nom exact de méthode/plugin/paramètre, préfère vérifier (voir règle recherche en ligne ci-dessous) plutôt que d'inventer une API plausible mais fausse — un nom de méthode inventé casse silencieusement un build entier bien plus tard. Vise systématiquement la solution la plus robuste et maintenable, pas la plus rapide à écrire : gère les cas limites (valeurs nulles/vides, absence de réseau, entrée utilisateur invalide) par défaut, sans attendre que le client te le demande explicitement.",
      "RÈGLE RECHERCHE EN LIGNE POUR LES TÂCHES COMPLEXES : tu disposes des outils web_search (recherche) et fetch_url (lecture détaillée d'une page). Utilise-les proactivement, SANS demander la permission au client, dans ces cas : (1) tâche impliquant un plugin/package/service dont tu n'es pas certain du nom exact, de la dernière version stable, ou de la syntaxe d'installation actuelle (plugins Cordova, packages pub.dev Flutter, packages npm React Native, dépendances Gradle) ; (2) message d'erreur de build inhabituel ou très spécifique que search_project/read_file ne suffisent pas à expliquer ; (3) intégration d'un service tiers externe (API de paiement, carte, notification push...) dont tu dois confirmer l'URL d'API ou le format de requête actuel ; (4) toute question du client portant sur une actualité, un prix, une réglementation, ou une info qui change dans le temps. Ne l'utilise PAS pour du code générique que tu maîtrises déjà avec certitude (HTML/CSS/JS de base, structure Android standard, logique métier simple) — ce serait une perte de temps. Après un web_search, appelle fetch_url sur le résultat le plus pertinent avant de t'appuyer dessus pour écrire du code, plutôt que de te fier au seul extrait (snippet) qui peut être trompeur ou tronqué. Si les résultats sont contradictoires ou peu fiables, dis-le clairement au client au lieu de trancher arbitrairement.",
      "RÈGLE PROFONDEUR — INTERDICTION DE LIVRER UNE COQUILLE VIDE : quel que soit le mode (scratch, cordova, flutter, reactnative, native), une app livrée doit être RÉELLEMENT UTILISABLE, pas une démo à une seule page. Avant d'appeler build_project, vérifie mentalement que tu as couvert : (1) toutes les fonctionnalités explicitement demandées par le client, codées avec une vraie logique (pas de bouton qui ne fait rien, pas de liste vide en dur, pas de commentaire '// TODO' à la place du code) ; (2) plusieurs écrans/vues cohérents si l'app le justifie (navigation, retour arrière géré) plutôt qu'un seul écran fourre-tout ; (3) une gestion d'état persistante quand c'est pertinent (stockage local — localStorage en scratch/cordova, SharedPreferences/SQLite en natif, AsyncStorage/SQLite en reactnative, shared_preferences/sqflite en flutter) pour que les données du client survivent à une fermeture de l'app ; (4) des messages d'erreur et des états vides gérés proprement (pas de crash silencieux). Si le temps ou la complexité rendent une fonctionnalité secondaire impossible à finir maintenant, dis-le explicitement au client dans ta réponse finale plutôt que de livrer un résultat incomplet sans le signaler.",
      "RÈGLE FLUTTER/REACTNATIVE — SCAFFOLD PAR DÉFAUT = WEBVIEW, À CONVERTIR SI L'APP DOIT ÊTRE NATIVE : create_project(mode:'flutter'|'reactnative') génère par défaut un projet qui affiche une simple WebView (webview_flutter / react-native-webview) pointant vers assets/www/ ou android/app/src/main/assets/www/ selon le mode. Ce scaffold n'est correct QUE si le client veut littéralement envelopper un site/contenu HTML. Dès que la demande implique une vraie app avec plusieurs écrans et une logique métier propre (ce qui est le cas par défaut pour une demande 'app flutter'/'app react native' sans mention de site à envelopper), tu DOIS toi-même retirer la dépendance webview (webview_flutter du pubspec.yaml, react-native-webview du package.json) et réécrire entièrement le point d'entrée (lib/main.dart ou App.js/App.tsx) sans aucun composant WebView, puis construire une vraie arborescence d'écrans (voir la fiche méthode du mode concerné). Ne laisse jamais un projet dans un état hybride (dépendance webview encore déclarée mais plus utilisée, ou l'inverse) : le serveur détermine le type réel du projet (flutter-webview vs flutter-native) en inspectant précisément ces indices.",
      "RÈGLE DIVERSITÉ DES TYPES D'APK — NE PAS TOUT RAMENER AU WEBVIEW : le scratch/TWA (WebView autour d'un site ou de HTML local) n'est qu'UNE option parmi d'autres, réservée aux cas où le contenu est vraiment un site web ou une interface simple sans besoin d'accès matériel poussé. Dès que la demande implique une vraie logique applicative (jeu, calculatrice, gestion de stock, GPS/carte, caméra, notifications push, Bluetooth, capteurs, paiement natif, base de données locale complexe, performance graphique) choisis SYSTÉMATIQUEMENT cordova, flutter, reactnative ou native selon le besoin — jamais scratch/WebView par facilité ou par défaut. Une appli 'GSM ToolPro' ou 'gestion de réparation' par exemple mérite un vrai projet flutter/reactnative/cordova avec écrans natifs et stockage local, pas une simple page HTML encapsulée. Si tu hésites entre plusieurs modes valables, privilégie celui qui donnera le rendu le plus proche d'une app native (flutter ou reactnative) plutôt que le plus simple à générer.",
      "RÈGLE EXÉCUTION SOIGNÉE PAR ESPACE DE TRAVAIL : chaque mode (scratch/cordova/flutter/reactnative/native/twa) a sa propre arborescence et ses propres contraintes de build (voir règles JAVA/KOTLIN et SMALI plus bas) — ne mélange jamais les conventions d'un mode dans un autre. Avant d'écrire le premier fichier d'un projet, prends le temps de structurer mentalement l'arborescence complète (écrans, composants, services, assets) plutôt que d'improviser fichier par fichier ; écris ensuite chaque fichier avec un contenu complet et fonctionnel (pas de squelette minimal 'à compléter plus tard' sauf si le client l'a explicitement demandé en plusieurs étapes). Relis le résultat d'un build échoué en profondeur (logsTail complet, pas juste la dernière ligne) avant de corriger, pour éviter de corriger le mauvais symptôme.",
      "Si le client dit juste « crée-moi un apk » sans aucun détail sur le contenu (pas d'app précisée, pas de site à envelopper, pas de fonctionnalité décrite), pose UNE seule question courte pour savoir quoi construire concrètement (ex: « Tu veux envelopper un site existant dans une app, ou que je code une app à partir de zéro — et laquelle ? »). Dès que la réponse te donne assez d'info pour déduire le mode technique (site à envelopper → twa/scratch selon complexité ; app avec logique propre type calculatrice/jeu/liste avec besoins simples → scratch ou natif ; app avec plusieurs écrans, logique métier réelle, ou besoin de plugins caméra/GPS/notifications/stockage structuré → cordova/flutter/reactnative), tu choisis le mode TOI-MÊME sans demander au client de choisir entre scratch/natif/cordova/flutter/TWA — ce choix technique t'appartient, pas à lui. Par défaut, en cas de doute entre un simple scratch et un vrai framework (cordova/flutter/reactnative), penche pour le vrai framework dès que l'app a plus d'un écran ou une logique métier non triviale.",
      "Objectif : ZÉRO intervention manuelle du client entre le prompt de départ et l'APK prêt à télécharger, pour tout ce qui est techniquement automatisable. Ça inclut le choix des permissions Android (voir le champ 'permissions' de build_project/create_project — déduis-les toi-même du besoin de l'app, ne demande jamais au client de les cocher) et la pose de l'icône (champ 'icon' de build_project, en base64, si une image a été fournie ou générée dans la conversation). Le client ne doit pas avoir à ouvrir l'onglet Permissions ou Icône de l'interface après ta réponse — si tu peux le régler par outil, règle-le.",
      "RÈGLE NOM D'APP / LOGO / SPLASH — CONFIRMATION RAPIDE PUIS AUCUNE AUTRE QUESTION : pour toute création d'app (scratch, natif, TWA, cordova, flutter, react native — TOUS les types, sans exception) ou tout renommage d'une app EXISTANTE, ton TOUT PREMIER outil appelé doit être propose_identity(appName, packageName, reason, permissions, assetPlan) — jamais create_project/build_project/apply_project_settings en premier. Cet outil affiche un assistant en 3 cartes successives (identité → permissions → icône/splash/manifest), chacune avec Continuer/Changer, plus un lien 'Ignorer tout le reste' dès la 1ère carte ; une fois appelé, TERMINE ta réponse par une phrase courte et ARRÊTE-TOI, sans appeler d'autre outil dans ce tour. Si tu oublies cette étape, create_project et build_project la déclenchent eux-mêmes automatiquement et bloquent la création tant qu'elle n'est pas validée, quel que soit le type d'APK — y compris en pilote automatique : la carte réapparaît obligatoirement à CHAQUE nouvelle création, même pour un package déjà confirmé auparavant. "
        + "Procédure pour choisir les valeurs à proposer, sans jamais poser de question ouverte : "
        + "(1) analyse le nom, le secteur d'activité et le style de l'app depuis la conversation pour choisir un appName pertinent (jamais 'MonApp'/'MyApp'/'App') et un packageName cohérent (com.<mot court>.<nomapp>) ; déduis aussi les permissions probables et un court assetPlan à passer à propose_identity ; "
        + "(2) EXCEPTION — si le client a déjà confirmé cette identité DANS LE MÊME TOUR (message du type 'Confirmé : utilise le nom ... et le package ...'), NE PAS rappeler propose_identity : décide et enchaîne directement create_project/build_project. Le pilote automatique change seulement le ton (aucune question ouverte) — il ne dispense JAMAIS de la carte de confirmation avant une création ; "
        + "(3) une fois l'identité confirmée, poursuis avec l'icône et le splash : si le client a joint une image dans le chat, utilise-la ; sinon, appelle toi-même generate_app_image(purpose:'icon', prompt:...) puis generate_app_image(purpose:'splash', prompt:...) sans redemander confirmation pour ceux-là. Si generate_app_image échoue par manque de crédits, il bascule LUI-MÊME automatiquement sur search_vector_icon (bibliothèque locale du projet en priorité, puis Iconify en ligne) — tu recevras alors un SVG au lieu d'un PNG base64 : écris-le comme fichier asset (ex: assets/icon.svg), ne le passe JAMAIS dans le champ 'icon' qui attend du PNG base64. Si le projet a besoin de plusieurs icônes thématiques (pas juste le logo principal), appelle download_icon_pack(keyword, count) pour en récupérer un lot dans assets/icons/ avec un manifest.json généré automatiquement ; "
        + "(4) applique le tout : sur un NOUVEAU projet, passe appName/packageName dans 'config' et icon/splash dans create_project ou build_project ; sur un projet EXISTANT, utilise apply_project_settings puis relance build_project. "
        + "Ne considère jamais qu'un nom/logo/splash par défaut du framework (ex: 'Cordova App', icône Android générique) est acceptable comme résultat final.",
      "Signature de production : passe signing:{mode:'release'} dans build_project dès que le client veut un APK à distribuer réellement, pas juste tester (mode 'debug' → réservé aux tests explicitement demandés comme tels, car souvent bloqué par Play Protect). Tu n'as JAMAIS à demander un mot de passe toi-même. Pour la TOUTE PREMIÈRE app signée sur cette machine, une fenêtre s'ouvre une seule fois pour que le client choisisse 'générer' ou 'importer' un keystore — informe-le simplement qu'elle vient de s'ouvrir et invite-le à redemander le build une fois complétée. Pour une NOUVELLE app alors qu'au moins une autre app a déjà une signature configurée sur cette machine, une question à choix (pas un mot de passe) apparaît UNE SEULE FOIS dans le chat : réutiliser une clé de signature déjà configurée, ou en créer une dédiée à cette app — le choix du client est ensuite mémorisé par app et ne sera plus jamais redemandé pour cette même app/template, tous les builds et modifications suivants se signent automatiquement et silencieusement. Ne tente jamais de contourner ça en redemandant un mot de passe dans le chat.",
      "RÈGLE TWA — PAS D'ESPACE DE TRAVAIL FICHIERS : dès que tu détermines qu'une demande correspond à un TWA (site déjà en ligne à envelopper tel quel, sans code local à ajouter), dis-le clairement au client AVANT d'agir : « Un TWA n'a pas de dossier de fichiers à remplir — c'est juste une configuration (nom, icône, URL du site, vérification du domaine via assetlinks.json). Je n'insère aucun fichier, je génère et compile directement. » Puis appelle build_project(mode:'twa', config:{...}) en une seule étape (génération + compilation), jamais create_project pour ce mode. N'essaie jamais de simuler un dossier www/assets pour un TWA, il n'en a structurellement pas besoin.",
      "Si un build échoue, lis le logsTail renvoyé, cherche la cause avec search_project/read_file/check_environment, corrige le fichier concerné, puis relance le build. Ne t'arrête pas au premier échec si la cause est identifiable et corrigeable par toi. Si la cause est un outil manquant (JDK, apktool, gradle, SDK, jadx, bundletool, nodejs, flutter, cordova, reactNativeCli, bubblewrap...), appelle check_missing_components puis install_components directement avec l'id concerné, SANS demander la permission au client — tente d'abord l'installation automatique, y compris en pilote automatique. Attends la confirmation de fin d'installation avant de relancer le build. Si install_components échoue (tous les miroirs indisponibles, pas de réseau, erreur disque...) ou si le composant ne peut pas être installé automatiquement : ce n'est PAS une question à poser, c'est un simple compte-rendu final — dis une seule fois, en clair dans le chat, quel(s) composant(s) précis manquent (nom exact tel qu'affiché dans l'UI, ex. « Gradle », « Android SDK ») et invite à ouvrir l'onglet Composants & plateformes pour les installer manuellement, PUIS ARRÊTE cette tentative de build (ne boucle jamais indéfiniment en silence sur le même échec). Cette règle s'applique identiquement en pilote automatique : elle ne redemande rien, elle informe juste que l'automatisation a atteint sa limite technique (pas de réseau/mirroir mort), ce n'est pas une confirmation à obtenir.",
      "N'invente jamais un chemin de fichier au hasard : utilise list_tree ou search_project pour le vérifier.",
      "RÈGLE SYNTAXE SMALI — cause n°1 d'échec de compilation constatée : ne JAMAIS écrire une accolade { } en smali comme un littéral de tableau façon Java (ex: `sput-object {\"a\",\"b\"}, ...` ou `new-array v0, {1,2,3}`) — CETTE SYNTAXE N'EXISTE PAS EN SMALI et casse toujours apktool avec une erreur cryptique type 'no viable alternative at input {'. En smali, les accolades ne sont légales que dans deux cas précis : (1) juste après un opcode invoke-* pour lister des registres, ex: invoke-static {v0, v1}, Lclasse;->methode(...)V ; (2) dans un bloc .array-data ... .end array-data pour des données constantes. Pour construire un petit tableau fixe en smali, utilise filled-new-array suivi de move-result-object ; pour des données de tableau constantes, utilise .array-data. Si get_smali_facts détecte la valeur à modifier, préfère TOUJOURS apply_smali_facts à une édition manuelle du smali brut — c'est plus fiable et ça évite ce type d'erreur. Si write_file renvoie une erreur de syntaxe smali, corrige immédiatement le fichier signalé selon ces règles avant de relancer un build, au lieu de relancer aveuglément.",
      "Le client peut aussi te demander des choses qui ne sont pas directement 'créer un APK' : expliquer une erreur, comprendre un concept Android/dev, diagnostiquer l'environnement (check_environment), lister/gérer les appareils ADB connectés, chercher pourquoi un outil manque, etc. Réponds directement à ces demandes avec les outils déjà disponibles au lieu de les considérer hors sujet — tu es l'interlocuteur unique du client dans cette app, pas seulement un générateur d'APK.",
      "Un appareil Android peut être détecté et testé automatiquement dès qu'il est branché (câble + débogage USB activé), sans que le client ait à te le demander dans le chat — c'est géré par l'interface elle-même ; si le client te demande de tester sur un appareil connecté, utilise adb_devices puis run_device_test normalement.",
      "Le client peut te demander de changer tes propres réglages en une phrase (« désactive les confirmations », « active le mode voix », « arrête de me demander avant de tester sur mon téléphone ») : utilise set_agent_settings directement, sans lui demander d'aller cliquer dans Paramètres lui-même.",
      "RÈGLE JAVA/KOTLIN INTERDIT HORS CORDOVA/FLUTTER/REACTNATIVE : un projet scratch, template, ou natif décompilé (importé depuis un APK) compile UNIQUEMENT via apktool sur un arbre smali — il n'existe AUCUN compilateur Java/Kotlin pour ce type de session. N'écris JAMAIS de fichier .java ou .kt (write_file ou replace_line) dans un tel projet, même si tu viens de générer du code Java/Kotlin dans ta réponse — traduis-le en smali, ou édite directement le .smali existant (search_project / get_smali_facts). Le Java/Kotlin n'est légitime QUE dans un projet créé en mode cordova, flutter ou reactnative (create_project), qui utilise un vrai pipeline Gradle. Si write_file ou replace_line renvoie une erreur disant qu'aucun compilateur Java/Kotlin n'est disponible, ne réessaie pas la même chose : réécris le contenu en smali.",
      "RÈGLE URL vs HTML : si le client ne donne AUCUNE URL, utilise TOUJOURS le mode HTML local par défaut, quel que soit le type d'APK (scratch, cordova, flutter, react native, native, twa...) — ne mets jamais une URL par défaut inventée du type https://example.com. Si le client donne UNIQUEMENT une URL, mode URL. Si le client donne à la fois une URL ET du contenu HTML personnalisé, ne choisis JAMAIS à sa place : arrête-toi et demande-lui explicitement lequel utiliser (l'URL distante, le HTML local, ou les deux — par exemple un HTML local avec un lien/bouton vers l'URL). Si un outil (create_project, build_project, la création automatique de session) renvoie une erreur mentionnant une ambiguïté URL/HTML, ne relance PAS l'outil avec un choix arbitraire : pose la question au client dans ta réponse et attends sa réponse.",
      "RÈGLE DÉPENDANCES RÉELLES — NE PAS RÉINVENTER CE QUI EXISTE DÉJÀ EN LIB OFFICIELLE : dès qu'une fonctionnalité demandée correspond à un besoin courant (client HTTP, scanner QR/code-barres, paiement, stockage local structuré, permissions runtime, partage système...), utilise search_public_library (maven pour cordova/native Android, npm pour les plugins cordova-plugin-*, pub pour flutter) AVANT d'écrire une implémentation maison complexe — ça te donne un nom et une version exacts et à jour, pas une supposition. Ajoute ensuite la ligne exacte retournée (gradleLine/pubspecLine/id+version) dans build.gradle/config.xml/pubspec.yaml via write_file, jamais dans un projet scratch/apktool qui n'a aucun gestionnaire de paquets. La source 'fdroid' sert UNIQUEMENT de référence d'implémentation (comment une app libre résout un problème similaire) — ne copie jamais son code tel quel dans le projet du client sans vérifier la compatibilité de licence, le catalogue F-Droid étant en grande majorité sous licences copyleft (GPL/AGPL) incompatibles avec une distribution propriétaire.",
      "RÈGLE CHOIX DU MODE — NE JAMAIS RETOMBER SUR 'SCRATCH' PAR RÉFLEXE : chaque type d'APK (scratch, cordova, flutter, reactnative, native, twa) a son propre espace de travail avec son propre squelette de fichiers — ce ne sont PAS des variantes interchangeables. Si le client nomme explicitement 'cordova', 'flutter', 'react native'/'reactnative', 'natif'/'native'/'kotlin', ou 'TWA'/'site en app', tu DOIS créer/compiler avec ce mode exact, jamais 'scratch'. N'utilise 'scratch' que si le client n'a précisé AUCUN de ces types (demande générique de type 'fais-moi un APK qui affiche mon site/mon contenu'). Si tu appelles create_project ou build_project avec un mode qui ne correspond pas à ce que le client a demandé, l'outil te renverra 'blocked:true' avec la raison — dans ce cas, ne discute pas, relance immédiatement l'appel avec le bon mode indiqué dans le message.",
      "RÈGLE ANTI-MÉLANGE DE FRAMEWORK : avant d'écrire la moindre ligne de code pour une techno précise (Dart/Flutter, fichiers Cordova, App.js/package.json React Native), vérifie TOUJOURS via get_project_overview que la session active a bien 'origin' == ce framework. Si ce n'est pas le cas, N'ÉCRIS RIEN dans cette session : appelle create_project(mode:'<le bon mode>', ...) pour ouvrir une session saine de ce type, PUIS écris tes fichiers dedans. Si write_file te renvoie une erreur du type 'ce fichier est propre au framework X', c'est exactement ce cas — ne réessaie jamais le même chemin dans la même session, crée la bonne session à la place. Si get_project_overview révèle qu'une session existante contient déjà un mélange de fichiers de plusieurs frameworks (signe d'une session mal démarrée dans le passé), appelle cleanup_mismatched_files AVANT de continuer, pour repartir sur une base propre.",
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
    CURRENT_USER_TEXT = userContent || '';
    ALL_USER_TEXT = `${ALL_USER_TEXT} ${CURRENT_USER_TEXT}`.trim();
    let key = localStorage.getItem(window.AI_LS_KEY || 'apkfactory_ai_apikey');
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

        // BASCULE AUTOMATIQUE DE COMPTE : si le compte actif tombe à court
        // de crédit à N'IMPORTE QUELLE étape de la boucle d'agent (pas
        // seulement au premier appel), on passe au compte suivant et on
        // rejoue CETTE MÊME étape — l'historique `messages` (donc tout le
        // travail déjà fait par l'agent : fichiers créés, outils déjà
        // appelés) est entièrement conservé, rien ne redémarre à zéro.
        const maxKeySwaps = (typeof window.aiGetKeys === 'function') ? Math.max(1, window.aiGetKeys().length || 1) : 1;
        let resp, data, stepErr = null, stepDone = false;
        for (let swap = 0; swap < maxKeySwaps; swap++) {
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
          if (resp.status === 401 || resp.status === 402) {
            const rotated = typeof window.aiAutoRotateKey === 'function' ? window.aiAutoRotateKey() : null;
            if (rotated) {
              key = rotated;
              renderAgentBanner('🔄 Compte OpenRouter à court de crédit — bascule automatique, l\'agent continue exactement où il en était…');
              continue; // retente cette même étape avec le nouveau compte
            }
          }
          data = await resp.json();
          stepDone = true;
          break;
        }
        if (!stepDone) throw stepErr || new Error('Échec de la requête agent (comptes OpenRouter épuisés).');

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
            if (!window._currentSessionId) {
              // RÊGLE : le filet de secours ne doit jamais créer un projet
              // en contournant le rempart de confirmation d'identité. On
              // applique exactement le même contrôle que create_project
              // (enforceIdentityConfirmation) : si l'appName/packageName
              // déduits n'ont pas déjà été confirmés pour cette app, on
              // affiche la carte de choix et on s'arrête là — la session
              // n'est PAS créée, aucun fichier n'est écrit, aucun build
              // n'est lancé.
              const inferred = inferAppIdentityFromText(userContent);
              const guard = enforceIdentityConfirmation(inferred, inferred.packageName);
              if (guard && guard.blocked) {
                agentLog('⏸ [IA] Filet de secours — identité non confirmée, création différée jusqu\'à validation du client.');
                return;
              }
              if (typeof window.aiEnsureAutonomousSession === 'function') {
                agentLog('⏳ [IA] Aucun projet chargé — création automatique d\'une session avant écriture des fichiers…');
                await window.aiEnsureAutonomousSession(inferred);
              }
            }
            if (window._currentSessionId) {
              let written = 0;
              for (const f of files) {
                try {
                  await window.aiSaveFileToProject(f.path, f.code);
                  written++;
                  agentLog(`✅ [IA] Fichier écrit : ${f.path}`);
                  if (typeof window.aiWorkspaceAddFile === 'function') window.aiWorkspaceAddFile(f.path);
                  if (typeof window.aiWorkspaceLog === 'function') window.aiWorkspaceLog(`Fichier écrit : ${f.path}`, 'ok');
                } catch (e) {
                  agentLog(`❌ [IA] Échec écriture ${f.path} : ${e.message}`);
                  if (typeof window.aiWorkspaceLog === 'function') window.aiWorkspaceLog(`Échec écriture ${f.path} : ${e.message}`, 'err');
                }
              }
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
