// license-renderer.js
// Flow : email -> check-email (existe ?) -> login ou signup (Supabase Auth)
// -> si licence active : accès direct -> sinon : plans -> paiement FedaPay -> polling

const SUPABASE_URL = "https://yvcdadenofftnbljutwk.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inl2Y2RhZGVub2ZmdG5ibGp1dHdrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI4NzQ0ODIsImV4cCI6MjA4ODQ1MDQ4Mn0.xqJzLpQszFmph599FBIvdE7NF88_i-JkABG-aSrAndE";

let currentEmail = "";
let currentUserId = null;
let accessToken = null;
let selectedPlanId = null;
let pollInterval = null;
let lastPaymentUrl = null;

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

async function supabaseFetch(path, options = {}) {
  return fetch(`${SUPABASE_URL}${path}`, {
    ...options,
    headers: {
      "apikey": SUPABASE_ANON_KEY,
      "Authorization": `Bearer ${accessToken || SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
}

// ── Vérifier la licence active d'un utilisateur ─────────────────────────────
async function checkActiveLicense(email) {
  const res = await supabaseFetch(
    `/rest/v1/apk_factory_licenses?email=eq.${encodeURIComponent(email)}&status=eq.active&order=expires_at.desc&limit=1`
  );
  if (!res.ok) return null;
  const data = await res.json();
  if (!data.length) return null;

  const license = data[0];
  if (license.expires_at && new Date(license.expires_at) < new Date()) return null;
  return license;
}

// ── Étape 1 : email -> vérifier si compte existant ──────────────────────────
document.getElementById("btn-check-email").addEventListener("click", async () => {
  const email = document.getElementById("input-email").value.trim().toLowerCase();
  hideMsg("msg-email");

  if (!email || !email.includes("@")) {
    showMsg("msg-email", "Entre un email valide.", "error");
    return;
  }

  const btn = document.getElementById("btn-check-email");
  setBtnLoading(btn, "Vérification…");

  try {
    currentEmail = email;

    const res = await supabaseFetch(`/functions/v1/check-email`, {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    const data = await res.json();

    if (!res.ok) {
      showMsg("msg-email", data.error || "Erreur de vérification.", "error");
      resetBtn(btn);
      return;
    }

    if (data.exists) {
      document.getElementById("login-sub").textContent =
        `Connecte-toi avec ${email} pour continuer.`;
      showStep("step-login");
    } else {
      showStep("step-signup");
    }
  } catch (err) {
    showMsg("msg-email", "Erreur de connexion. Vérifie ta connexion internet.", "error");
  } finally {
    resetBtn(btn);
  }
});

document.getElementById("btn-back-email-1").addEventListener("click", () => {
  showStep("step-email");
});
document.getElementById("btn-back-email-2").addEventListener("click", () => {
  showStep("step-email");
});

// ── Connexion (compte existant) ──────────────────────────────────────────────
document.getElementById("btn-login").addEventListener("click", async () => {
  const password = document.getElementById("input-password-login").value;
  hideMsg("msg-login");

  if (!password) {
    showMsg("msg-login", "Entre ton mot de passe.", "error");
    return;
  }

  const btn = document.getElementById("btn-login");
  setBtnLoading(btn, "Connexion…");

  try {
    const res = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
      method: "POST",
      headers: { "apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ email: currentEmail, password }),
    });
    const data = await res.json();

    if (!res.ok) {
      showMsg("msg-login", "Email ou mot de passe incorrect.", "error");
      resetBtn(btn);
      return;
    }

    accessToken = data.access_token;
    currentUserId = data.user.id;

    await afterAuthSuccess();
  } catch (err) {
    showMsg("msg-login", "Erreur réseau. Réessaie.", "error");
    resetBtn(btn);
  }
});

// ── Création de compte (nouvel email) ────────────────────────────────────────
document.getElementById("btn-signup").addEventListener("click", async () => {
  const password = document.getElementById("input-password-signup").value;
  hideMsg("msg-signup");

  if (!password || password.length < 6) {
    showMsg("msg-signup", "Le mot de passe doit faire au moins 6 caractères.", "error");
    return;
  }

  const btn = document.getElementById("btn-signup");
  setBtnLoading(btn, "Création du compte…");

  try {
    const res = await fetch(`${SUPABASE_URL}/auth/v1/signup`, {
      method: "POST",
      headers: { "apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ email: currentEmail, password }),
    });
    const data = await res.json();

    if (!res.ok) {
      showMsg("msg-signup", data.msg || data.error_description || "Erreur lors de la création du compte.", "error");
      resetBtn(btn);
      return;
    }

    // Avec confirmation email désactivée (par défaut sur Supabase Auth pour
    // un nouveau projet sauf si activée manuellement), access_token est déjà présent.
    if (data.access_token) {
      accessToken = data.access_token;
      currentUserId = data.user.id;
      await afterAuthSuccess();
    } else {
      // Confirmation email activée -> il faut se reconnecter après confirmation
      showMsg("msg-signup", "Compte créé. Vérifie ta boîte mail pour confirmer, puis reconnecte-toi.", "success");
      setTimeout(() => showStep("step-email"), 2500);
      resetBtn(btn);
    }
  } catch (err) {
    showMsg("msg-signup", "Erreur réseau. Réessaie.", "error");
    resetBtn(btn);
  }
});

// ── Après connexion/inscription réussie : licence active ou plans ───────────
async function afterAuthSuccess() {
  document.getElementById("user-pill-email").textContent = currentEmail;

  const license = await checkActiveLicense(currentEmail);
  if (license) {
    window.electronAPI.licenseValid(currentEmail, license.expires_at);
    return;
  }

  document.getElementById("plans-sub").textContent =
    "Aucune licence active sur ce compte. Sélectionne une formule pour continuer.";
  await loadPlans();
  showStep("step-plans");
}

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

document.getElementById("btn-logout").addEventListener("click", () => {
  accessToken = null;
  currentUserId = null;
  selectedPlanId = null;
  document.getElementById("input-email").value = "";
  document.getElementById("input-password-login").value = "";
  document.getElementById("input-password-signup").value = "";
  showStep("step-email");
});

// ── Paiement FedaPay ──────────────────────────────────────────────────────────
document.getElementById("btn-pay").addEventListener("click", async () => {
  hideMsg("msg-plans");

  // Relit le plan sélectionné directement dans le DOM (sécurité supplémentaire
  // au cas où la variable selectedPlanId se serait perdue).
  const selectedEl = document.querySelector(".plan.selected");
  const planId = selectedEl ? selectedEl.dataset.planId : selectedPlanId;

  if (!planId) {
    showMsg("msg-plans", "Sélectionne d'abord une formule.", "error");
    return;
  }
  if (!currentEmail) {
    showMsg("msg-plans", "Erreur interne : email manquant. Reconnecte-toi.", "error");
    return;
  }

  const btn = document.getElementById("btn-pay");
  setBtnLoading(btn, "Création du paiement…");

  try {
    const res = await supabaseFetch(`/functions/v1/create-payment`, {
      method: "POST",
      body: JSON.stringify({ email: currentEmail, user_id: currentUserId, plan_id: planId }),
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
    window.electronAPI.openExternal(lastPaymentUrl);
    showStep("step-waiting");
    startPolling();
  } catch (err) {
    console.error("[btn-pay] Exception:", err);
    showMsg("msg-plans", "Erreur réseau lors du paiement. Détail : " + (err?.message || err), "error");
    resetBtn(btn);
  }
});

// ── Polling : attendre l'activation de la licence après paiement ───────────
function startPolling() {
  if (pollInterval) clearInterval(pollInterval);

  pollInterval = setInterval(async () => {
    try {
      const license = await checkActiveLicense(currentEmail);
      if (license) {
        clearInterval(pollInterval);
        showMsg("msg-waiting", "Paiement confirmé ! Lancement de l'application…", "success");
        setTimeout(() => {
          window.electronAPI.licenseValid(currentEmail, license.expires_at);
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

document.getElementById("btn-cancel-waiting").addEventListener("click", () => {
  if (pollInterval) clearInterval(pollInterval);
  resetBtn(document.getElementById("btn-pay"));
  showStep("step-plans");
});

document.getElementById("link-support").addEventListener("click", () => {
  window.electronAPI.openExternal("https://wa.me/22900000000"); // à remplacer par ton vrai contact
});
