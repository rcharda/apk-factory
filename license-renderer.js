// license-renderer.js
// Flow : clé de licence (10 caractères) -> vérification directe dans Supabase -> accès
// Achat : plans -> paiement FedaPay -> une clé est générée automatiquement et liée
// au paiement -> polling jusqu'à confirmation -> accès.
//
// L'ancien système (email + mot de passe + Supabase Auth) est entièrement retiré.
// Il n'y a plus de notion de "compte" : une licence = une clé, point.

const SUPABASE_URL = "https://yvcdadenofftnbljutwk.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inl2Y2RhZGVub2ZmdG5ibGp1dHdrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI4NzQ0ODIsImV4cCI6MjA4ODQ1MDQ4Mn0.xqJzLpQszFmph599FBIvdE7NF88_i-JkABG-aSrAndE";

let deviceId = null;            // identifiant unique de CET appareil (généré une fois, côté main process)
let currentLicenseKey = null;   // clé actuellement en cours de vérification/activation
let selectedPlanId = null;
let pollInterval = null;
let lastPaymentUrl = null;

// ── fetch() avec timeout obligatoire ────────────────────────────────────────
// AUCUN fetch nu dans ce fichier : sans timeout, un réseau qui bloque en
// silence (pare-feu d'entreprise, Wi-Fi captif, DNS qui ne répond jamais...)
// laisse la promesse en attente indéfiniment — l'écran reste alors figé sur
// "Vérification de la licence…" pour toujours, sans aucune erreur affichée.
// Avec AbortController, on force un échec propre après quelques secondes,
// ce qui retombe sur les messages d'erreur déjà prévus ("connexion instable",
// "impossible de vérifier"...) au lieu d'un blocage muet.
function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(timer));
}

// ── Format de la clé de licence ─────────────────────────────────────────────
// 10 caractères, alphabet réduit pour éviter les confusions à la recopie
// (pas de 0/O, pas de 1/I/L). Affichée à l'écran avec un tiret au milieu
// ("ABCDE-FGHJK") mais TOUJOURS stockée/comparée sans le tiret.
const KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
const KEY_LENGTH = 10;

function generateLicenseKey() {
  let key = "";
  for (let i = 0; i < KEY_LENGTH; i++) {
    key += KEY_ALPHABET[Math.floor(Math.random() * KEY_ALPHABET.length)];
  }
  return key;
}
function normalizeKey(raw) {
  return (raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, KEY_LENGTH);
}
function formatKeyForDisplay(key) {
  if (!key) return "";
  return key.length > 5 ? key.slice(0, 5) + "-" + key.slice(5) : key;
}

// ── Modal de confirmation maison (remplace window.confirm) ─────────────────
// window.confirm() est une boîte de dialogue native et SYNCHRONE. Dans une
// fenêtre Electron sans cadre (frame:false / -webkit-app-region:drag, comme
// ici), elle peut ne pas s'afficher, s'afficher hors focus, ou être bloquée
// par la config du BrowserWindow — d'où le "je ne vois pas le popup". Ce
// modal HTML est rendu à l'intérieur de la fenêtre elle-même : il s'affiche
// donc toujours, quelle que soit la config Electron.
function showConfirmModal(message) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("modal-device");
    const textEl = document.getElementById("modal-device-text");
    const yesBtn = document.getElementById("modal-device-yes");
    const noBtn = document.getElementById("modal-device-no");

    if (!overlay || !textEl || !yesBtn || !noBtn) {
      resolve(window.confirm(message));
      return;
    }

    textEl.textContent = message;

    function cleanup(result) {
      overlay.classList.remove("active");
      yesBtn.removeEventListener("click", onYes);
      noBtn.removeEventListener("click", onNo);
      resolve(result);
    }
    function onYes() { cleanup(true); }
    function onNo() { cleanup(false); }

    yesBtn.addEventListener("click", onYes);
    noBtn.addEventListener("click", onNo);
    overlay.classList.add("active");
  });
}

// IP publique de CET appareil, à titre INFORMATIF uniquement (support/audit
// dans Supabase). Ne sert JAMAIS de critère de blocage — le seul critère de
// verrouillage reste device_id.
async function getPublicIp() {
  try {
    const r = await fetchWithTimeout("https://api.ipify.org?format=json", {}, 5000);
    if (!r.ok) return null;
    const d = await r.json();
    return d.ip || null;
  } catch (e) { return null; }
}

// ── Licence "1 appareil à la fois" ──────────────────────────────────────────
// Nécessite 2 colonnes sur la table apk_factory_licenses : device_id (text,
// nullable) et device_bound_at (timestamptz, nullable), + une policy RLS qui
// autorise l'UPDATE de ces 2 colonnes via la clé anon (aucun JWT utilisateur
// n'existe plus dans ce système, tout se fait avec la clé anon).
async function ensureDeviceBinding(license, { silent }) {
  const alreadyFree = !license.device_id || license.device_id === deviceId;
  if (alreadyFree) {
    try {
      const ip = await getPublicIp();
      const res = await supabaseFetch(`/rest/v1/rpc/apk_factory_bind_device`, {
        method: "POST",
        body: JSON.stringify({ p_license_id: license.id, p_device_id: deviceId, p_ip: ip }),
      });
      if (!res.ok) console.error("[device-binding] RPC bind_device refusé par Supabase :", res.status, await res.text().catch(() => ""));
    } catch (e) {
      console.error("[device-binding] échec réseau du bind (best-effort, accès non bloqué) :", e);
    }
    return true;
  }

  // Un autre appareil est déjà lié à cette licence.
  if (silent) return false; // pas de popup lors d'une reconnexion automatique silencieuse

  const confirmed = await showConfirmModal(
    "Cette licence est déjà utilisée sur un autre appareil.\n\n" +
    "Elle n'est valable que sur un seul appareil à la fois : continuer ici déconnectera automatiquement l'autre appareil.\n\n" +
    "Continuer sur CET appareil ?"
  );
  if (!confirmed) return false;

  try {
    const ip = await getPublicIp();
    const res = await supabaseFetch(`/rest/v1/rpc/apk_factory_bind_device`, {
      method: "POST",
      body: JSON.stringify({ p_license_id: license.id, p_device_id: deviceId, p_ip: ip }),
    });
    if (!res.ok) console.error("[device-binding] RPC bind_device refusé par Supabase :", res.status, await res.text().catch(() => ""));
  } catch (e) {
    console.error("[device-binding] échec réseau du bind lors du transfert d'appareil :", e);
  }
  return true;
}

// ── Démarrage : tente une reconnexion silencieuse si une clé est déjà
// enregistrée localement (l'utilisateur ne doit pas retaper sa clé à chaque
// ouverture de l'app). En cas d'échec (clé révoquée/expirée, appareil repris
// ailleurs...), on retombe simplement sur l'écran de saisie de clé, sans
// message d'erreur intrusif.
(async function bootAutoLogin() {
  try {
    deviceId = await window.electronAPI.getDeviceId();
  } catch (e) { deviceId = null; }

  let stored = null;
  try { stored = await window.electronAPI.getStoredSession(); } catch (e) {}

  if (!stored || !stored.license_key) {
    showStep("step-key");
    return;
  }

  try {
    const { license, uncertain } = await fetchLicenseByKey(stored.license_key);

    if (uncertain) {
      // Impossible de vérifier (réseau/Supabase indisponible) : on bloque
      // l'accès par sécurité, MAIS on ne supprime PAS la session locale —
      // elle pourra reservir dès que la connexion sera rétablie.
      showStep("step-key");
      showMsg("msg-key", "Impossible de vérifier ta licence (connexion indisponible). Réessaie, ou entre ta clé.", "error");
      return;
    }

    const state = evaluateLicense(license);
    if (state.status !== "active") throw new Error("licence introuvable ou inactive");

    const bound = await ensureDeviceBinding(license, { silent: true });
    if (!bound) throw new Error("appareil non lié (utilisé ailleurs)");

    currentLicenseKey = stored.license_key;
    await window.electronAPI.saveSession({ license_key: currentLicenseKey });
    window.electronAPI.licenseValid(currentLicenseKey, license.expires_at);
  } catch (e) {
    try { await window.electronAPI.clearSession(); } catch (e2) {}
    showStep("step-key");
  }
})();

function showStep(id) {
  document.querySelectorAll(".step").forEach(el => el.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}

function showMsg(elId, text, type) {
  const el = document.getElementById(elId);
  el.textContent = text;
  el.className = "msg " + type;
  el.style.display = "block";
}

function hideMsg(elId) {
  const el = document.getElementById(elId);
  el.style.display = "none";
}

function setBtnLoading(btn, loadingText) {
  btn.dataset.label = btn.dataset.label || btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>${loadingText}`;
}

function resetBtn(btn) {
  btn.disabled = false;
  btn.textContent = btn.dataset.label || btn.textContent;
}

// Toutes les requêtes Supabase passent par la clé anon : il n'y a plus de
// jeton utilisateur (pas de compte, pas de Supabase Auth).
async function supabaseFetch(path, options = {}) {
  return fetchWithTimeout(`${SUPABASE_URL}${path}`, {
    ...options,
    headers: {
      "apikey": SUPABASE_ANON_KEY,
      "Authorization": `Bearer ${SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  }, 10000);
}

// ── Récupère une licence par sa clé, quel que soit son statut ──────────────
// IMPORTANT : une policy RLS/PostgREST peut renvoyer 200 OK avec un tableau
// vide lors d'un blip transitoire (cache, race sur le JWT, etc.) — ce n'est
// PAS la même chose qu'une clé qui n'existe vraiment pas. On distingue les
// deux avec un retry immédiat + un statut "uncertain" dédié, pour ne jamais
// afficher "clé invalide" ni couper une session sur un simple accroc réseau.
async function queryLicenseByKeyOnce(key) {
  const res = await supabaseFetch(
    `/rest/v1/apk_factory_licenses?license_key=eq.${encodeURIComponent(key)}&limit=1`
  );
  if (!res.ok) return { networkError: true };
  const data = await res.json();
  return { rows: data };
}

async function fetchLicenseByKey(key) {
  if (!key) return { license: null, uncertain: false };
  try {
    let result = await queryLicenseByKeyOnce(key);
    if (result.networkError) return { license: null, uncertain: true };

    if (!result.rows.length) {
      // Retry immédiat avant de conclure quoi que ce soit : absorbe la
      // plupart des blips de cache PostgREST à lui seul.
      await new Promise((r) => setTimeout(r, 700));
      result = await queryLicenseByKeyOnce(key);
      if (result.networkError) return { license: null, uncertain: true };
    }

    if (!result.rows.length) {
      // Toujours vide après retry : on ne peut pas encore être certain à
      // 100%, mais on a fait le nécessaire pour absorber un blip ponctuel.
      // On remonte "uncertain" pour laisser l'appelant décider (ex: ne pas
      // clearSession sur un simple échec d'auto-login).
      return { license: null, uncertain: true };
    }

    return { license: result.rows[0], uncertain: false };
  } catch (e) {
    return { license: null, uncertain: true };
  }
}

// Interprète le statut d'une ligne de licence renvoyée par Supabase.
function evaluateLicense(license) {
  if (!license) return { status: "not_found" };
  if (license.status === "pending") return { status: "pending" };
  if (license.status !== "active") return { status: "inactive" };
  if (license.expires_at && new Date(license.expires_at) < new Date()) return { status: "expired" };
  return { status: "active" };
}

// ── Étape : saisie de la clé de licence ─────────────────────────────────────
const keyInput = document.getElementById("input-license-key");
if (keyInput) {
  keyInput.addEventListener("input", (e) => {
    const raw = normalizeKey(e.target.value);
    e.target.value = formatKeyForDisplay(raw);
  });
  keyInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("btn-activate-key").click();
  });
}

document.getElementById("btn-activate-key").addEventListener("click", async () => {
  const key = normalizeKey(document.getElementById("input-license-key").value);
  hideMsg("msg-key");

  if (key.length !== KEY_LENGTH) {
    showMsg("msg-key", `La clé doit faire ${KEY_LENGTH} caractères.`, "error");
    return;
  }

  const btn = document.getElementById("btn-activate-key");
  setBtnLoading(btn, "Vérification…");

  try {
    const { license, uncertain } = await fetchLicenseByKey(key);

    if (uncertain) {
      showMsg("msg-key", "Connexion instable, impossible de vérifier la clé. Réessaie dans un instant.", "error");
      resetBtn(btn);
      return;
    }

    const state = evaluateLicense(license);

    if (state.status === "not_found") {
      showMsg("msg-key", "Clé invalide. Vérifie que tu l'as bien recopiée, ou achète un abonnement ci-dessous.", "error");
      resetBtn(btn);
      return;
    }
    if (state.status === "pending") {
      showMsg("msg-key", "Paiement en attente de confirmation pour cette clé. Réessaie dans quelques minutes ou contacte le support.", "info");
      resetBtn(btn);
      return;
    }
    if (state.status === "expired") {
      showMsg("msg-key", "Cette clé a expiré. Achète un nouvel abonnement ci-dessous pour la renouveler.", "error");
      resetBtn(btn);
      return;
    }
    if (state.status === "inactive") {
      showMsg("msg-key", "Cette clé n'est plus active. Contacte le support.", "error");
      resetBtn(btn);
      return;
    }

    const bound = await ensureDeviceBinding(license, { silent: false });
    if (!bound) {
      showMsg("msg-key", "Activation annulée : un autre appareil reste actif sur cette licence.", "error");
      resetBtn(btn);
      return;
    }

    currentLicenseKey = key;
    try { await window.electronAPI.saveSession({ license_key: key }); } catch (e) {}

    showMsg("msg-key", "Licence activée ! Lancement de l'application…", "success");
    setTimeout(() => {
      window.electronAPI.licenseValid(key, license.expires_at);
    }, 800);
  } catch (err) {
    showMsg("msg-key", "Erreur réseau. Réessaie.", "error");
    resetBtn(btn);
  }
});

document.getElementById("link-go-plans").addEventListener("click", async () => {
  hideMsg("msg-key");
  await loadPlans();
  showStep("step-plans");
});

document.getElementById("btn-back-key").addEventListener("click", () => {
  hideMsg("msg-plans");
  showStep("step-key");
});

// ── Charger les plans publics ────────────────────────────────────────────────
async function loadPlans() {
  const list = document.getElementById("plans-list");
  list.innerHTML = '<div style="text-align:center;color:var(--muted);font-size:12px;padding:20px 0;">Chargement des tarifs…</div>';

  try {
    const res = await supabaseFetch(`/rest/v1/apk_factory_plans?is_active=eq.true&order=price_fcfa.asc`);
    const plans = await res.json();

    if (!plans.length) {
      list.innerHTML = '<div style="text-align:center;color:var(--muted);font-size:12px;padding:20px 0;">Aucun plan disponible. Contacte le support.</div>';
      return;
    }

    list.innerHTML = "";
    plans.forEach(plan => {
      const div = document.createElement("div");
      div.className = "plan";
      div.dataset.planId = plan.id;
      div.innerHTML = `
        <div>
          <div class="plan-name">${plan.name}</div>
          <div class="plan-duration">${plan.duration_days} jours</div>
        </div>
        <div class="plan-price">${plan.price_fcfa.toLocaleString("fr-FR")} FCFA</div>
      `;
      div.addEventListener("click", () => {
        document.querySelectorAll(".plan").forEach(p => p.classList.remove("selected"));
        div.classList.add("selected");
        selectedPlanId = plan.id;
        document.getElementById("btn-pay").disabled = false;
      });
      list.appendChild(div);
    });
  } catch (err) {
    list.innerHTML = '<div style="text-align:center;color:var(--red);font-size:12px;padding:20px 0;">Erreur de chargement des tarifs.</div>';
  }
}

// ── Paiement FedaPay ──────────────────────────────────────────────────────────
// 1) On génère une clé de licence côté client et on crée une ligne "pending"
//    dans Supabase (nécessite une policy RLS d'INSERT anonyme limitée aux
//    lignes status='pending' — voir notes SQL fournies séparément).
// 2) On demande la création du paiement à l'edge function create-payment, en
//    lui passant CETTE clé (plus d'email/user_id : l'edge function doit être
//    mise à jour côté Supabase pour accepter { license_key, plan_id } et
//    activer la ligne correspondante après confirmation FedaPay, au lieu de
//    chercher par email).
// 3) On affiche la clé à l'utilisateur (au cas où il ferme l'app) et on
//    poll jusqu'à ce que le statut passe à "active".
document.getElementById("btn-pay").addEventListener("click", async () => {
  hideMsg("msg-plans");

  const selectedEl = document.querySelector(".plan.selected");
  const planId = selectedEl ? selectedEl.dataset.planId : selectedPlanId;

  if (!planId) {
    showMsg("msg-plans", "Sélectionne d'abord une formule.", "error");
    return;
  }

  const btn = document.getElementById("btn-pay");
  setBtnLoading(btn, "Préparation…");

  try {
    // 1) Génère une clé unique et crée la licence "pending"
    let newKey = null;
    for (let attempt = 0; attempt < 5 && !newKey; attempt++) {
      const candidate = generateLicenseKey();
      const res = await supabaseFetch(`/rest/v1/apk_factory_licenses`, {
        method: "POST",
        headers: { "Prefer": "return=representation" },
        body: JSON.stringify({ license_key: candidate, plan_id: planId, status: "pending" }),
      });
      if (res.status === 201) {
        newKey = candidate;
      } else if (res.status !== 409) {
        const errText = await res.text().catch(() => "");
        throw new Error(`Impossible de préparer la licence (${res.status}). ${errText}`);
      }
      // 409 = conflit sur license_key (déjà pris) -> on retente avec une nouvelle clé
    }
    if (!newKey) throw new Error("Impossible de générer une clé unique, réessaie.");

    // 2) Crée le paiement FedaPay lié à cette clé
    const res = await supabaseFetch(`/functions/v1/create-payment`, {
      method: "POST",
      body: JSON.stringify({ license_key: newKey, plan_id: planId }),
    });
    const data = await res.json();
    console.log("[create-payment] réponse:", res.status, data);

    if (!res.ok || !data.payment_url) {
      const details = data.details ? ` (${JSON.stringify(data.details)})` : "";
      showMsg("msg-plans", (data.error || "Erreur lors de la création du paiement.") + details, "error");
      resetBtn(btn);
      return;
    }

    lastPaymentUrl = data.payment_url;
    currentLicenseKey = newKey;
    document.getElementById("waiting-key-value").textContent = formatKeyForDisplay(newKey);
    window.electronAPI.openExternal(lastPaymentUrl);
    showStep("step-waiting");
    startPolling(newKey);
  } catch (err) {
    console.error("[btn-pay] Exception:", err);
    showMsg("msg-plans", "Erreur : " + (err?.message || err), "error");
    resetBtn(btn);
  }
});

// ── Polling : attendre l'activation de la licence après paiement ───────────
function startPolling(key) {
  if (pollInterval) clearInterval(pollInterval);

  pollInterval = setInterval(async () => {
    try {
      const { license, uncertain } = await fetchLicenseByKey(key);
      if (uncertain || !license) return; // on réessaie silencieusement au prochain tick
      const state = evaluateLicense(license);
      if (state.status === "active") {
        clearInterval(pollInterval);
        const bound = await ensureDeviceBinding(license, { silent: false });
        if (!bound) {
          showMsg("msg-waiting", "Connexion annulée : un autre appareil reste actif sur cette licence.", "error");
          return;
        }
        try { await window.electronAPI.saveSession({ license_key: key }); } catch (e) {}
        showMsg("msg-waiting", "Paiement confirmé ! Lancement de l'application…", "success");
        setTimeout(() => {
          window.electronAPI.licenseValid(key, license.expires_at);
        }, 1200);
      }
    } catch (err) {
      // silencieux, on réessaie au prochain tick
    }
  }, 4000);
}

document.getElementById("btn-reopen-payment").addEventListener("click", () => {
  if (lastPaymentUrl) window.electronAPI.openExternal(lastPaymentUrl);
});

document.getElementById("btn-copy-waiting-key").addEventListener("click", () => {
  if (!currentLicenseKey) return;
  navigator.clipboard?.writeText(currentLicenseKey).catch(() => {});
  const el = document.getElementById("btn-copy-waiting-key");
  const original = el.textContent;
  el.textContent = "Copié !";
  setTimeout(() => { el.textContent = original; }, 1500);
});

document.getElementById("btn-cancel-waiting").addEventListener("click", () => {
  if (pollInterval) clearInterval(pollInterval);
  resetBtn(document.getElementById("btn-pay"));
  showStep("step-plans");
});

document.getElementById("link-support").addEventListener("click", () => {
  window.electronAPI.openExternal("https://wa.me/22900000000"); // à remplacer par ton vrai contact
});
