#!/usr/bin/env python3
"""
APK Factory v3 - Serveur local
Génère des APK WebView de ZÉRO sans aucun template.
Lance avec: python server.py
"""

import sys

# ── Fix encodage Windows (cp1252 → utf-8) ────────────────────────────────────
# Python 3.14 sur Windows utilise cp1252 par défaut pour stdout/stderr,
# ce qui fait planter les print() contenant des caractères unicode (╔═╗ etc.)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import base64
import http.server
import io
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── Génération d'un PNG valide en pur Python (sans Pillow) ───────────────────
def _make_solid_png(width, height, r=33, g=150, b=243, a=255):
    """Génère un PNG RGBA valide avec couleur unie — aucune dépendance."""
    import struct as _s, zlib as _z
    sig = b'\x89PNG\r\n\x1a\n'
    def _chunk(name, data):
        return _s.pack('>I', len(data)) + name + data + _s.pack('>I', _z.crc32(name + data) & 0xFFFFFFFF)
    ihdr = _s.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    row  = b'\x00' + bytes([r, g, b, a]) * width
    return sig + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', _z.compress(row * height, 9)) + _chunk(b'IEND', b'')

# Icône 48×48 bleu Material (#2196F3) — PNG rigoureusement valide (CRC corrects)
DEFAULT_ICON_BYTES = _make_solid_png(48, 48, 33, 150, 243, 255)

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

BASE_DIR = Path(__file__).parent
TOOLS_DIR = BASE_DIR / "tools"
SDK_DIR   = TOOLS_DIR / "android-sdk"
WORK_DIR  = BASE_DIR / "workspace"
OUTPUT_DIR = BASE_DIR / "output"
BUGLOG_FILE = BASE_DIR / "tools" / "bug_log.json"

WORK_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
TOOLS_DIR.mkdir(exist_ok=True)

TEXT_EXTENSIONS = {
    '.smali', '.xml', '.java', '.kt', '.json', '.txt', '.properties',
    '.MF', '.SF', '.gradle', '.pro', '.cfg', '.yml', '.yaml', '.md',
    '.html', '.htm', '.js', '.css', '.aidl', '.pem'
}
MAX_TEXT_SIZE = 4_000_000


# =============================================================
# LOGGING
# =============================================================
class OpLogger:
    def __init__(self):
        self.lines  = []
        self._status = "idle"
        self.result_file = None
        self.session = None
        self._last_logged_error_line = None

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value
        # Alimente automatiquement le journal de bugs persistant dès qu'une
        # opération de build échoue — capture tous les chemins d'erreur sans
        # avoir à patcher individuellement chaque "logger.status = 'error'"
        # dispersé dans le code (création keystore, recompile, build scratch,
        # build legacy, signature...).
        if value == "error":
            try:
                # On reprend la dernière ligne ❌ comme titre si possible —
                # plus parlant qu'un message générique "Échec". On retire le
                # préfixe "[HH:MM:SS] " déjà présent dans la ligne, puisque
                # l'entrée du journal a son propre champ "ts".
                err_lines = [l for l in self.lines if "❌" in l]
                if err_lines:
                    title = re.sub(r'^\[\d{2}:\d{2}:\d{2}\]\s*', '', err_lines[-1])
                    title = title.lstrip("❌ ").strip()
                else:
                    title = "Échec d'une opération (build/signature)"
                detail = "\n".join(self.lines[-15:])
                buglog_add("error", title, detail, source="system")
            except Exception:
                pass

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.lines.append(entry)
        print(entry)
        # Capture aussi les avertissements explicites (⚠) émis pendant un
        # build qui se termine "done" — ex: MINEUR #7 (URL vide → fallback
        # silencieux). Ces cas ne mettent jamais status="error" puisque le
        # build réussit techniquement, mais méritent l'indicateur jaune.
        if "⚠" in msg and msg != self._last_logged_error_line:
            self._last_logged_error_line = msg
            try:
                buglog_add("warning", msg.lstrip("⚠ ").strip()[:200], msg, source="system")
            except Exception:
                pass

OPS = {"legacy": OpLogger()}
CURRENT_SESSION = None


# =============================================================
# JOURNAL DE BUGS / NOTES — persistant sur disque (tools/bug_log.json)
# =============================================================
# Permet à l'UI d'afficher un indicateur de santé (vert/jaune/rouge) +
# un bloc-notes qui garde un historique des erreurs/avertissements
# rencontrés ET des bugs signalés manuellement par l'utilisateur via
# le formulaire de l'interface — séparé des logs de build (qui eux sont
# éphémères et propres à une seule opération).
BUGLOG_MAX_ENTRIES = 500
_buglog_lock = threading.Lock()

def _buglog_read():
    if not BUGLOG_FILE.exists():
        return []
    try:
        data = json.loads(BUGLOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _buglog_write(entries):
    try:
        BUGLOG_FILE.write_text(
            json.dumps(entries[-BUGLOG_MAX_ENTRIES:], ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[buglog] échec écriture: {e}")

def buglog_add(severity, title, detail="", source="system"):
    """
    severity: 'error' (rouge) | 'warning' (jaune) | 'info' (neutre)
    source:   'system' (détecté automatiquement) | 'user' (signalé via le formulaire)
    """
    with _buglog_lock:
        entries = _buglog_read()
        entry = {
            "id": new_session_id(),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "severity": severity if severity in ("error", "warning", "info") else "info",
            "title": (title or "")[:300],
            "detail": (detail or "")[:5000],
            "source": source if source in ("user", "system") else "system",
            "resolved": False,
        }
        entries.append(entry)
        _buglog_write(entries)
        return entry

def buglog_compute_health():
    """
    Agrège les entrées NON résolues pour déterminer l'état de santé global :
    - 'error'   (rouge)  s'il existe au moins une entrée error non résolue
    - 'warning' (jaune)  sinon, s'il existe au moins une entrée warning non résolue
    - 'ok'      (vert)   sinon
    Retourne aussi un message texte résumant la raison principale.
    """
    entries = [e for e in _buglog_read() if not e.get("resolved")]
    errors   = [e for e in entries if e.get("severity") == "error"]
    warnings = [e for e in entries if e.get("severity") == "warning"]
    if errors:
        last = errors[-1]
        return {
            "state": "error",
            "message": f"{len(errors)} erreur(s) active(s) — dernière : {last['title']}",
            "count_errors": len(errors),
            "count_warnings": len(warnings),
        }
    if warnings:
        last = warnings[-1]
        return {
            "state": "warning",
            "message": f"{len(warnings)} avertissement(s) — dernier : {last['title']}",
            "count_errors": 0,
            "count_warnings": len(warnings),
        }
    return {
        "state": "ok",
        "message": "Aucun problème connu — tout fonctionne normalement",
        "count_errors": 0,
        "count_warnings": 0,
    }

def get_op(token):
    if token not in OPS:
        OPS[token] = OpLogger()
    return OPS[token]


# =============================================================
# OUTILS SYSTÈME
# =============================================================
def run_cmd(cmd, logger, cwd=None, timeout=600):
    logger.log(f"$ {' '.join(str(c) for c in cmd)}")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.log("⚠ Timeout commande")
        return False
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    for line in (r.stderr or "").strip().split("\n"):
        if line.strip(): logger.log("⚠ " + line)
    return r.returncode == 0

def run_cmd_capture(cmd, logger, cwd=None, timeout=60):
    logger.log(f"$ {' '.join(str(c) for c in cmd)}")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, ""
    for line in (r.stderr or "").strip().split("\n"):
        if line.strip(): logger.log("⚠ " + line)
    return r.returncode == 0, (r.stdout or "")

def _candidates(name):
    yield TOOLS_DIR / name
    yield TOOLS_DIR / (name + ".jar")
    yield TOOLS_DIR / (name + ".bat")
    yield TOOLS_DIR / (name + ".exe")
    bt = SDK_DIR / "build-tools"
    if bt.exists():
        for v in sorted([d for d in bt.iterdir() if d.is_dir()], reverse=True):
            for n in (name, name + ".bat", name + ".exe"):
                yield v / n
    pt = SDK_DIR / "platform-tools"
    if pt.exists():
        for n in (name, name + ".bat", name + ".exe"):
            yield pt / n

def find_tool(name):
    for c in _candidates(name):
        if c.exists(): return str(c)
    return shutil.which(name)

def find_build_tools_version():
    bt = SDK_DIR / "build-tools"
    if not bt.exists(): return None
    versions = [d.name for d in bt.iterdir() if d.is_dir()]
    return sorted(versions, reverse=True)[0] if versions else None

def _pass_arg(password, tmp_dir, name):
    pwd_file = Path(tmp_dir) / name
    pwd_file.write_text(password, encoding="utf-8", newline="")
    return f"file:{pwd_file}", pwd_file

def _safe_b64(data, name):
    """Décode du base64 avec un message d'erreur clair si les données sont corrompues."""
    if not data: return None
    try:
        return base64.b64decode(data)
    except Exception as e:
        raise ValueError(f"Données {name} base64 invalides: {e}")



def _safe_extract_zip(zf, dest_dir, logger=None):
    """
    Extraction sécurisée d'un ZIP — protège contre le zip traversal
    (chemins en ../ qui sortiraient du dossier de destination).
    Filtre aussi les artefacts macOS (.DS_Store, __MACOSX) — BUG-M06.
    """
    dest_dir = Path(dest_dir).resolve()
    skipped = 0
    for member in zf.infolist():
        fn = member.filename
        # BUG-M06 — ignorer les artefacts macOS qui perturbent la détection d'index.html
        if fn.startswith("__MACOSX/") or fn.endswith("/.DS_Store") or fn == ".DS_Store":
            skipped += 1
            continue
        # Normaliser le chemin et détecter les traversals
        target = (dest_dir / fn).resolve()
        if not str(target).startswith(str(dest_dir)):
            if logger: logger.log(f"⚠ ZIP: chemin suspect ignoré: {fn}")
            skipped += 1
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src_f, open(target, 'wb') as dst_f:
                shutil.copyfileobj(src_f, dst_f)
    if skipped and logger:
        logger.log(f"⚠ ZIP: {skipped} entrée(s) ignorée(s) (macOS / traversal path)")

# =============================================================
# IMAGE
# =============================================================
def ensure_png_bytes(data, logger=None, label="image"):
    if not data: return data
    if data[:8] == PNG_MAGIC: return data
    if not PIL_AVAILABLE:
        raise RuntimeError(
            f"{label} n'est pas un PNG valide. Installe Pillow (pip install Pillow) "
            "pour la conversion automatique JPEG/WEBP → PNG."
        )
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        if logger: logger.log(f"🔄 {label}: converti automatiquement en PNG")
        return buf.getvalue()
    except Exception as e:
        raise RuntimeError(f"Impossible de convertir {label} en PNG: {e}")

def make_icon_png(src_bytes, size):
    """Redimensionne src_bytes (PNG/JPEG/WEBP) vers size×size PNG."""
    if not src_bytes: return _make_solid_png(size, size)
    if PIL_AVAILABLE:
        try:
            im = Image.open(io.BytesIO(src_bytes)).convert("RGBA").resize((size, size), Image.LANCZOS)
            buf = io.BytesIO(); im.save(buf, format="PNG"); return buf.getvalue()
        except Exception: pass
    # fallback sans Pillow: si deja PNG valide, retourner tel quel
    if src_bytes[:8] == PNG_MAGIC: return src_bytes
    # En dernier recours: icone unicolore valide
    return _make_solid_png(size, size)


# =============================================================
# GÉNÉRATION DU CODE SOURCE (smali + ressources + manifeste)
# — AUCUN TEMPLATE REQUIS —
# =============================================================

def _smali_string_escape(s):
    """
    Échappe une chaîne pour une littérale smali (const-string v, "...").
    Le format smali est proche de Java : \\, ", \n, \t, \r doivent être échappés,
    sinon apktool b échoue avec une erreur de parsing sur cette ligne.
    """
    return (str(s).replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


# Permissions Android "dangereuses" qui nécessitent une demande au runtime
# (API 23+) en plus de leur déclaration dans le manifest. Sans cette demande,
# le système refuse silencieusement l'accès même si la permission est cochée
# dans le builder et présente dans AndroidManifest.xml — cf. CRITIQUE #2 du
# rapport de bugs (getUserMedia()/navigator.geolocation échouent sans crash).
RUNTIME_DANGEROUS_PERMISSIONS = [
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.READ_MEDIA_IMAGES",
    "android.permission.READ_MEDIA_VIDEO",
    "android.permission.READ_MEDIA_AUDIO",
    "android.permission.READ_CONTACTS",
    "android.permission.WRITE_CONTACTS",
    "android.permission.GET_ACCOUNTS",
    "android.permission.CALL_PHONE",
    "android.permission.READ_PHONE_STATE",
    "android.permission.SEND_SMS",
    "android.permission.READ_SMS",
    "android.permission.RECEIVE_SMS",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.BLUETOOTH_CONNECT",
    "android.permission.BLUETOOTH_SCAN",
]


def _smali_main(package, url_or_asset, mode, orientation="unspecified", allow_file_access=None,
                 permissions=None):
    """
    Génère le smali de MainActivity — compatible Android 6 (API 23) → 16+.
    mode: 'url'   → loadUrl(url_or_asset)
          'asset' → loadUrl("file:///android_asset/index.html")
          'www'   → loadUrl("file:///android_asset/www/index.html")
    allow_file_access: si None, déduit du mode (True pour asset/www, False pour url)
    permissions: liste des permissions Android déclarées dans le manifest —
                 celles qui font partie de RUNTIME_DANGEROUS_PERMISSIONS seront
                 demandées au runtime via requestPermissions() avant le chargement
                 de l'URL (CRITIQUE #2 — sans ça CAMERA/RECORD_AUDIO/GPS restent
                 refusées en silence par la WebView même si cochées dans l'UI).
    """
    if allow_file_access is None:
        allow_file_access = (mode in ("asset", "www"))
    if mode == "url":
        load_url = url_or_asset or "https://example.com"
    elif mode == "www":
        load_url = "file:///android_asset/www/index.html"
    else:  # asset
        load_url = "file:///android_asset/index.html"

    load_url = _smali_string_escape(load_url)
    pkg_smali = package.replace(".", "/")

    permissions = permissions or []
    dangerous = [p for p in permissions if p in RUNTIME_DANGEROUS_PERMISSIONS]

    # -- Bloc de demande de permissions runtime (injecté dans onCreate, juste
    #    avant le chargement de l'URL) --
    if dangerous:
        # Construit le tableau String[] des permissions à demander.
        # IMPORTANT : const/4 n'encode que des littéraux de -8 à 7. Avec
        # jusqu'à 21 permissions dangereuses possibles (RUNTIME_DANGEROUS_
        # PERMISSIONS), la taille du tableau et certains indices dépassent
        # cette plage — on utilise donc const/16 (jusqu'à 32767) partout
        # pour ces valeurs, qui reste valide même pour de petits nombres.
        perm_array_lines = []
        n = len(dangerous)
        perm_array_lines.append(f"    const/16 v2, 0x{n:x}")
        perm_array_lines.append("    new-array v2, v2, [Ljava/lang/String;")
        for i, perm in enumerate(dangerous):
            perm_array_lines.append(f"    const/16 v3, 0x{i:x}")
            perm_array_lines.append(f'    const-string v4, "{perm}"')
            perm_array_lines.append("    aput-object v4, v2, v3")
        perm_array_block = "\n".join(perm_array_lines)

        permission_request_block = f'''
    # -- CRITIQUE #2 : demande runtime des permissions dangereuses (API 23+) --
    # Sans cette demande explicite, déclarer CAMERA/RECORD_AUDIO/ACCESS_FINE_LOCATION
    # dans le manifest ne suffit pas : Android les laisse en état "refusé" et la
    # WebView échoue silencieusement (getUserMedia/geolocation ne fonctionnent jamais).
    # On utilise Activity->requestPermissions(), la méthode NATIVE du SDK Android
    # (disponible directement depuis API 23, AUCUNE dépendance externe requise) —
    # et surtout PAS androidx.core.app.ActivityCompat, qui n'existe dans aucun
    # .jar de ce projet (volontairement sans appcompat/androidx, cf. _styles_xml)
    # et provoquerait un ClassNotFoundException au lancement si utilisée ici.
    sget v0, Landroid/os/Build$VERSION;->SDK_INT:I
    const/16 v1, 0x17
    if-lt v0, v1, :skip_runtime_perms

{perm_array_block}
    const/4 v3, 0x1
    invoke-virtual {{p0, v2, v3}}, Landroid/app/Activity;->requestPermissions([Ljava/lang/String;I)V

    :skip_runtime_perms
'''
    else:
        permission_request_block = ""

    # onBackPressed est déprécié Android 13+ (API 33) mais on le garde pour compat.
    # On utilise également shouldOverrideUrlLoading(WebView, WebResourceRequest) — API 24+
    # ET la version legacy (String) pour couvrir API <24.
    return f'''.class public L{pkg_smali}/MainActivity;
.super Landroid/app/Activity;
.source "MainActivity.java"

.field private wv:Landroid/webkit/WebView;

.method public constructor <init>()V
    .registers 1
    invoke-direct {{p0}}, Landroid/app/Activity;-><init>()V
    return-void
.end method

.method protected onCreate(Landroid/os/Bundle;)V
    .registers 7
    .param p1, "savedInstanceState"

    invoke-super {{p0, p1}}, Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V

    # -- Masquer le titre/ActionBar --
    const/4 v0, 0x1
    invoke-virtual {{p0, v0}}, Landroid/app/Activity;->requestWindowFeature(I)Z

    # -- Plein écran selon la version Android --
    # FLAG_FULLSCREEN (0x400) est déprécié depuis Android 11 (API 30) et n'a
    # plus aucun effet quand targetSdk >= 35 (edge-to-edge forcé par le
    # système). Sans ce correctif, le contenu de la WebView se superposait
    # à la barre de statut sur Android 15+ (rendu cassé, pas un crash mais
    # visuellement faux). On détecte donc la version au runtime :
    #   - API >= 30 : Window.setDecorFitsSystemWindows(false) — méthode
    #     moderne, gère correctement l'edge-to-edge sur Android 11 à 16+.
    #   - API < 30  : ancien FLAG_FULLSCREEN, qui fonctionne normalement
    #     sur ces versions plus anciennes et ne doit pas être retiré pour
    #     elles (sinon régression visuelle sur Android 6-10).
    sget v0, Landroid/os/Build$VERSION;->SDK_INT:I
    const/16 v1, 0x1e
    if-lt v0, v1, :use_legacy_fullscreen

    invoke-virtual {{p0}}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    const/4 v1, 0x0
    invoke-virtual {{v0, v1}}, Landroid/view/Window;->setDecorFitsSystemWindows(Z)V
    goto :fullscreen_done

    :use_legacy_fullscreen
    invoke-virtual {{p0}}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    const v1, 0x400
    invoke-virtual {{v0, v1}}, Landroid/view/Window;->addFlags(I)V

    :fullscreen_done
{permission_request_block}
    # -- Créer le WebView --
    new-instance v0, Landroid/webkit/WebView;
    invoke-direct {{v0, p0}}, Landroid/webkit/WebView;-><init>(Landroid/content/Context;)V
    iput-object v0, p0, L{pkg_smali}/MainActivity;->wv:Landroid/webkit/WebView;

    # -- WebSettings --
    invoke-virtual {{v0}}, Landroid/webkit/WebView;->getSettings()Landroid/webkit/WebSettings;
    move-result-object v1

    const/4 v2, 0x1
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setJavaScriptEnabled(Z)V
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setDomStorageEnabled(Z)V
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setBuiltInZoomControls(Z)V
    const/4 v3, 0x0
    invoke-virtual {{v1, v3}}, Landroid/webkit/WebSettings;->setDisplayZoomControls(Z)V

    # LOAD_DEFAULT = -1
    const/4 v2, -0x1
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setCacheMode(I)V

    # setMediaPlaybackRequiresUserGesture(false) — API 17+
    const/4 v2, 0x0
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setMediaPlaybackRequiresUserGesture(Z)V

    # setMixedContentMode(0 = MIXED_CONTENT_ALWAYS_ALLOW) — Android 5+ (API 21+)
    # v2 est déjà 0x0 ici — pas de const/4 supplémentaire nécessaire
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setMixedContentMode(I)V

{"" if not allow_file_access else """    # setAllowFileAccessFromFileURLs(true) — accès assets locaux (modes asset/www uniquement)
    # Inutile et déprécié Android 16+ en mode URL réseau — omis volontairement en mode url
    const/4 v2, 0x1
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setAllowFileAccessFromFileURLs(Z)V

    # setAllowUniversalAccessFromFileURLs(true)
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setAllowUniversalAccessFromFileURLs(Z)V
"""}
    # -- Activer la géolocalisation côté WebSettings (requis en complément de
    #    onGeolocationPermissionsShowPrompt pour que navigator.geolocation
    #    fonctionne dans la page chargée) — v1 contient encore le WebSettings ici.
    const/4 v2, 0x1
    invoke-virtual {{v1, v2}}, Landroid/webkit/WebSettings;->setGeolocationEnabled(Z)V

    # -- WebViewClient --
    new-instance v1, L{pkg_smali}/InternalWebViewClient;
    invoke-direct {{v1}}, L{pkg_smali}/InternalWebViewClient;-><init>()V
    invoke-virtual {{v0, v1}}, Landroid/webkit/WebView;->setWebViewClient(Landroid/webkit/WebViewClient;)V

    # -- WebChromeClient personnalisé --
    # CRITIQUE #2 : le WebChromeClient générique d'Android refuse en silence
    # toute demande de caméra/micro (getUserMedia) ou de géolocalisation
    # (navigator.geolocation) faite depuis le JS de la page. On utilise donc
    # notre propre sous-classe qui accorde la requête (si des permissions
    # dangereuses sont sélectionnées, l'utilisateur a déjà été sollicité par
    # la boîte de dialogue système Android via requestPermissions ci-dessus).
    new-instance v1, L{pkg_smali}/InternalWebChromeClient;
    invoke-direct {{v1}}, L{pkg_smali}/InternalWebChromeClient;-><init>()V
    invoke-virtual {{v0, v1}}, Landroid/webkit/WebView;->setWebChromeClient(Landroid/webkit/WebChromeClient;)V

    # -- Charger l'URL --
    const-string v1, "{load_url}"
    invoke-virtual {{v0, v1}}, Landroid/webkit/WebView;->loadUrl(Ljava/lang/String;)V

    # -- Afficher le WebView --
    invoke-virtual {{p0, v0}}, Landroid/app/Activity;->setContentView(Landroid/view/View;)V
    return-void
.end method

# onBackPressed conservé pour compatibilité <API33
.method public onBackPressed()V
    .registers 3
    iget-object v0, p0, L{pkg_smali}/MainActivity;->wv:Landroid/webkit/WebView;
    if-eqz v0, :super_back
    invoke-virtual {{v0}}, Landroid/webkit/WebView;->canGoBack()Z
    move-result v1
    if-eqz v1, :super_back
    invoke-virtual {{v0}}, Landroid/webkit/WebView;->goBack()V
    return-void
    :super_back
    invoke-super {{p0}}, Landroid/app/Activity;->onBackPressed()V
    return-void
.end method
'''


def _smali_webchromeclient(package):
    """
    Sous-classe de WebChromeClient qui accorde automatiquement les demandes
    de permission web (caméra/micro via getUserMedia, géolocalisation) —
    CRITIQUE #2. Sans cette classe, Android utilise le comportement par
    défaut de WebChromeClient qui REFUSE silencieusement ces demandes.
    """
    pkg_smali = package.replace(".", "/")
    return f'''.class public L{pkg_smali}/InternalWebChromeClient;
.super Landroid/webkit/WebChromeClient;
.source "InternalWebChromeClient.java"

.method public constructor <init>()V
    .registers 1
    invoke-direct {{p0}}, Landroid/webkit/WebChromeClient;-><init>()V
    return-void
.end method

# onPermissionRequest — accorde la demande JS (getUserMedia: caméra/micro).
# L'utilisateur a déjà été sollicité par la boîte de dialogue système Android
# via Activity->requestPermissions() dans MainActivity.onCreate ; ici on
# transmet cette autorisation déjà accordée à la couche WebView/JS.
.method public onPermissionRequest(Landroid/webkit/PermissionRequest;)V
    .registers 3
    .param p1, "request"
    invoke-virtual {{p1}}, Landroid/webkit/PermissionRequest;->getResources()[Ljava/lang/String;
    move-result-object v0
    invoke-virtual {{p1, v0}}, Landroid/webkit/PermissionRequest;->grant([Ljava/lang/String;)V
    return-void
.end method

# onGeolocationPermissionsShowPrompt — accorde automatiquement l'accès GPS
# demandé par navigator.geolocation côté JS (origin, allow=true, retain=false).
.method public onGeolocationPermissionsShowPrompt(Ljava/lang/String;Landroid/webkit/GeolocationPermissions$Callback;)V
    .registers 5
    .param p1, "origin"
    .param p2, "callback"
    const/4 v0, 0x1
    const/4 v1, 0x0
    invoke-interface {{p2, p1, v0, v1}}, Landroid/webkit/GeolocationPermissions$Callback;->invoke(Ljava/lang/String;ZZ)V
    return-void
.end method
'''


def _smali_webviewclient(package):
    pkg_smali = package.replace(".", "/")
    return f'''.class public L{pkg_smali}/InternalWebViewClient;
.super Landroid/webkit/WebViewClient;
.source "InternalWebViewClient.java"

.method public constructor <init>()V
    .registers 1
    invoke-direct {{p0}}, Landroid/webkit/WebViewClient;-><init>()V
    return-void
.end method

# API <24 — signature String (legacy, Android 6/7 compat)
.method public shouldOverrideUrlLoading(Landroid/webkit/WebView;Ljava/lang/String;)Z
    .registers 4
    const/4 v0, 0x0
    return v0
.end method

# API 24+ — signature WebResourceRequest (Android 7+, requise Android 12+)
.method public shouldOverrideUrlLoading(Landroid/webkit/WebView;Landroid/webkit/WebResourceRequest;)Z
    .registers 4
    const/4 v0, 0x0
    return v0
.end method

# onPageStarted — p0=this, p1=WebView, p2=String, p3=Bitmap → 4 registres
.method public onPageStarted(Landroid/webkit/WebView;Ljava/lang/String;Landroid/graphics/Bitmap;)V
    .registers 4
    invoke-super {{p0, p1, p2, p3}}, Landroid/webkit/WebViewClient;->onPageStarted(Landroid/webkit/WebView;Ljava/lang/String;Landroid/graphics/Bitmap;)V
    return-void
.end method

# onReceivedError legacy (API <23) — p0=this, p1=WebView, p2=int, p3=String, p4=String → 5 registres
.method public onReceivedError(Landroid/webkit/WebView;ILjava/lang/String;Ljava/lang/String;)V
    .registers 5
    return-void
.end method

# onReceivedError API 23+ (Android 6+) — p0=this, p1=WebView, p2=WebResourceRequest, p3=WebResourceError
.method public onReceivedError(Landroid/webkit/WebView;Landroid/webkit/WebResourceRequest;Landroid/webkit/WebResourceError;)V
    .registers 4
    return-void
.end method
'''


def _manifest_xml(package, app_name, version_code, version_name,
                  min_sdk, target_sdk, permissions, orientation="unspecified",
                  use_cleartext=True):
    perms_xml = "\n".join(
        f'    <uses-permission android:name="{p}"/>' for p in permissions
    )
    # usesCleartextTraffic pour Android 8.0 (API 26) et inférieur (réseau HTTP)
    cleartext_attr = 'android:usesCleartextTraffic="true"' if use_cleartext else ""
    # networkSecurityConfig requis pour charger du HTTP sur Android 9+ (API 28+)
    net_sec = 'android:networkSecurityConfig="@xml/network_security_config"'
    # roundIcon requis sur Android 7.1+ (Pixel Launcher et autres lanceurs adaptifs)
    round_icon = 'android:roundIcon="@mipmap/ic_launcher_round"'
    # configChanges étendu pour Android 14+
    config_changes = "orientation|keyboardHidden|screenSize|smallestScreenSize|screenLayout|density|uiMode"
    target_int = int(target_sdk) if str(target_sdk).isdigit() else 34
    min_int    = int(min_sdk) if str(min_sdk).isdigit() else 23

    # Construction des attributs optionnels — on évite les lignes vides dans le XML
    app_attrs = [
        'android:label="@string/app_name"',
        'android:icon="@mipmap/ic_launcher"',
        round_icon,
        'android:allowBackup="false"',
        'android:hardwareAccelerated="true"',
    ]
    if use_cleartext:
        app_attrs.append(cleartext_attr)
    app_attrs.append(net_sec)
    # fullBackupContent / dataExtractionRules requis Android 12+ (API 31+)
    if target_int >= 31:
        app_attrs.append('android:fullBackupContent="false"')
        app_attrs.append('android:dataExtractionRules="@xml/data_extraction_rules"')
    # enableOnBackInvokedCallback requis Android 13+ (API 33+) — doit être false pour compat
    if target_int >= 33:
        app_attrs.append('android:enableOnBackInvokedCallback="false"')

    # android:theme — applique AppTheme défini dans res/values/styles.xml
    app_attrs.append('android:theme="@style/AppTheme"')

    app_attrs_str = "\n        ".join(app_attrs)
    package_safe = xml_escape_attr(package)
    version_name_safe = xml_escape_attr(version_name)

    return f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{package_safe}"
    android:versionCode="{version_code}"
    android:versionName="{version_name_safe}">

    <uses-sdk
        android:minSdkVersion="{min_sdk}"
        android:targetSdkVersion="{target_sdk}" />

{perms_xml}

    <application
        {app_attrs_str}>

        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:screenOrientation="{orientation}"
            android:configChanges="{config_changes}"
            android:windowSoftInputMode="adjustResize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>

    </application>
</manifest>
'''


# Mapping Android API → nom de version pour apktool.yml
_SDK_CODENAME = {
    "19": "4.4", "21": "5.0.1", "22": "5.1.1", "23": "6.0",
    "24": "7.0", "25": "7.1.1", "26": "8.0.0", "27": "8.1.0",
    "28": "9", "29": "10", "30": "11", "31": "12", "32": "12L",
    "33": "13", "34": "14", "35": "15", "36": "16",
}

def _apktool_yml(package, version_code, version_name, min_sdk, target_sdk):
    # BUG-C05 — ne pas forcer compileSdk à 35 : si le framework android-35 n'est
    # pas dans le cache apktool local, le build échoue avec "Could not find
    # framework resources for sdk 35". On utilise le targetSdk réel choisi par
    # l'utilisateur, qui est forcément disponible puisque apktool le télécharge
    # à la première utilisation du SDK concerné.
    target_int = int(target_sdk) if str(target_sdk).isdigit() else 34
    compile_sdk = str(target_int)
    compile_name = _SDK_CODENAME.get(compile_sdk, compile_sdk)
    # versionName DOIT être entre guillemets dans le YAML apktool
    # sinon "1.0" est parsé comme flottant → erreur de recompilation
    ver_name_safe = str(version_name).replace("'", "")
    return f"""!!brut.androlib.meta.MetaInfo
compileSdkVersion: '{compile_sdk}'
compileSdkVersionCodename: '{compile_name}'
isFrameworkApk: false
packageInfo:
  forcedPackageId: '127'
  renameManifestPackage: null
sdkInfo:
  minSdkVersion: '{min_sdk}'
  targetSdkVersion: '{target_sdk}'
sharedLibrary: false
sparseResources: false
unknownFiles: {{}}
usesFramework:
  ids:
  - 1
  tag: null
version: 2.9.3
versionInfo:
  versionCode: '{version_code}'
  versionName: '{ver_name_safe}'
"""


def xml_escape_text(s):
    """Échappe le texte pour un contenu d'élément XML (entre balises)."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;"))

def xml_escape_attr(s):
    """Échappe le texte pour une valeur d'attribut XML (entre guillemets doubles)."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))

def _strings_xml(app_name):
    # Échappe correctement pour un contenu de balise (PAS de \" — invalide en XML)
    safe = xml_escape_text(app_name)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">{safe}</string>
</resources>
'''


def _styles_xml():
    # Theme.DeviceDefault.NoActionBar — disponible API 21+ (Android 5+), fonctionne Android 6→16
    # Pas besoin d'appcompat — plus robuste selon les ROMs Android 6
    return '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="AppTheme" parent="android:Theme.DeviceDefault.NoActionBar">
        <item name="android:windowBackground">@android:color/black</item>
        <item name="android:windowNoTitle">true</item>
        <item name="android:windowFullscreen">false</item>
        <item name="android:windowContentOverlay">@null</item>
        <item name="android:windowActionBar">false</item>
    </style>
</resources>
'''


def _colors_xml():
    return '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="white">#FFFFFF</color>
    <color name="black">#000000</color>
    <color name="primary">#2196F3</color>
</resources>
'''


def _network_security_xml():
    """Autorise le trafic HTTP clair et les CAs utilisateur (Android 6 → 16+)."""
    return '''<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <!-- Autorise HTTP (cleartext) sur tous les domaines — à restreindre en prod -->
    <base-config cleartextTrafficPermitted="true">
        <trust-anchors>
            <!-- CAs système (tous Android) -->
            <certificates src="system"/>
            <!-- CAs utilisateur — Android 7+ (API 24+) : nécessaire pour les proxys de debug -->
            <certificates src="user"/>
        </trust-anchors>
    </base-config>
</network-security-config>
'''

def _data_extraction_rules_xml():
    """Requis pour android:dataExtractionRules sur API 31+ (Android 12+)."""
    return '''<?xml version="1.0" encoding="utf-8"?>
<data-extraction-rules>
    <cloud-backup>
        <exclude domain="root" />
    </cloud-backup>
    <device-transfer>
        <exclude domain="root" />
    </device-transfer>
</data-extraction-rules>
'''


ICON_SIZES = {
    "mipmap-mdpi":    48,
    "mipmap-hdpi":    72,
    "mipmap-xhdpi":   96,
    "mipmap-xxhdpi":  144,
    "mipmap-xxxhdpi": 192,
}

# Mots-clés Java/Kotlin réservés — un segment de package ne peut pas en porter le nom
_JAVA_RESERVED = {
    "abstract","assert","boolean","break","byte","case","catch","char","class",
    "const","continue","default","do","double","else","enum","extends","final",
    "finally","float","for","goto","if","implements","import","instanceof","int",
    "interface","long","native","new","package","private","protected","public",
    "return","short","static","strictfp","super","switch","synchronized","this",
    "throw","throws","transient","try","void","volatile","while","true","false","null",
    "fun","val","var","when","object","is","in","as",
}

def normalize_package_name(raw, fallback="com.example.app"):
    """
    Valide/corrige un nom de package Android. Un package invalide est la cause
    n°1 de "package invalide" / "analyse du paquet échouée" à l'installation.
    Règles Android : >=2 segments, chaque segment commence par une lettre,
    ne contient que [a-zA-Z0-9_], aucun segment ne peut être un mot réservé.
    """
    raw = (raw or "").strip()
    if not raw:
        return fallback

    # Remplacer tout caractère interdit par underscore, retirer les espaces
    raw = re.sub(r'[^A-Za-z0-9_.]', '_', raw)
    segments = [s for s in raw.split('.') if s != ""]

    fixed = []
    for seg in segments:
        # Un segment ne peut pas commencer par un chiffre
        if seg and seg[0].isdigit():
            seg = "_" + seg
        # Un segment ne peut pas être un mot réservé
        if seg.lower() in _JAVA_RESERVED:
            seg = seg + "_"
        if seg:
            fixed.append(seg)

    if len(fixed) < 2:
        # Pas assez de segments valides → on complète avec le fallback
        base_segments = fallback.split(".")
        fixed = (fixed + base_segments)[:max(2, len(fixed))] if fixed else base_segments

    return ".".join(fixed)


def parse_custom_permissions(raw):
    if not raw: return []
    parts = re.split(r'[,\n;]+', raw)
    out = []
    for p in parts:
        p = p.strip()
        if not p: continue
        if "." not in p:
            p = "android.permission." + p.upper()
        if p not in out: out.append(p)
    return out


import uuid as _uuid

# =============================================================
# SESSIONS
# =============================================================
def new_session_id():
    # BUG-m01 — timestamp en ms = collision possible si deux builds démarrent
    # dans la même milliseconde (multi-onglets). On utilise uuid4 + timestamp
    # pour garantir l'unicité tout en gardant un tri chronologique.
    return f"{int(time.time() * 1000)}_{_uuid.uuid4().hex[:8]}"

def session_dir(sid):   return WORK_DIR / sid
def source_dir(sid):    return session_dir(sid) / "source"   # arbre smali/res/etc.
def decompiled_dir(sid): return source_dir(sid)              # alias pour compat

def list_sessions():
    out = []
    if not WORK_DIR.exists(): return out
    for d in sorted(WORK_DIR.iterdir(), reverse=True):
        meta_f = d / "session.json"
        if meta_f.exists():
            try: meta = json.loads(meta_f.read_text(encoding="utf-8"))
            except: meta = {}
            out.append({
                "session": d.name,
                "created": meta.get("created"),
                "packageOld": meta.get("package", meta.get("packageOld")),
                "hasDecompiled": (d / "source").exists() or (d / "decompiled").exists(),
                "origin": meta.get("origin", "scratch"),  # "scratch" | "decompile"
            })
    return out

def delete_session(sid):
    sd = session_dir(sid)
    if sd.exists(): shutil.rmtree(sd)

def _safe_target(sid, rel_path):
    # Supporte à la fois source/ (v3) et decompiled/ (v2 legacy)
    sdir_v3 = source_dir(sid).resolve()
    sdir_v2 = (session_dir(sid) / "decompiled").resolve()
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    rel_path = (rel_path or "").lstrip("/\\")
    target = (sdir / rel_path).resolve()
    if not str(target).startswith(str(sdir)):
        raise PermissionError("Chemin invalide")
    return sdir, target

def list_tree_all(sid):
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    if not sdir.exists(): return []
    items = []
    for p in sorted(sdir.rglob('*'), key=lambda x: str(x).lower()):
        rel = str(p.relative_to(sdir)).replace('\\', '/')
        items.append({'path': rel, 'type': 'dir' if p.is_dir() else 'file',
                      'size': p.stat().st_size if p.is_file() else 0})
    return items

def read_file_safe(sid, rel_path):
    sdir, target = _safe_target(sid, rel_path)
    if not target.is_file(): raise FileNotFoundError("Fichier introuvable")
    size = target.stat().st_size
    ext = target.suffix
    if size > MAX_TEXT_SIZE: return {"text": False, "reason": "too_large", "size": size}
    if ext in TEXT_EXTENSIONS or ext == "":
        try:
            return {"text": True, "content": target.read_text(encoding="utf-8"), "size": size}
        except UnicodeDecodeError:
            return {"text": False, "reason": "binary", "size": size}
    return {"text": False, "reason": "binary_type", "size": size, "ext": ext}

def write_file_safe(sid, rel_path, content):
    sdir, target = _safe_target(sid, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

def write_binary_safe(sid, rel_path, data_bytes):
    sdir, target = _safe_target(sid, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data_bytes)

def read_binary_raw(sid, rel_path):
    sdir, target = _safe_target(sid, rel_path)
    if not target.is_file(): raise FileNotFoundError("Fichier introuvable")
    return target.read_bytes()


# =============================================================
# CRÉATION DU PROJET DE ZÉRO (SANS TEMPLATE)
# =============================================================
def create_scratch_session(config, icon_bytes, splash_bytes, site_zip_bytes, logger):
    """
    Crée un dossier source complet pour apktool b — sans aucun template APK.
    Retourne le session id.
    """
    sid = new_session_id()
    sdir = session_dir(sid)
    src  = source_dir(sid)
    sdir.mkdir(parents=True)
    src.mkdir(parents=True)

    # ── Paramètres ────────────────────────────────────────────
    package      = normalize_package_name(config.get("packageName"))
    app_name     = (config.get("appName")     or "MyApp").strip()
    version_code = str(config.get("versionCode") or "1")
    version_name = str(config.get("versionName") or "1.0")
    min_sdk      = str(config.get("minSdk")      or "23")
    target_sdk   = str(config.get("targetSdk")   or "35")
    orientation  = config.get("orientation", "unspecified")
    mode         = config.get("mode", "url")         # url | html | sitezip
    app_url      = (config.get("appUrl") or "").strip()
    html_inline  = config.get("htmlContent") or ""

    # Permissions
    default_perms = [
        "android.permission.INTERNET",
        "android.permission.ACCESS_NETWORK_STATE",
    ]
    selected_perms = config.get("permissions", [])
    extra_perms    = parse_custom_permissions(config.get("customPermissions", ""))
    permissions    = list(dict.fromkeys(default_perms + selected_perms + extra_perms))

    logger.log(f"📦 Package : {package}")
    logger.log(f"📱 App     : {app_name} v{version_name} (code {version_code})")
    logger.log(f"🔧 SDK     : minSdk={min_sdk} targetSdk={target_sdk}")

    # ── apktool.yml ───────────────────────────────────────────
    (src / "apktool.yml").write_text(
        _apktool_yml(package, version_code, version_name, min_sdk, target_sdk),
        encoding="utf-8"
    )
    logger.log("✅ apktool.yml")

    # ── AndroidManifest.xml ───────────────────────────────────
    (src / "AndroidManifest.xml").write_text(
        _manifest_xml(package, app_name, version_code, version_name,
                      min_sdk, target_sdk, permissions, orientation),
        encoding="utf-8"
    )
    logger.log("✅ AndroidManifest.xml")

    # ── Smali ─────────────────────────────────────────────────
    pkg_path = package.replace(".", "/")
    smali_pkg = src / "smali" / pkg_path
    smali_pkg.mkdir(parents=True)

    if mode == "url":
        if not app_url:
            logger.log("⚠ MINEUR #7 — mode URL actif mais aucune URL fournie : "
                       "fallback vers https://example.com. Vérifie le champ URL "
                       "si ce n'est pas voulu.")
        wv_mode, wv_arg = "url", app_url or "https://example.com"
    elif mode == "sitezip":
        wv_mode, wv_arg = "www", ""
    else:
        wv_mode, wv_arg = "asset", ""

    (smali_pkg / "MainActivity.smali").write_text(
        _smali_main(package, wv_arg, wv_mode, orientation, permissions=permissions), encoding="utf-8"
    )
    (smali_pkg / "InternalWebViewClient.smali").write_text(
        _smali_webviewclient(package), encoding="utf-8"
    )
    (smali_pkg / "InternalWebChromeClient.smali").write_text(
        _smali_webchromeclient(package), encoding="utf-8"
    )
    logger.log(f"✅ Smali générés ({wv_mode})")

    # ── Ressources ────────────────────────────────────────────
    res = src / "res"
    values = res / "values"
    values.mkdir(parents=True)
    (values / "strings.xml").write_text(_strings_xml(app_name), encoding="utf-8")
    (values / "styles.xml").write_text(_styles_xml(), encoding="utf-8")
    (values / "colors.xml").write_text(_colors_xml(), encoding="utf-8")
    logger.log("✅ res/values/")

    # ── Icônes (normale + round pour Android 7.1+) ──────────
    for folder, size in ICON_SIZES.items():
        icon_dir = res / folder
        icon_dir.mkdir(parents=True)
        png_data = make_icon_png(icon_bytes, size) if icon_bytes else DEFAULT_ICON_BYTES
        (icon_dir / "ic_launcher.png").write_bytes(png_data)
        (icon_dir / "ic_launcher_round.png").write_bytes(png_data)
    logger.log(f"✅ Icônes ({len(ICON_SIZES)} densités + round)")

    # ── XML requis Android 12+ ────────────────────────────────
    xml_dir = res / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    # network_security_config.xml — autorise HTTP et HTTPS
    (xml_dir / "network_security_config.xml").write_text(_network_security_xml(), encoding="utf-8")
    # data_extraction_rules.xml — requis targetSdk 31+
    (xml_dir / "data_extraction_rules.xml").write_text(_data_extraction_rules_xml(), encoding="utf-8")
    logger.log("✅ res/xml/ (network_security_config + data_extraction_rules)")

    # ── Assets ────────────────────────────────────────────────
    assets_dir = src / "assets"
    assets_dir.mkdir(exist_ok=True)

    if mode == "html" and html_inline:
        # BUG-m03 — forcer <meta charset="UTF-8"> si absent (un HTML collé depuis Word
        # peut contenir charset="ISO-8859-1" → caractères cassés dans la WebView)
        if "<meta charset" not in html_inline.lower() and "<meta http-equiv=\"content-type\"" not in html_inline.lower():
            html_inline = html_inline.replace(
                "<head>", '<head>\n<meta charset="UTF-8">', 1
            ) if "<head>" in html_inline else (
                '<meta charset="UTF-8">\n' + html_inline
            )
        (assets_dir / "index.html").write_text(html_inline, encoding="utf-8")
        logger.log("✅ assets/index.html injecté")

    elif mode == "sitezip" and site_zip_bytes:
        logger.log("🗂 Extraction du site complet (zip)...")
        www_dir = assets_dir / "www"
        www_dir.mkdir()
        zpath = sdir / "site_upload.zip"
        zpath.write_bytes(site_zip_bytes)
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract_zip(zf, www_dir, logger)
        # Remonter d'un niveau si tout est dans un sous-dossier unique
        children = list(www_dir.iterdir())
        if len(children) == 1 and children[0].is_dir() and not (www_dir / "index.html").exists():
            inner = children[0]
            for item in inner.iterdir():
                shutil.move(str(item), str(www_dir / item.name))
            inner.rmdir()
        zpath.unlink(missing_ok=True)
        if (www_dir / "index.html").exists():
            logger.log("✅ Site extrait dans assets/www/")
        else:
            logger.log("⚠ Pas d'index.html trouvé à la racine du zip")

    # ── Splash (mode scratch) ─────────────────────────────────
    # En mode scratch il n'y a pas d'images préexistantes à remplacer.
    # On écrit le splash dans assets/ et res/drawable/ pour qu'il soit
    # visible dans le sélecteur d'images du Mode Dev.
    if splash_bytes:
        try:
            splash_png = ensure_png_bytes(splash_bytes, logger, "splash")
            (assets_dir / "splash.png").write_bytes(splash_png)
            drawable_dir = res / "drawable"
            drawable_dir.mkdir(parents=True, exist_ok=True)
            (drawable_dir / "splash.png").write_bytes(splash_png)
            logger.log("✅ Splash écrit dans assets/splash.png + res/drawable/splash.png")
        except Exception as _e:
            logger.log(f"⚠ Splash ignoré : {_e}")

    # ── Métadonnées de session ─────────────────────────────────
    meta = {
        "created": time.time(),
        "package": package,
        "packageOld": package,
        "appName": app_name,
        "origin": "scratch",
    }
    (sdir / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    logger.log(f"✅ Session créée: {sid}")
    return sid


# =============================================================
# DÉCOMPILATION (mode Dev — conservé pour APK existants)
# =============================================================
def decompile_to_session(template_bytes, logger):
    sid = new_session_id()
    sd  = session_dir(sid)
    sd.mkdir(parents=True)
    template_path = sd / "template.apk"

    if template_bytes:
        template_path.write_bytes(template_bytes)
        logger.log("Template APK chargé depuis upload")
    else:
        raise RuntimeError(
            "Aucun template APK fourni. "
            "Utilise le mode scratch (sans template) ou uploade un APK."
        )

    java    = find_tool("java")
    apktool = find_tool("apktool")
    if not java:    raise RuntimeError("Java non trouvé — installe Java JDK")
    if not apktool: raise RuntimeError("apktool non trouvé dans tools/ ou PATH")

    decompiled = sd / "source"
    logger.log("🔧 Décompilation APK...")
    if decompiled.exists():
        shutil.rmtree(str(decompiled))
        logger.log("🗑 Dossier source existant supprimé avant décompilation")
    if apktool.endswith(('.jar', '.bat')):
        cmd = [java, "-jar", apktool, "d", str(template_path), "-o", str(decompiled), "-f"]
    else:
        cmd = [apktool, "d", str(template_path), "-o", str(decompiled), "-f"]
    ok = run_cmd(cmd, logger)
    if not ok or not decompiled.exists():
        raise RuntimeError("Échec décompilation — vérifie les logs")

    manifest = decompiled / "AndroidManifest.xml"
    pkg_old  = "com.example.app"
    if manifest.exists():
        mc = manifest.read_text(encoding="utf-8", errors="ignore")
        m  = re.search(r'package="([^"]+)"', mc)
        if m: pkg_old = m.group(1)

    meta = {
        "created": time.time(),
        "package": pkg_old,
        "packageOld": pkg_old,
        "origin": "decompile",
    }
    (sd / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    logger.log(f"✅ Session prête: {sid} (package: {pkg_old})")
    return sid, pkg_old


# =============================================================
# APPLY CONFIG (modification d'une session existante)
# =============================================================
def apply_config(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger):
    """
    Met à jour une session existante (scratch ou décompilée) avec les
    paramètres de l'utilisateur.  Pour une session scratch, on régénère
    directement les fichiers générés.
    """
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    src = sdir_v3 if sdir_v3.exists() else sdir_v2

    # Détecte l'origine
    meta = {}
    meta_f = session_dir(sid) / "session.json"
    if meta_f.exists():
        try: meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except: pass
    origin = meta.get("origin", "decompile")

    package_new_raw = (config.get("packageName") or "").strip()
    package_new  = normalize_package_name(package_new_raw) if package_new_raw else ""
    app_name     = (config.get("appName")     or "MyApp").strip()
    version_code = str(config.get("versionCode") or "1")
    version_name = str(config.get("versionName") or "1.0")
    min_sdk      = str(config.get("minSdk")      or "23")
    target_sdk   = str(config.get("targetSdk")   or "35")
    orientation  = config.get("orientation", "unspecified")
    mode         = config.get("mode", "url")
    app_url      = (config.get("appUrl") or "").strip()
    html_inline  = config.get("htmlContent") or ""

    selected_perms = config.get("permissions", [])
    extra_perms    = parse_custom_permissions(config.get("customPermissions", ""))
    default_perms  = ["android.permission.INTERNET", "android.permission.ACCESS_NETWORK_STATE"]
    permissions    = list(dict.fromkeys(default_perms + selected_perms + extra_perms))

    if origin == "scratch":
        # Régénère les fichiers depuis zéro
        logger.log("🔄 Mise à jour de la session scratch...")
        if package_new:
            old_pkg  = meta.get("package", "com.example.app")
            pkg_path = old_pkg.replace(".", "/")
            new_path = package_new.replace(".", "/")
            if old_pkg != package_new:
                # Renommer les dossiers smali
                old_smali = src / "smali" / pkg_path
                new_smali = src / "smali" / new_path
                if old_smali.exists():
                    new_smali.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(str(old_smali), str(new_smali), dirs_exist_ok=True)
                    shutil.rmtree(str(old_smali))

                # Réécrire les smali avec le bon package
                if mode == "url":
                    if not app_url:
                        logger.log("⚠ MINEUR #7 — mode URL actif mais aucune URL "
                                   "fournie : fallback vers https://example.com.")
                    wv_mode, wv_arg = "url", app_url or "https://example.com"
                elif mode == "sitezip":
                    wv_mode, wv_arg = "www", ""
                else:
                    wv_mode, wv_arg = "asset", ""

                new_smali_pkg = src / "smali" / new_path
                new_smali_pkg.mkdir(parents=True, exist_ok=True)
                (new_smali_pkg / "MainActivity.smali").write_text(
                    _smali_main(package_new, wv_arg, wv_mode, orientation, permissions=permissions),
                    encoding="utf-8"
                )
                (new_smali_pkg / "InternalWebViewClient.smali").write_text(
                    _smali_webviewclient(package_new), encoding="utf-8"
                )
                (new_smali_pkg / "InternalWebChromeClient.smali").write_text(
                    _smali_webchromeclient(package_new), encoding="utf-8"
                )
                meta["package"] = package_new
                meta["packageOld"] = package_new
            else:
                # Même package, juste mettre à jour l'URL/mode
                if mode == "url":
                    if not app_url:
                        logger.log("⚠ MINEUR #7 — mode URL actif mais aucune URL "
                                   "fournie : fallback vers https://example.com.")
                    wv_mode, wv_arg = "url", app_url or "https://example.com"
                elif mode == "sitezip": wv_mode, wv_arg = "www", ""
                else: wv_mode, wv_arg = "asset", ""
                smali_pkg = src / "smali" / pkg_path
                smali_pkg.mkdir(parents=True, exist_ok=True)
                (smali_pkg / "MainActivity.smali").write_text(
                    _smali_main(package_new or old_pkg, wv_arg, wv_mode, orientation, permissions=permissions),
                    encoding="utf-8"
                )
                # BUG-M02 — toujours régénérer InternalWebViewClient pour éviter un
                # ClassNotFoundException si le fichier a été corrompu ou supprimé
                (smali_pkg / "InternalWebViewClient.smali").write_text(
                    _smali_webviewclient(package_new or old_pkg), encoding="utf-8"
                )
                # Idem pour InternalWebChromeClient (CRITIQUE #2) — toujours régénéré
                (smali_pkg / "InternalWebChromeClient.smali").write_text(
                    _smali_webchromeclient(package_new or old_pkg), encoding="utf-8"
                )

        pkg_final = package_new or meta.get("package", "com.example.app")

        # Manifeste
        (src / "AndroidManifest.xml").write_text(
            _manifest_xml(pkg_final, app_name, version_code, version_name,
                          min_sdk, target_sdk, permissions, orientation),
            encoding="utf-8"
        )
        # apktool.yml
        (src / "apktool.yml").write_text(
            _apktool_yml(pkg_final, version_code, version_name, min_sdk, target_sdk),
            encoding="utf-8"
        )
        # Strings
        values = src / "res" / "values"
        values.mkdir(parents=True, exist_ok=True)
        (values / "strings.xml").write_text(_strings_xml(app_name), encoding="utf-8")

        # Icônes (normale + round)
        if icon_bytes:
            for folder, size in ICON_SIZES.items():
                icon_dir = src / "res" / folder
                icon_dir.mkdir(parents=True, exist_ok=True)
                png_data = make_icon_png(icon_bytes, size)
                (icon_dir / "ic_launcher.png").write_bytes(png_data)
                (icon_dir / "ic_launcher_round.png").write_bytes(png_data)
            logger.log(f"✅ Icônes mises à jour (+ round)")
        # S'assurer que res/xml/ existe toujours avec les fichiers requis
        xml_dir = src / "res" / "xml"
        xml_dir.mkdir(parents=True, exist_ok=True)
        if not (xml_dir / "network_security_config.xml").exists():
            (xml_dir / "network_security_config.xml").write_text(_network_security_xml(), encoding="utf-8")
        if not (xml_dir / "data_extraction_rules.xml").exists():
            (xml_dir / "data_extraction_rules.xml").write_text(_data_extraction_rules_xml(), encoding="utf-8")

    else:
        # Session décompilée — comportement classique (patch fichiers)
        _apply_config_decompiled(sid, src, config, icon_bytes, splash_bytes, site_zip_bytes, logger,
                                 app_name, package_new, version_code, version_name,
                                 min_sdk, target_sdk, permissions, mode, app_url, html_inline)
        meta["package"] = package_new or meta.get("package", "com.example.app")
        meta["appName"] = app_name

    # Assets communs (site zip / html inline)
    assets_dir = src / "assets"
    assets_dir.mkdir(exist_ok=True)
    if mode == "html" and html_inline:
        (assets_dir / "index.html").write_text(html_inline, encoding="utf-8")
        logger.log("✅ assets/index.html mis à jour")
    elif mode == "sitezip" and site_zip_bytes:
        www_dir = assets_dir / "www"
        if www_dir.exists(): shutil.rmtree(www_dir)
        www_dir.mkdir(parents=True)
        zpath = session_dir(sid) / "site_upload.zip"
        zpath.write_bytes(site_zip_bytes)
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract_zip(zf, www_dir, logger)
        children = list(www_dir.iterdir())
        if len(children) == 1 and children[0].is_dir() and not (www_dir / "index.html").exists():
            inner = children[0]
            for item in inner.iterdir():
                shutil.move(str(item), str(www_dir / item.name))
            inner.rmdir()
        zpath.unlink(missing_ok=True)
        logger.log("✅ Site extrait")

    meta["appName"] = app_name
    meta_f.write_text(json.dumps(meta), encoding="utf-8")


def _apply_config_decompiled(sid, src, config, icon_bytes, splash_bytes, site_zip_bytes, logger,
                              app_name, package_new, version_code, version_name,
                              min_sdk, target_sdk, permissions, mode, app_url, html_inline):
    """Patch d'une session décompilée depuis un APK existant."""
    manifest_path = src / "AndroidManifest.xml"
    if not manifest_path.exists():
        raise RuntimeError("AndroidManifest.xml introuvable dans la session")

    mc = manifest_path.read_text(encoding="utf-8", errors="ignore")
    pkg_old = re.search(r'package="([^"]+)"', mc)
    pkg_old = pkg_old.group(1) if pkg_old else "com.example.app"

    if package_new and package_new != pkg_old:
        logger.log(f"📦 Renommage package: {pkg_old} → {package_new}")
        safe_exts = {'.smali', '.xml', '.json', '.txt', '.properties', '.MF', '.SF', '.gradle'}
        count = 0
        # BUG CRITIQUE CORRIGÉ : en smali le package s'écrit en forme "slash"
        # (Lcom/oldtemplate/app/MainActivity;), PAS en forme pointée
        # (com.oldtemplate.app). L'ancien code ne remplaçait QUE la forme
        # pointée — donc le nom de classe interne des .smali n'était jamais
        # mis à jour, alors que le dossier, lui, était bien déplacé.
        # Résultat : le manifest référence .MainActivity sous le nouveau
        # package, mais la classe réelle déclare encore l'ancien package →
        # ClassNotFoundException au lancement → l'app s'ouvre et se ferme
        # aussitôt. On remplace donc les DEUX formes, dans cet ordre
        # (slash d'abord, qui est la forme la plus fréquente en smali).
        pkg_old_slash = pkg_old.replace(".", "/")
        pkg_new_slash = package_new.replace(".", "/")
        for f in src.rglob('*'):
            # AndroidManifest.xml est traité séparément ci-dessous (regex ciblées
            # sur les attributs précis) — l'exclure ici évite un double traitement
            # imprévisible si le contenu du package apparaît dans d'autres attributs.
            if f.is_file() and f.suffix in safe_exts and f.name != "AndroidManifest.xml":
                try:
                    content = f.read_bytes()
                    changed = False
                    if pkg_old_slash.encode() in content:
                        content = content.replace(pkg_old_slash.encode(), pkg_new_slash.encode())
                        changed = True
                    if pkg_old.encode() in content:
                        content = content.replace(pkg_old.encode(), package_new.encode())
                        changed = True
                    if changed:
                        f.write_bytes(content); count += 1
                except: pass
        logger.log(f"Package remplacé dans {count} fichier(s)")
        old_path = pkg_old.replace(".", "/")
        new_path = package_new.replace(".", "/")
        for smali_dir in src.glob("smali*"):
            old_d = smali_dir / old_path
            new_d = smali_dir / new_path
            if old_d.exists() and old_d != new_d:
                new_d.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copytree(str(old_d), str(new_d), dirs_exist_ok=True)
                    shutil.rmtree(str(old_d))
                    logger.log(f"Dossier smali renommé")
                except Exception as e:
                    logger.log(f"⚠ Rename smali: {e}")

    # Garantir que res/values/strings.xml existe AVANT de forcer
    # android:label="@string/app_name" dans le manifest — sinon apktool b
    # échoue avec "resource string/app_name not found".
    values_dir = src / "res" / "values"
    values_dir.mkdir(parents=True, exist_ok=True)
    strings_xml_pre = values_dir / "strings.xml"
    if not strings_xml_pre.exists():
        strings_xml_pre.write_text(_strings_xml(app_name), encoding="utf-8")
        logger.log("✅ res/values/strings.xml créé (absent du template)")
    elif '<string name="app_name">' not in strings_xml_pre.read_text(encoding="utf-8", errors="ignore"):
        sc0 = strings_xml_pre.read_text(encoding="utf-8", errors="ignore")
        sc0 = sc0.replace("</resources>",
                           f'    <string name="app_name">{xml_escape_text(app_name)}</string>\n</resources>')
        strings_xml_pre.write_text(sc0, encoding="utf-8")
        logger.log("✅ string app_name ajoutée à strings.xml existant")

    pkg_final = package_new or pkg_old
    pkg_final_safe = xml_escape_attr(pkg_final)
    version_name_safe = xml_escape_attr(version_name)
    mc = manifest_path.read_text(encoding="utf-8", errors="ignore")
    mc = re.sub(r'package="[^"]*"', f'package="{pkg_final_safe}"', mc, count=1)
    # On pointe toujours vers @string/app_name (mis à jour juste après dans strings.xml)
    # plutôt que d'écrire le nom en dur — évite une désynchronisation entre les deux.
    mc = re.sub(r'android:label="[^"]*"', 'android:label="@string/app_name"', mc, count=1)
    mc = re.sub(r'android:versionCode="[^"]*"', f'android:versionCode="{version_code}"', mc)
    mc = re.sub(r'android:versionName="[^"]*"', f'android:versionName="{version_name_safe}"', mc)
    mc = re.sub(r'android:minSdkVersion="[^"]*"', f'android:minSdkVersion="{min_sdk}"', mc)
    mc = re.sub(r'android:targetSdkVersion="[^"]*"', f'android:targetSdkVersion="{target_sdk}"', mc)
    # BUG-FIX : remplacer toutes les références restantes à l'ancien package
    # dans le Manifest (android:name, android:authorities, android:permission,
    # etc. dans <activity>, <provider>, <receiver>, <service>).
    # Le bloc de remplacement massif (plus haut) exclut volontairement le
    # Manifest pour éviter un double traitement imprévisible — on fait donc
    # ce remplacement ici, APRÈS les regex ciblées sur les attributs techniques,
    # pour ne pas interférer avec elles.
    if package_new and pkg_old and package_new != pkg_old:
        mc = mc.replace(pkg_old, package_new)
        logger.log(f"✅ android:name / authorities mis à jour dans le Manifest ({pkg_old} → {package_new})")
    # Ajouter uniquement les permissions absentes — évite les conflits de doublons
    for p in permissions:
        short = p.split(".")[-1]
        if p not in mc and short not in mc:
            perm_line = f'\n    <uses-permission android:name="{p}"/>'
            # Insérer avant </manifest> ou avant <application
            if '<application' in mc:
                mc = mc.replace('<application', perm_line + '\n    <application', 1)
            else:
                mc = mc.replace('</manifest>', perm_line + '\n</manifest>', 1)
    manifest_path.write_text(mc, encoding="utf-8")
    logger.log("✅ AndroidManifest.xml patché")

    strings_xml = src / "res" / "values" / "strings.xml"
    if strings_xml.exists():
        sc = strings_xml.read_text(encoding="utf-8", errors="ignore")
        app_name_safe = xml_escape_text(app_name)
        sc = re.sub(r'<string name="app_name">[^<]*</string>',
                    f'<string name="app_name">{app_name_safe}</string>', sc)
        strings_xml.write_text(sc, encoding="utf-8")

    # IMPORTANT #4 (cf. rapport de bugs) : sur un template décompilé (Mode Dev),
    # seul le mode "url" patchait jusqu'ici l'appel WebView->loadUrl(...) du
    # smali d'origine. En mode "html" ou "sitezip", les fichiers étaient bien
    # écrits dans assets/index.html ou assets/www/, mais l'appel loadUrl du
    # template continuait à charger son ancienne cible — le nouveau contenu
    # n'était donc jamais affiché. On utilise la même regex ciblée (uniquement
    # l'argument direct de loadUrl) pour les trois modes, avec la cible
    # appropriée à chaque cas.
    if mode == "url" and app_url:
        target_value = app_url
    elif mode == "html":
        target_value = "file:///android_asset/index.html"
    elif mode == "sitezip":
        target_value = "file:///android_asset/www/index.html"
    else:
        target_value = None

    if target_value:
        total = 0
        # On ne remplace QUE la chaîne passée directement à WebView->loadUrl(...).
        # L'ancien code remplaçait toute URL trouvée dans n'importe quel .smali,
        # ce qui pouvait casser des appels API/pub/paiement intégrés au template.
        if mode == "url":
            # En mode URL, la valeur d'origine est forcément une URL http(s) —
            # on ne cible que ça pour ne pas toucher à un file:/// existant.
            value_pattern = r'https?://[^\s"\'\\]+'
        else:
            # En mode html/sitezip, l'appel loadUrl du template peut charger
            # n'importe quoi (une URL distante, un file:///android_asset/...
            # existant, etc.) — on cible large : toute chaîne passée à loadUrl.
            value_pattern = r'[^\s"\'\\]+'
        load_url_pattern = re.compile(
            r'(const-string(?:/jumbo)?\s+v\d+\s*,\s*")'
            r'(' + value_pattern + r')'
            r'("\s*\n(?:\s*\n)*\s*invoke-virtual\s*\{[^}]*\},\s*Landroid/webkit/WebView;->loadUrl)'
        )
        for smali_f in src.rglob("*.smali"):
            try:
                content = smali_f.read_text(encoding="utf-8", errors="ignore")
                def _sub(m):
                    nonlocal total
                    if m.group(2) == target_value:
                        return m.group(0)
                    total += 1
                    return m.group(1) + _smali_string_escape(target_value) + m.group(3)
                new_content = load_url_pattern.sub(_sub, content)
                if new_content != content:
                    smali_f.write_text(new_content, encoding="utf-8")
            except: pass
        if total:
            logger.log(f"URL de chargement WebView remplacée dans {total} emplacement(s) → {target_value}")
        else:
            logger.log("⚠ Aucun appel WebView.loadUrl(...) trouvé à patcher — "
                        "vérifie manuellement le smali si le template n'est pas une simple WebView")

    if icon_bytes:
        icon_bytes_png = ensure_png_bytes(icon_bytes, logger, "icône")
        icon_dirs = ['mipmap-hdpi','mipmap-xhdpi','mipmap-xxhdpi','mipmap-xxxhdpi','mipmap-mdpi',
                     'drawable','drawable-hdpi','drawable-xhdpi','drawable-xxhdpi','drawable-xxxhdpi']
        icon_names = ['ic_launcher.png','icon.png','ic_launcher_round.png','app_icon.png','launcher_icon.png']
        replaced = 0
        for d in icon_dirs:
            dp = src / "res" / d
            if dp.exists():
                for iname in icon_names:
                    f = dp / iname
                    if f.exists(): f.write_bytes(icon_bytes_png); replaced += 1
        logger.log(f"Icônes remplacées: {replaced} fichiers")

    if splash_bytes:
        splash_bytes_png = ensure_png_bytes(splash_bytes, logger, "splash")
        # splash_paths: liste de chemins relatifs explicites sélectionnés
        # par l'utilisateur dans la grille d'images (via /replace-images ou
        # transmis dans config["splashPaths"]). Si vide → fallback sur les
        # noms connus (comportement historique pour compatibilité).
        splash_paths = (config or {}).get("splashPaths", [])
        replaced = 0
        if splash_paths:
            for rel in splash_paths:
                target = src / rel.lstrip("/\\")
                if target.is_file():
                    target.write_bytes(splash_bytes_png); replaced += 1
                else:
                    # Chemin inexistant → créer quand même (cas assets/splash.png)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(splash_bytes_png); replaced += 1
            logger.log(f"Splash remplacé dans {replaced} chemin(s) sélectionné(s)")
        else:
            # Fallback : cherche par nom / pattern dans toutes les images
            # (png + jpg + webp) — inclut les noms exacts connus ET tout
            # fichier dont le nom contient "splash", "launch" ou "background".
            SPLASH_EXACT = {
                'splash.png','splash.jpg','splash.webp',
                'splash_screen.png','splash_screen.jpg',
                'background_splash.png','background_splash.jpg',
                'launch_background.png','launch_background.jpg',
                'launch_screen.png','launch_screen.jpg',
            }
            SPLASH_KEYWORDS = ('splash', 'launch_bg', 'launch_background',
                               'launch_screen', 'background_splash')
            IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}
            for f in src.rglob("*"):
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue
                name_low = f.name.lower()
                if name_low in SPLASH_EXACT or any(k in name_low for k in SPLASH_KEYWORDS):
                    f.write_bytes(splash_bytes_png); replaced += 1
            if replaced == 0:
                logger.log("⚠ Aucun fichier splash trouvé automatiquement. "
                           "Sélectionne les images à remplacer dans la grille "
                           "(clic droit sur une image → cocher, puis 'Appliquer le splash').")
            else:
                logger.log(f"✅ Splash remplacé: {replaced} fichier(s) trouvé(s) automatiquement")


# =============================================================
# RECOMPILATION + SIGNATURE
# =============================================================
def recompile_session(sid, signing, out_name, logger):
    sd = session_dir(sid)

    # Détermine le dossier source
    sdir_v3 = source_dir(sid)
    sdir_v2 = sd / "decompiled"
    src = sdir_v3 if sdir_v3.exists() else sdir_v2

    java    = find_tool("java")
    apktool = find_tool("apktool")
    if not java:    raise RuntimeError("java introuvable")
    if not apktool: raise RuntimeError("apktool introuvable")

    # minSdkVersion pour apksigner — défaut Android 6 (API 23)
    effective_min_sdk = "23"
    try:
        mc = (src / "AndroidManifest.xml").read_text(encoding="utf-8", errors="ignore")
        m  = re.search(r'android:minSdkVersion="(\d+)"', mc)
        if m: effective_min_sdk = m.group(1)
    except: pass

    logger.log("🔨 Compilation APK depuis les sources...")
    unsigned_apk = sd / "unsigned.apk"

    if apktool.endswith(('.jar', '.bat')):
        cmd = [java, "-jar", apktool, "b", str(src), "-o", str(unsigned_apk)]
    else:
        cmd = [apktool, "b", str(src), "-o", str(unsigned_apk)]
    ok = run_cmd(cmd, logger)
    if not ok or not unsigned_apk.exists():
        raise RuntimeError(
            "Échec compilation — vérifie les logs.\n"
            "Causes fréquentes : Java absent, apktool absent, "
            "erreur de syntaxe smali, ressources invalides."
        )

    aligned_apk = sd / "aligned.apk"
    zipalign = find_tool("zipalign")
    if zipalign:
        logger.log("🔧 Zipalign...")
        aligned_apk.unlink(missing_ok=True)
        ok_zip = run_cmd([zipalign, "-f", "-v", "4", str(unsigned_apk), str(aligned_apk)], logger)
        # BUG-M07 — la condition * 0.5 rejetait à tort les APKs valides dont le contenu
        # est très compressible (l'APK aligné peut légitimement être plus petit que l'original).
        # On vérifie uniquement que zipalign a réussi ET produit un fichier non vide.
        if not ok_zip or not aligned_apk.exists() or aligned_apk.stat().st_size == 0:
            logger.log("⚠ Zipalign a échoué — utilisation APK non aligné (installable mais refusé par certains appareils)")
            aligned_apk.unlink(missing_ok=True)
            aligned_apk = unsigned_apk
    else:
        logger.log("⚠ zipalign non trouvé, étape sautée — APK non aligné peut être refusé par Samsung/Tecno")
        aligned_apk = unsigned_apk

    final_apk = OUTPUT_DIR / out_name
    signing_mode = (signing or {}).get("mode", "debug")

    if signing_mode == "nosign":
        shutil.copy(aligned_apk, final_apk)
        logger.log(f"✅ APK compilé (non signé) : {final_apk.name}")
        return final_apk

    keystore = None
    ks_pass  = "android"
    key_pass = "android"
    alias    = "androiddebugkey"
    # Flag pour détecter un keystore PKCS12 généré par Python (BUG-C03)
    ks_is_pkcs12 = False

    if signing_mode == "custom" and signing.get("keystoreB64"):
        keystore = sd / "custom.keystore"
        keystore.write_bytes(base64.b64decode(signing["keystoreB64"]))
        alias    = signing.get("alias")    or alias
        ks_pass  = signing.get("storePass") or ks_pass
        key_pass = signing.get("keyPass")   or ks_pass
        # Détecter si c'est un PKCS12 (magic bytes PK\x03\x04 ou 0x30 0x82) — BUG-C03
        try:
            magic = keystore.read_bytes()[:4]
            if magic[:2] == b'PK' or magic[0:1] == b'\x30':
                ks_is_pkcs12 = True
        except Exception:
            pass
        logger.log(f"🔑 Keystore personnalisé (alias: {alias}{'  PKCS12' if ks_is_pkcs12 else ''})")
    else:
        keystore = TOOLS_DIR / "debug.keystore"
        logger.log("🔑 Keystore debug")
        logger.log("⚠ APK debug = souvent bloqué par Play Protect. Crée un keystore perso pour éviter ça.")

    # BUG-C01 — Vérification explicite avant d'aller plus loin
    if not keystore or not keystore.exists():
        raise RuntimeError(
            f"Keystore introuvable : {keystore}\n"
            "Lance launcher.bat pour créer debug.keystore, "
            "ou fournis un keystore personnalisé via l'interface."
        )

    apksigner = find_tool("apksigner")
    signed_ok = False

    if apksigner and keystore and keystore.exists():
        logger.log("✍ Signature avec apksigner...")
        ks_pass_arg,  ks_pass_file  = _pass_arg(ks_pass,  sd, "ks.pass.tmp")
        key_pass_arg, key_pass_file = _pass_arg(key_pass, sd, "key.pass.tmp")
        min_sdk_int = int(effective_min_sdk) if effective_min_sdk.isdigit() else 23
        # v4 signing (APK Signature Scheme v4) — Android 11+ (API 30+)
        # Active uniquement si minSdk >= 30 pour éviter problèmes sur appareils plus anciens
        # Sur Android 6-10, seuls v1+v2+v3 sont nécessaires
        enable_v4 = min_sdk_int >= 30
        cmd = [apksigner, "sign",
               "--ks", str(keystore),
               "--ks-pass", ks_pass_arg, "--key-pass", key_pass_arg,
               "--min-sdk-version", effective_min_sdk,
               "--v1-signing-enabled", "true",
               "--v2-signing-enabled", "true",
               "--v3-signing-enabled", "true",
               # BUG-C03 — forcer PKCS12 si keystore généré par la lib Python cryptography
               *( ["--ks-type", "PKCS12"] if ks_is_pkcs12 else [] ),
               "--v4-signing-enabled", "true" if enable_v4 else "false"]
        if alias: cmd += ["--ks-key-alias", alias]
        cmd += ["--out", str(final_apk), str(aligned_apk)]
        try:
            signed_ok = run_cmd(cmd, logger)
        finally:
            for f in (ks_pass_file, key_pass_file):
                try: f.unlink(missing_ok=True)
                except: pass
        if signed_ok and final_apk.exists():
            _verify_signature_schemes(apksigner, final_apk, logger)

    if not signed_ok:
        jarsigner = find_tool("jarsigner")
        if jarsigner and keystore and keystore.exists():
            logger.log("✍ Tentative jarsigner...")
            shutil.copy(aligned_apk, final_apk)
            signed_ok = run_cmd([jarsigner, "-verbose", "-sigalg", "SHA1withRSA",
                                  "-digestalg", "SHA1", "-keystore", str(keystore),
                                  "-storepass", ks_pass, "-keypass", key_pass,
                                  str(final_apk), alias], logger)

    if not signed_ok:
        raise RuntimeError(
            "Aucun signeur trouvé (apksigner/jarsigner). "
            "Installe JDK 17+ (https://adoptium.net) et le SDK Android build-tools, "
            "puis relance launcher.bat."
        )

    # Vérification intégrité ZIP finale
    try:
        with zipfile.ZipFile(final_apk) as zf:
            bad = zf.testzip()
            if bad: raise RuntimeError(f"Entrée corrompue: {bad}")
            # Normaliser les chemins (certains APKs utilisent "./AndroidManifest.xml")
            names = [n.lstrip('./') for n in zf.namelist()]
            if "AndroidManifest.xml" not in names:
                raise RuntimeError("AndroidManifest.xml absent — build invalide")
    except zipfile.BadZipFile:
        final_apk.unlink(missing_ok=True)
        raise RuntimeError("APK final corrompu (disque plein ou outil interrompu). Relance.")

    # Nettoyer keystore custom
    if signing_mode == "custom" and keystore and keystore.name == "custom.keystore":
        try: keystore.unlink()
        except: pass

    logger.log(f"✅ APK prêt : {final_apk.name}")
    return final_apk


def _verify_signature_schemes(apksigner, apk_path, logger):
    try:
        ok, out = run_cmd_capture([apksigner, "verify", "--verbose", str(apk_path)], logger)
        if not ok:
            logger.log("⚠ apksigner verify a échoué")
            return
        # BUG-m05 — apksigner > 34 préfixe certaines lignes avec "WARNING: " ce qui
        # cassait le pattern précédent → faux succès "Signature vérifiée" sans vérification réelle.
        # On utilise re.MULTILINE + un pattern qui ignore les préfixes éventuels.
        schemes = dict(re.findall(
            r"(?:^|\n)[^\n]*?Verified using (v[\d.]+) scheme[^\n]*?:\s*(true|false)",
            out, re.IGNORECASE
        ))
        missing = [s for s, v in schemes.items() if v.lower() == "false" and s in ("v1", "v2", "v3")]
        if not schemes:
            logger.log("⚠ Vérification signature : impossible de lire les schémas (sortie apksigner non standard)")
        elif missing:
            logger.log(f"⚠ Schémas de signature manquants : {', '.join(missing)}")
        else:
            active = [s for s, v in schemes.items() if v.lower() == "true"]
            logger.log(f"✅ Signature vérifiée : {'/'.join(sorted(active))} actifs")
    except Exception as e:
        logger.log(f"⚠ Vérification non concluante: {e}")


# =============================================================
# KEYSTORE PYTHON PUR
# =============================================================
def _ensure_cryptography(logger):
    try:
        import cryptography; return True
    except ImportError:
        logger.log("📦 Installation de 'cryptography'...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "cryptography", "--quiet"], check=True)
            logger.log("✅ cryptography installée"); return True
        except Exception as e:
            logger.log(f"❌ Impossible d'installer cryptography: {e}"); return False

def create_keystore_python(ks_path, alias, store_pass, key_pass, dname_str, validity_days, logger):
    if not _ensure_cryptography(logger): return False
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    import datetime
    from datetime import timezone

    logger.log("🔐 Génération clé RSA 2048...")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name_attrs = []
    for part in dname_str.split(","):
        part = part.strip()
        if "=" not in part: continue
        k, v = part.split("=", 1)
        k = k.strip().upper(); v = v.strip()
        if k == "CN":  name_attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, v))
        elif k == "O": name_attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, v))
        elif k == "C": name_attrs.append(x509.NameAttribute(NameOID.COUNTRY_NAME, v))
    if not name_attrs:
        name_attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, "App"))
    subject = issuer = x509.Name(name_attrs)
    now = datetime.datetime.now(timezone.utc)  # aware datetime — requis par cryptography >= 41
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=validity_days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(private_key, hashes.SHA256()))
    p12 = pkcs12.serialize_key_and_certificates(
        name=alias.encode(), key=private_key, cert=cert, cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(store_pass.encode())
    )
    ks_path.write_bytes(p12)
    logger.log(f"✅ Keystore créé: {ks_path.name}"); return True


# =============================================================
# TEST SUR APPAREIL RÉEL VIA ADB
# =============================================================

ADB_CRASH_MARKERS = [
    r"FATAL EXCEPTION",
    r"AndroidRuntime.*FATAL",
    r"Process .* has died",
    r"java\.lang\.\w+Exception",
    r"android\.app\.ActivityThread\$\w+Exception",
    r"SIGSEGV",
    r"Fatal signal",
]

def adb_list_devices(adb_path):
    """Retourne la liste des appareils connectés et prêts (état 'device')."""
    try:
        r = subprocess.run([adb_path, "devices", "-l"],
                           capture_output=True, text=True, timeout=15)
        lines = (r.stdout or "").splitlines()
        devices = []
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                model = ""
                for p in parts[2:]:
                    if p.startswith("model:"):
                        model = p[6:].replace("_", " ")
                        break
                devices.append({"serial": serial, "model": model or serial})
        return devices
    except Exception:
        return []

def run_device_test(apk_path, package_name, logger, serial=None):
    """
    Installe l'APK sur un appareil/émulateur via adb, le lance, surveille
    le logcat pendant quelques secondes pour détecter un crash, puis désinstalle.
    """
    adb = find_tool("adb")
    if not adb:
        logger.log("❌ adb introuvable — installez le SDK Android (platform-tools)")
        logger.status = "error"
        return

    # Base command avec serial optionnel
    def adb_cmd(*args):
        base = [adb]
        if serial:
            base += ["-s", serial]
        return base + list(args)

    # 1 — Vérifier qu'un appareil est disponible
    logger.log("🔍 Vérification des appareils connectés...")
    devices = adb_list_devices(adb)
    if not devices:
        logger.log("❌ Aucun appareil connecté (adb devices ne retourne rien en état 'device')")
        logger.log("   → Branche un téléphone/émulateur et active le débogage USB")
        logger.status = "error"
        return

    target = serial or devices[0]["serial"]
    target_label = next((d["model"] for d in devices if d["serial"] == target), target)
    logger.log(f"📱 Appareil cible : {target_label} ({target})")

    # 2 — Installer l'APK
    logger.log(f"📦 Installation de {Path(apk_path).name}...")
    ok = run_cmd(adb_cmd("install", "-r", str(apk_path)), logger, timeout=120)
    if not ok:
        logger.log("❌ adb install a échoué — vérifiez la signature et la compatibilité SDK")
        logger.status = "error"
        return
    logger.log("✅ Installation réussie")

    # 3 — Lancer l'app via monkey (trouve le LAUNCHER sans connaître l'activité exacte)
    logger.log(f"🚀 Lancement de {package_name}...")
    run_cmd(adb_cmd("shell", "monkey", "-p", package_name,
                    "-c", "android.intent.category.LAUNCHER", "1"), logger, timeout=30)

    # 4 — Attendre quelques secondes puis lire logcat
    logger.log("⏳ Surveillance du logcat (5 s)...")
    time.sleep(5)

    try:
        r = subprocess.run(
            adb_cmd("logcat", "-d", "-t", "500"),
            capture_output=True, text=True, timeout=20
        )
        logcat_out = r.stdout or ""
    except Exception as e:
        logcat_out = ""
        logger.log(f"⚠ Impossible de lire le logcat: {e}")

    # Filtrer les lignes concernant notre package
    relevant_lines = [
        line for line in logcat_out.splitlines()
        if package_name in line or any(
            re.search(m, line) for m in ADB_CRASH_MARKERS
        )
    ]
    crash_lines = [
        line for line in relevant_lines
        if any(re.search(m, line) for m in ADB_CRASH_MARKERS)
    ]

    # 5 — Désinstaller
    logger.log(f"🗑 Désinstallation de {package_name}...")
    run_cmd(adb_cmd("shell", "pm", "uninstall", package_name), logger, timeout=30)

    # 6 — Résultat
    if crash_lines:
        logger.log("❌ CRASH DÉTECTÉ lors du lancement :")
        for line in crash_lines[:20]:
            logger.log(f"   {line}")
        logger.log("💡 Consulte le logcat complet pour le stack trace détaillé")
        logger.status = "error"
        logger._result = {"crashed": True, "crashLines": crash_lines[:20]}
    else:
        logger.log("✅ Aucun crash détecté — l'application s'est lancée normalement")
        if relevant_lines:
            logger.log(f"   ({len(relevant_lines)} ligne(s) de log liées au package)")
        logger.status = "done"
        logger._result = {"crashed": False}


# =============================================================
# PIPELINE COMPLET — BUILD DEPUIS ZÉRO
# =============================================================
def do_build_scratch(config, icon_bytes, splash_bytes, site_zip_bytes):
    """Build APK sans template (mode scratch)."""
    logger = OPS["legacy"]
    logger.lines = []; logger.status = "building"; logger.result_file = None
    keep_session = bool(config.get("keepSession"))
    sid = None
    try:
        sid = create_scratch_session(config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        logger.session = sid

        app_name     = (config.get("appName") or "MyApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}.apk"
        signing = config.get("signing", {"mode": "debug"})

        final_apk = recompile_session(sid, signing, out_name, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
        if keep_session:
            logger.log(f"💾 Session conservée: {sid}")
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"
    finally:
        if sid and not keep_session:
            delete_session(sid)

def do_build_legacy(config, template_bytes, icon_bytes, splash_bytes, site_zip_bytes):
    """Build APK depuis un template (décompile + patch + recompile)."""
    logger = OPS["legacy"]
    logger.lines = []; logger.status = "building"; logger.result_file = None
    keep_session = bool(config.get("keepSession"))
    sid = None
    try:
        if template_bytes:
            sid, _ = decompile_to_session(template_bytes, logger)
        else:
            # Pas de template → utilise le mode scratch sans réinitialiser le logger
            logger.log("ℹ Aucun template fourni → génération from scratch")
            keep_session = bool(config.get("keepSession"))
            try:
                sid = create_scratch_session(config, icon_bytes, splash_bytes, site_zip_bytes, logger)
                logger.session = sid
                app_name     = (config.get("appName") or "MyApp").strip()
                version_name = str(config.get("versionName") or "1.0")
                out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}.apk"
                signing = config.get("signing", {"mode": "debug"})
                final_apk = recompile_session(sid, signing, out_name, logger)
                logger.result_file = str(final_apk)
                logger.status = "done"
                if keep_session:
                    logger.log(f"💾 Session conservée: {sid}")
            except Exception as e:
                import traceback
                logger.log(f"❌ Erreur: {e}")
                logger.log(traceback.format_exc())
                logger.status = "error"
            finally:
                if sid and not keep_session:
                    delete_session(sid)
            return
        logger.session = sid
        apply_config(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        app_name     = (config.get("appName") or "MyApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}.apk"
        signing = config.get("signing", {"mode": "debug"})
        final_apk = recompile_session(sid, signing, out_name, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
        if keep_session: logger.log(f"💾 Session conservée: {sid}")
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}"); logger.log(traceback.format_exc())
        logger.status = "error"
    finally:
        if sid and not keep_session:
            delete_session(sid)


# =============================================================
# HTTP HANDLER
# =============================================================
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_cors(); self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def _err(self, msg, code=400): self._json({"error": str(msg)}, code)

    def do_OPTIONS(self):
        self.send_response(200); self.send_cors(); self.end_headers()

    def do_GET(self):
        global CURRENT_SESSION
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            html_file = BASE_DIR / "builder.html"
            if html_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_cors(); self.end_headers()
                self.wfile.write(html_file.read_bytes())
            else:
                self.send_response(404); self.end_headers()
                self.wfile.write(b"builder.html not found")
            return

        if path == "/status":
            token = qs.get("session", ["legacy"])[0]
            op = get_op(token)
            self._json({
                "status": op.status,
                "logs":   op.lines[-200:],
                "file":   os.path.basename(op.result_file) if op.result_file else None,
                "session": op.session,
                "result": getattr(op, '_result', None),
            })
            return

        if path.startswith("/download/"):
            filename = unquote(path.replace("/download/", ""))
            filepath = OUTPUT_DIR / filename
            if filepath.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.android.package-archive")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(filepath.stat().st_size))
                self.send_cors(); self.end_headers()
                self.wfile.write(filepath.read_bytes())
            else:
                self.send_response(404); self.end_headers()
            return

        if path == "/check":
            bt = find_build_tools_version()
            # BUG-M04 — vérifier que Java est >= 11 (apktool requiert Java 11+)
            # find_tool("java") retourne True même si c'est Java 8 — badge vert trompeur
            java_path = find_tool("java")
            java_ok = False
            java_version_str = None
            if java_path:
                try:
                    import subprocess as _sp
                    r = _sp.run([java_path, "-version"], capture_output=True, text=True, timeout=10)
                    ver_out = (r.stderr or r.stdout or "")
                    m = re.search(r'version "([^"]+)"', ver_out)
                    if m:
                        java_version_str = m.group(1)
                        # Java 8 → "1.8.x", Java 11+ → "11.x", "17.x"...
                        major = java_version_str.split(".")[0]
                        if major == "1":
                            # legacy versioning: 1.8 → Java 8
                            minor = java_version_str.split(".")[1] if "." in java_version_str else "0"
                            java_ok = int(minor) >= 11
                        else:
                            java_ok = int(major) >= 11
                except Exception:
                    java_ok = bool(java_path)  # fallback
            adb_path = find_tool("adb")
            adb_devices = adb_list_devices(adb_path) if adb_path else []
            self._json({
                "java":                 java_ok,
                "javaPresent":          bool(java_path),
                "javaVersion":          java_version_str,
                "apktool":              bool(find_tool("apktool")),
                "zipalign":             bool(find_tool("zipalign")),
                "apksigner":            bool(find_tool("apksigner")),
                "jarsigner":            bool(find_tool("jarsigner")),
                "template":             (BASE_DIR / "template.apk").exists(),
                "keystore":             (TOOLS_DIR / "debug.keystore").exists(),
                "customKeystoreExists": (TOOLS_DIR / "mon.keystore").exists(),
                "customKeystoreName":   "mon.keystore" if (TOOLS_DIR / "mon.keystore").exists() else None,
                "buildToolsVersion":    bt,
                "sdkPresent":           SDK_DIR.exists(),
                "scratchMode":          True,
                "adb":                  bool(adb_path),
                "adbDevices":           adb_devices,
            })
            return

        if path == "/sessions":
            self._json({"sessions": list_sessions()})
            return

        # ── /list-output ─────────────────────────────────────────────────────
        # Retourne tous les APKs dans output/, triés par date de modification décroissante.
        # Utilisé par le frontend pour détecter l'apparition d'un APK signé manuellement.
        if path == "/list-output":
            try:
                apks = []
                if OUTPUT_DIR.exists():
                    for f in OUTPUT_DIR.iterdir():
                        if f.suffix.lower() == ".apk" and f.is_file():
                            apks.append({
                                "name":    f.name,
                                "size":    f.stat().st_size,
                                "mtime":   f.stat().st_mtime,
                                "signed":  "_signed" in f.name,
                            })
                apks.sort(key=lambda x: x["mtime"], reverse=True)
                self._json({"files": apks})
            except Exception as e:
                self._err(str(e), 500)
            return

        if path == "/tree":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            rel = qs.get("path", [""])[0]
            if not sid:
                self._json({"loaded": False, "items": []}); return
            if not rel:
                try:
                    items = list_tree_all(sid)
                    self._json({"loaded": True, "items": items})
                except Exception as e:
                    self._json({"loaded": False, "items": [], "error": str(e)})
            else:
                try:
                    sdir, target = _safe_target(sid, rel)
                    if not target.exists(): raise FileNotFoundError()
                    if target.is_file(): target = target.parent
                    entries = []
                    for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                        entries.append({"name": p.name, "type": "dir" if p.is_dir() else "file",
                                        "size": p.stat().st_size if p.is_file() else None,
                                        "path": str(p.relative_to(sdir)).replace("\\", "/")})
                    self._json({"entries": entries})
                except Exception as e:
                    self._err(e, 404)
            return

        if path == "/file":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            rel = qs.get("path", [""])[0]
            if not sid: self._err("Aucun projet chargé", 404); return
            try:
                data = read_file_safe(sid, rel)
                if data.get("text"):
                    self._json({"type": "text", "content": data["content"], "size": data.get("size", 0)})
                else:
                    ext = (data.get("ext") or os.path.splitext(rel)[1]).lower()
                    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                        try:
                            raw = read_binary_raw(sid, rel)
                            b64 = base64.b64encode(raw).decode()
                            mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                                        ".jpeg": "image/jpeg", ".gif": "image/gif",
                                        ".webp": "image/webp"}
                            mime = mime_map.get(ext, "image/png")
                            self._json({"type": "image", "content": b64, "mime": mime, "size": data.get("size", 0)})
                        except: self._json({"type": "binary", "reason": "binary", "size": data.get("size", 0)})
                    else:
                        self._json({"type": "binary", "reason": data.get("reason", "binary"), "size": data.get("size", 0)})
            except Exception as e:
                self._err(e, 404)
            return

        if path == "/file-raw":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            rel = qs.get("path", [""])[0]
            try:
                data = read_binary_raw(sid, rel)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_cors(); self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._err(e, 404)
            return

        # ── /list-images : retourne toutes les images PNG/JPG/WEBP de la session ──
        # Utilisé par le sélecteur de splash dans le formulaire pour montrer
        # toutes les images existantes de l'APK et permettre de choisir lesquelles
        # remplacer plutôt que de chercher à l'aveugle par nom de fichier.
        if path == "/list-images":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._json({"images": []}); return
            try:
                sdir_v3 = source_dir(sid)
                sdir_v2 = session_dir(sid) / "decompiled"
                src = sdir_v3 if sdir_v3.exists() else sdir_v2
                images = []
                IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                if src.exists():
                    for f in sorted(src.rglob("*")):
                        if not f.is_file(): continue
                        if f.suffix.lower() not in IMAGE_EXTS: continue
                        rel = str(f.relative_to(src)).replace("\\", "/")
                        try:
                            raw = f.read_bytes()
                            b64 = base64.b64encode(raw).decode()
                            ext = f.suffix.lower().lstrip(".")
                            mime_map = {"png": "image/png", "jpg": "image/jpeg",
                                        "jpeg": "image/jpeg", "webp": "image/webp",
                                        "gif": "image/gif"}
                            mime = mime_map.get(ext, "image/png")
                            images.append({
                                "path": rel,
                                "name": f.name,
                                "size": f.stat().st_size,
                                "mime": mime,
                                "data": b64,
                            })
                        except Exception:
                            pass
                self._json({"images": images, "count": len(images)})
            except Exception as e:
                self._err(str(e), 500)
            return

        # ── /bug-log : journal persistant (logs auto + bugs signalés) ───────
        # + état de santé agrégé (vert/jaune/rouge) pour l'indicateur de l'UI.
        if path == "/bug-log":
            try:
                entries = list(reversed(_buglog_read()))  # plus récent en premier
                health = buglog_compute_health()
                self._json({"entries": entries, "health": health})
            except Exception as e:
                self._err(e, 500)
            return

        self.send_response(404); self.end_headers()

    def do_DELETE(self):
        global CURRENT_SESSION
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/session":
            sid = qs.get("session", [""])[0]
            delete_session(sid); OPS.pop(sid, None)
            if CURRENT_SESSION == sid: CURRENT_SESSION = None
            self._json({"deleted": sid}); return
        if parsed.path == "/file":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            rel = qs.get("path", [""])[0]
            if not sid or not rel: self._err("Paramètres manquants", 400); return
            try:
                _, target = _safe_target(sid, rel)
                if target.is_dir(): shutil.rmtree(target)
                elif target.is_file(): target.unlink()
                self._json({"deleted": rel})
            except Exception as e:
                self._err(e, 500)
            return
        # ── /bug-log : vide tout le journal ──────────────────────────────────
        if parsed.path == "/bug-log":
            with _buglog_lock:
                _buglog_write([])
            self._json({"cleared": True})
            return
        # ── /bug-log-entry?id=... : supprime/marque résolue une entrée précise ──
        if parsed.path == "/bug-log-entry":
            entry_id = qs.get("id", [""])[0]
            if not entry_id:
                self._err("id manquant", 400); return
            with _buglog_lock:
                entries = _buglog_read()
                entries = [e for e in entries if e.get("id") != entry_id]
                _buglog_write(entries)
            self._json({"deleted": entry_id})
            return
        self.send_response(404); self.end_headers()

    def _read_json_body(self, max_bytes=64 * 1024 * 1024):  # 64 MB max
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > max_bytes:
            # Drain pour ne pas bloquer le socket
            self.rfile.read(length)
            raise ValueError(f"Payload trop volumineux ({length} bytes, max {max_bytes})")
        try:
            body = self.rfile.read(length) if length else b"{}"
            return json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Corps JSON invalide: {e}")

    def do_POST(self):
        global CURRENT_SESSION
        path = self.path.split("?")[0]
        # Wrapper _read_json_body pour renvoyer 400 si payload invalide/trop grand
        _orig_read = self._read_json_body
        def _safe_read(max_bytes=64*1024*1024):
            try:
                return _orig_read(max_bytes)
            except ValueError as _ve:
                self._err(str(_ve), 400)
                raise
        self._read_json_body = _safe_read

        # ── /build-scratch : génère APK de ZÉRO, sans template ──────────────
        if path == "/build-scratch":
            op = OPS["legacy"]
            if op.status == "building":
                self._err("Build déjà en cours", 409); return
            payload = self._read_json_body()
            config        = payload.get("config", payload)
            try:
                icon_bytes    = _safe_b64(payload.get("icon"),    "icon")
                splash_bytes  = _safe_b64(payload.get("splash"),  "splash")
                site_zip_bytes= _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_scratch,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True})
            return

        # ── /build : génère APK depuis template OU from scratch ─────────────
        if path == "/build":
            op = OPS["legacy"]
            if op.status == "building":
                self._err("Build déjà en cours", 409); return
            payload        = self._read_json_body()
            config         = payload.get("config", payload)
            tmpl_b64       = payload.get("templateApk") or payload.get("apk")
            try:
                template_bytes = _safe_b64(tmpl_b64,               "templateApk")
                icon_bytes     = _safe_b64(payload.get("icon"),     "icon")
                splash_bytes   = _safe_b64(payload.get("splash"),   "splash")
                site_zip_bytes = _safe_b64(payload.get("siteZip"),  "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_legacy,
                args=(config, template_bytes, icon_bytes, splash_bytes, site_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True})
            return

        # ── /import : décompile un APK uploadé (Mode Dev) ───────────────────
        if path == "/import":
            op = OPS["legacy"]
            if op.status == "building":
                self._err("Décompilation déjà en cours", 409); return
            payload    = self._read_json_body()
            apk_b64    = payload.get("apk") or payload.get("templateApk")
            tmpl_bytes = base64.b64decode(apk_b64) if apk_b64 else None
            op.lines = []; op.status = "building"; op.result_file = None; op.session = None

            def _do_import():
                global CURRENT_SESSION
                try:
                    sid, pkg_old = decompile_to_session(tmpl_bytes, op)
                    CURRENT_SESSION = sid; op.session = sid; op.status = "done"
                except Exception as e:
                    import traceback
                    op.log(f"❌ {e}"); op.log(traceback.format_exc())
                    op.status = "error"
            threading.Thread(target=_do_import, daemon=True).start()
            self._json({"started": True})
            return

        # ── /create-scratch-session : crée une session source sans template ──
        if path == "/create-scratch-session":
            global CURRENT_SESSION
            op = OPS["legacy"]
            if op.status == "building":
                self._err("Opération déjà en cours", 409); return
            payload = self._read_json_body()
            config        = payload.get("config", payload)
            icon_bytes    = base64.b64decode(payload["icon"])    if payload.get("icon")    else None
            splash_bytes  = base64.b64decode(payload["splash"])  if payload.get("splash")  else None
            site_zip_bytes= base64.b64decode(payload["siteZip"]) if payload.get("siteZip") else None
            op.lines = []; op.status = "building"; op.result_file = None; op.session = None

            def _do_create():
                global CURRENT_SESSION
                try:
                    sid = create_scratch_session(config, icon_bytes, splash_bytes, site_zip_bytes, op)
                    CURRENT_SESSION = sid; op.session = sid; op.status = "done"
                except Exception as e:
                    import traceback
                    op.log(f"❌ {e}"); op.log(traceback.format_exc())
                    op.status = "error"
            threading.Thread(target=_do_create, daemon=True).start()
            self._json({"started": True})
            return

        # ── /apply : applique config sur la session courante ─────────────────
        if path == "/apply":
            if not CURRENT_SESSION:
                self._err("Aucun projet chargé", 400); return
            op = OPS["legacy"]
            if op.status == "building":
                self._err("Opération déjà en cours", 409); return
            payload = self._read_json_body()
            config        = payload.get("config", payload)
            icon_bytes    = base64.b64decode(payload["icon"])    if payload.get("icon")    else None
            splash_bytes  = base64.b64decode(payload["splash"])  if payload.get("splash")  else None
            site_zip_bytes= base64.b64decode(payload["siteZip"]) if payload.get("siteZip") else None
            op.lines = []; op.status = "building"; op.result_file = None
            sid_snap = CURRENT_SESSION

            def _do_apply():
                try:
                    apply_config(sid_snap, config, icon_bytes, splash_bytes, site_zip_bytes, op)
                    op.status = "done"
                except Exception as e:
                    import traceback
                    op.log(f"❌ {e}"); op.log(traceback.format_exc())
                    op.status = "error"
            threading.Thread(target=_do_apply, daemon=True).start()
            self._json({"started": True})
            return

        # ── /recompile : recompile la session courante ───────────────────────
        if path == "/recompile":
            if not CURRENT_SESSION:
                self._err("Aucun projet chargé", 400); return
            op = OPS["legacy"]
            if op.status == "building":
                self._err("Opération déjà en cours", 409); return
            payload = self._read_json_body()
            signing  = payload.get("signing",  {"mode": "debug"})
            out_name = payload.get("outName",  "output.apk")
            sid_snap = CURRENT_SESSION
            op.lines = []; op.status = "building"; op.result_file = None

            def _do_recompile():
                try:
                    final_apk = recompile_session(sid_snap, signing, out_name, op)
                    op.result_file = str(final_apk); op.status = "done"
                except Exception as e:
                    import traceback
                    op.log(f"❌ {e}"); op.log(traceback.format_exc())
                    op.status = "error"
            threading.Thread(target=_do_recompile, daemon=True).start()
            self._json({"started": True})
            return

        # ── /select-session ──────────────────────────────────────────────────
        if path == "/select-session":
            payload = self._read_json_body()
            sid = payload.get("session")
            if not sid or not session_dir(sid).exists():
                self._err("Session introuvable", 404); return
            CURRENT_SESSION = sid
            self._json({"selected": sid}); return

        # ── /save-file ────────────────────────────────────────────────────────
        if path == "/save-file":
            payload = self._read_json_body()
            sid     = payload.get("session") or CURRENT_SESSION
            rel     = payload.get("path", "")
            content = payload.get("content", "")
            if not sid: self._err("Aucun projet chargé", 400); return
            try:
                write_file_safe(sid, rel, content)
                self._json({"saved": rel})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /upload-file (binaire dans assets/) ──────────────────────────────
        if path == "/upload-file":
            payload  = self._read_json_body()
            sid      = payload.get("session") or CURRENT_SESSION
            rel      = payload.get("path", "")
            data_b64 = payload.get("data", "")
            if not sid: self._err("Aucun projet chargé", 400); return
            try:
                write_binary_safe(sid, rel, base64.b64decode(data_b64))
                self._json({"saved": rel})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /replace-images : remplace une liste de chemins d'images précis ──
        # Reçoit { session, paths: ["res/drawable/splash.png", ...], data: "<b64>" }
        # Écrit le même contenu image dans chaque chemin sélectionné.
        # Convertit automatiquement en PNG si Pillow est disponible.
        if path == "/replace-images":
            payload  = self._read_json_body()
            sid      = payload.get("session") or CURRENT_SESSION
            paths    = payload.get("paths", [])   # liste de chemins relatifs
            data_b64 = payload.get("data", "")
            if not sid:   self._err("Aucun projet chargé", 400); return
            if not paths: self._err("Aucun chemin fourni", 400); return
            if not data_b64: self._err("Aucune image fournie", 400); return
            try:
                raw = base64.b64decode(data_b64)
                # Conversion PNG si nécessaire
                dummy_logger = OpLogger()
                raw_png = ensure_png_bytes(raw, dummy_logger, "splash")
                replaced = 0
                errors = []
                for rel in paths:
                    try:
                        write_binary_safe(sid, rel, raw_png)
                        replaced += 1
                    except Exception as e:
                        errors.append(f"{rel}: {e}")
                self._json({"replaced": replaced, "errors": errors})
            except Exception as e:
                self._err(str(e), 500)
            return

        # ── /create-keystore ─────────────────────────────────────────────────
        # CRITIQUE #1 (cf. rapport de bugs) : un keystore de prod ne doit JAMAIS
        # être régénéré silencieusement — sinon chaque nouveau build signe avec
        # une identité différente et Android refuse la mise à jour par-dessus
        # l'app déjà installée ("signatures do not match" / package en conflit).
        # Comportement désormais : si le fichier existe déjà, on le RÉUTILISE
        # (on le relit et on le renvoie en base64) au lieu de le supprimer,
        # sauf si l'utilisateur a explicitement demandé un nouveau keystore
        # (payload.forceNew = true), ce qui doit rester une action volontaire
        # et avertie (l'UI affiche un avertissement avant de l'envoyer).
        if path == "/create-keystore":
            payload      = self._read_json_body()
            ks_name      = payload.get("name",       "mon.keystore")
            alias        = payload.get("alias",      "monapp")
            store_pass   = payload.get("storePass",  "android")
            key_pass     = payload.get("keyPass",    "android")
            dname        = payload.get("dname",      "CN=App,O=App,C=US")
            validity     = int(payload.get("validity", 10000))
            force_new    = bool(payload.get("forceNew", False))
            logger       = get_op("keystore-" + new_session_id())
            logger.status = "building"

            def _do_ks():
                try:
                    ks_path = TOOLS_DIR / ks_name

                    # ── Réutilisation : le keystore existe déjà et on ne force pas
                    # sa régénération → on vérifie d'abord que le mot de passe
                    # fourni est correct avant de le renvoyer (BUG-KS01).
                    if ks_path.exists() and not force_new:
                        # Vérification du mot de passe avec keytool -list
                        keytool_check = find_tool("keytool")
                        pass_ok = False
                        if keytool_check:
                            try:
                                result = subprocess.run(
                                    [keytool_check, "-list", "-keystore", str(ks_path),
                                     "-storepass", store_pass, "-noprompt"],
                                    capture_output=True, text=True, timeout=15
                                )
                                pass_ok = result.returncode == 0
                            except Exception:
                                pass_ok = False
                        else:
                            # keytool absent : on ne peut pas vérifier, on laisse passer
                            pass_ok = True

                        if not pass_ok:
                            logger.log(f"❌ Mot de passe incorrect pour le keystore existant '{ks_name}'.")
                            logger.log("💡 Solutions : (1) Entre le bon mot de passe, "
                                       "(2) Coche 'Régénérer un nouveau keystore' pour en créer un nouveau.")
                            logger.status = "error"
                            logger._result = {"error": "wrong_password",
                                              "message": f"Le mot de passe fourni ne correspond pas au keystore existant '{ks_name}'. "
                                                         "Utilise le bon mot de passe ou coche 'Régénérer un nouveau keystore'."}
                            return

                        ks_b64 = base64.b64encode(ks_path.read_bytes()).decode()
                        logger.log(f"♻️ Keystore existant réutilisé: {ks_name} "
                                   f"(alias demandé: {alias})")
                        logger.log("ℹ️ Aucune nouvelle clé générée — la signature "
                                    "restera compatible avec les installations précédentes.")
                        logger._result = {"created": False, "reused": True,
                                          "keystoreName": ks_name,
                                          "keystoreB64": ks_b64, "alias": alias,
                                          "storePass": store_pass, "keyPass": key_pass}
                        logger.status = "done"
                        return

                    if ks_path.exists() and force_new:
                        logger.log(f"⚠️ Régénération forcée demandée — l'ancien "
                                   f"keystore '{ks_name}' va être remplacé. Les "
                                   f"mises à jour vers les APK déjà installés signés "
                                   f"avec l'ancienne clé ne seront plus possibles.")
                        ks_path.unlink()

                    keytool = find_tool("keytool")
                    ok = False
                    if keytool:
                        cmd = [keytool, "-genkey", "-v", "-keystore", str(ks_path),
                               "-alias", alias, "-keyalg", "RSA", "-keysize", "2048",
                               "-validity", str(validity), "-storepass", store_pass,
                               "-keypass", key_pass, "-dname", dname, "-noprompt"]
                        ok = run_cmd(cmd, logger)
                    if not ok or not ks_path.exists():
                        ok = create_keystore_python(ks_path, alias, store_pass, key_pass, dname, validity, logger)
                    if ok and ks_path.exists():
                        ks_b64 = base64.b64encode(ks_path.read_bytes()).decode()
                        logger.log(f"✅ Nouveau keystore créé: {ks_name}")
                        logger._result = {"created": True, "reused": False, "keystoreName": ks_name,
                                          "keystoreB64": ks_b64, "alias": alias,
                                          "storePass": store_pass, "keyPass": key_pass}
                        logger.status = "done"
                    else:
                        logger.log("❌ Échec création keystore")
                        logger.status = "error"
                except Exception as e:
                    import traceback
                    logger.log(f"❌ {e}"); logger.log(traceback.format_exc())
                    logger.status = "error"
            threading.Thread(target=_do_ks, daemon=True).start()
            token = f"keystore-{new_session_id()}"
            OPS[token] = logger
            self._json({"started": True, "token": token})
            return

        # ── /sign-apk ────────────────────────────────────────────────────────
        if path == "/sign-apk":
            payload  = self._read_json_body()
            apk_name = payload.get("apkName", "")
            ks_b64   = payload.get("keystoreB64")
            alias    = payload.get("alias", "monapp")
            ks_pass  = payload.get("storePass", "android")
            key_pass = payload.get("keyPass", "android")
            if not apk_name: self._err("apkName manquant", 400); return
            apk_path = OUTPUT_DIR / apk_name
            if not apk_path.exists(): self._err(f"APK introuvable: {apk_name}", 404); return
            logger = get_op("sign-" + new_session_id())
            logger.status = "building"
            # Calculer le token AVANT de démarrer le thread — évite la race condition
            # où le thread peut s'exécuter et se terminer avant que le token soit calculé
            sign_token = f"sign-{new_session_id()}"
            OPS[sign_token] = logger
            ks_tmp = WORK_DIR / f"tmp_{apk_name}.keystore"
            if ks_b64: ks_tmp.write_bytes(base64.b64decode(ks_b64))
            else: ks_tmp = TOOLS_DIR / "debug.keystore"

            # Déterminer minSdkVersion depuis la session courante (cohérent avec
            # recompile_session) — sans ça apksigner peut choisir un jeu de
            # schémas de signature incohérent avec le manifest réel, ce qui
            # cause "Problème lors de l'analyse du paquet" à l'installation.
            effective_min_sdk_sign = "23"
            try:
                if CURRENT_SESSION:
                    sdir_v3 = source_dir(CURRENT_SESSION)
                    sdir_v2 = session_dir(CURRENT_SESSION) / "decompiled"
                    ssrc = sdir_v3 if sdir_v3.exists() else sdir_v2
                    mtxt = (ssrc / "AndroidManifest.xml").read_text(encoding="utf-8", errors="ignore")
                    m = re.search(r'android:minSdkVersion="(\d+)"', mtxt)
                    if m: effective_min_sdk_sign = m.group(1)
            except: pass

            def _do_sign():
                try:
                    signed_name = apk_name.replace(".apk", "_signed.apk")
                    signed_path = OUTPUT_DIR / signed_name
                    zipalign = find_tool("zipalign")
                    aligned  = WORK_DIR / f"tmp_{apk_name}_aligned.apk"
                    if zipalign:
                        aligned.unlink(missing_ok=True)
                        run_cmd([zipalign, "-f", "-v", "4", str(apk_path), str(aligned)], logger)
                    else:
                        aligned = apk_path
                        logger.log("⚠ zipalign absent — APK non aligné (peut être refusé par certains appareils)")

                    # ── Vérification keystore ────────────────────────────────
                    if not ks_tmp.exists():
                        logger.log("❌ Keystore introuvable.")
                        logger.log("💡 Solution : ouvre le panneau Signature (🔑) et crée ou uploade ton keystore.")
                        logger.status = "error"
                        return

                    # ── Vérification mot de passe keystore avant d'appeler apksigner ──
                    keytool_bin = find_tool("keytool")
                    if keytool_bin and ks_b64:  # seulement pour les keystores custom uploadés
                        try:
                            chk = subprocess.run(
                                [keytool_bin, "-list", "-keystore", str(ks_tmp),
                                 "-storepass", ks_pass, "-noprompt"],
                                capture_output=True, text=True, timeout=15
                            )
                            if chk.returncode != 0:
                                logger.log("❌ Mot de passe keystore incorrect.")
                                logger.log("💡 Solution : entre le mot de passe exact choisi lors de la création du keystore.")
                                logger.log("💡 Si tu l'as oublié, coche 'Régénérer un nouveau keystore' dans le panneau Signature.")
                                logger.status = "error"
                                return
                        except Exception:
                            pass  # keytool check échoue → on laisse apksigner essayer

                    # ── Recherche apksigner ──────────────────────────────────
                    apksigner = find_tool("apksigner")

                    # ── Téléchargement automatique SDK si apksigner absent ───
                    if not apksigner:
                        logger.log("⚠ apksigner introuvable — tentative de téléchargement du SDK Android...")
                        try:
                            import urllib.request, zipfile as _zf, tempfile as _tmp
                            sdk_url = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
                            sdk_zip = TOOLS_DIR / "cmdline-tools.zip"
                            logger.log("📥 Téléchargement SDK Android command-line tools (~100 Mo)...")
                            urllib.request.urlretrieve(sdk_url, sdk_zip)
                            if sdk_zip.exists() and sdk_zip.stat().st_size > 10000:
                                extract_dest = SDK_DIR / "cmdline-tools"
                                extract_dest.mkdir(parents=True, exist_ok=True)
                                with _zf.ZipFile(sdk_zip) as zf:
                                    zf.extractall(extract_dest)
                                sdk_zip.unlink(missing_ok=True)
                                logger.log("📦 Extraction SDK terminée")
                                # Trouver sdkmanager
                                sdkmgr = None
                                for p in (
                                    extract_dest / "cmdline-tools" / "bin" / "sdkmanager.bat",
                                    extract_dest / "bin" / "sdkmanager.bat",
                                ):
                                    if p.exists(): sdkmgr = str(p); break
                                if sdkmgr:
                                    sdk_root = str(SDK_DIR)
                                    logger.log("📦 Installation build-tools 34.0.0 via sdkmanager...")
                                    subprocess.run(
                                        [sdkmgr, f"--sdk_root={sdk_root}", "--licenses"],
                                        input="y\ny\ny\ny\ny\ny\ny\n", text=True,
                                        capture_output=True, timeout=120
                                    )
                                    subprocess.run(
                                        [sdkmgr, f"--sdk_root={sdk_root}", "build-tools;34.0.0"],
                                        capture_output=True, timeout=300
                                    )
                                    apksigner = find_tool("apksigner")
                                    if apksigner:
                                        logger.log("✅ apksigner installé avec succès")
                                    else:
                                        logger.log("⚠ build-tools installé mais apksigner toujours introuvable")
                                else:
                                    logger.log("⚠ sdkmanager introuvable après extraction")
                            else:
                                logger.log("⚠ Téléchargement SDK échoué (réseau ou taille invalide)")
                        except Exception as sdk_err:
                            logger.log(f"⚠ Téléchargement SDK échoué: {sdk_err}")

                    if not apksigner:
                        logger.log("❌ apksigner toujours introuvable après tentative d'installation.")
                        logger.log("💡 Solution manuelle : lance launcher.bat pour installer le SDK Android (Java 17+ requis).")
                        logger.log("💡 Ou télécharge manuellement : https://developer.android.com/studio#command-tools")
                        logger.status = "error"
                        return

                    # ── Signature ────────────────────────────────────────────
                    kp_arg, kp_file = _pass_arg(ks_pass, WORK_DIR, f"sign_{apk_name}.pass")
                    kk_arg, kk_file = _pass_arg(key_pass, WORK_DIR, f"sign_{apk_name}.kpass")
                    min_sdk_int_sign = int(effective_min_sdk_sign) if effective_min_sdk_sign.isdigit() else 23
                    enable_v4_sign = min_sdk_int_sign >= 30
                    cmd = [apksigner, "sign", "--ks", str(ks_tmp),
                           "--ks-pass", kp_arg, "--key-pass", kk_arg,
                           "--ks-key-alias", alias,
                           "--min-sdk-version", effective_min_sdk_sign,
                           "--v1-signing-enabled", "true",
                           "--v2-signing-enabled", "true",
                           "--v3-signing-enabled", "true",
                           "--v4-signing-enabled", "true" if enable_v4_sign else "false",
                           "--out", str(signed_path), str(aligned)]
                    try:
                        ok = run_cmd(cmd, logger)
                    finally:
                        for f in (kp_file, kk_file):
                            try: f.unlink(missing_ok=True)
                            except: pass

                    if ok and signed_path.exists():
                        _verify_signature_schemes(apksigner, signed_path, logger)
                        logger._result = {"signed": signed_name}
                        logger.status = "done"
                        return

                    # apksigner a échoué → diagnostic
                    logger.log("❌ Signature échouée.")
                    logger.log("💡 Causes fréquentes : mot de passe incorrect, alias invalide, keystore corrompu.")
                    logger.log("💡 Coche 'Régénérer un nouveau keystore' dans le panneau Signature pour repartir de zéro.")
                    logger.status = "error"

                except Exception as e:
                    import traceback
                    logger.log(f"❌ {e}"); logger.log(traceback.format_exc())
                    logger.status = "error"
                finally:
                    if ks_b64 and ks_tmp.exists():
                        try: ks_tmp.unlink()
                        except: pass
                    aligned_path = WORK_DIR / f"tmp_{apk_name}_aligned.apk"
                    if aligned_path.exists() and aligned_path != apk_path:
                        try: aligned_path.unlink()
                        except: pass
            threading.Thread(target=_do_sign, daemon=True).start()
            self._json({"started": True, "token": sign_token})
            return


        # ── /adb-devices ─────────────────────────────────────────────────────
        if path == "/adb-devices":
            adb_path = find_tool("adb")
            if not adb_path:
                self._json({"adb": False, "devices": [], "error": "adb introuvable — SDK platform-tools non installé"})
                return
            devices = adb_list_devices(adb_path)
            self._json({"adb": True, "devices": devices})
            return

        # ── /device-test ──────────────────────────────────────────────────────
        if path == "/device-test":
            payload      = self._read_json_body()
            apk_name     = payload.get("apkName", "")
            package_name = payload.get("packageName", "")
            serial       = payload.get("serial") or None
            if not apk_name:
                self._err("apkName manquant", 400); return
            if not package_name:
                self._err("packageName manquant", 400); return
            apk_path = OUTPUT_DIR / apk_name
            if not apk_path.exists():
                self._err(f"APK introuvable: {apk_name}", 404); return

            token = "devtest-" + new_session_id()
            logger = OpLogger()
            logger.status = "building"
            OPS[token] = logger

            def _do_device_test():
                run_device_test(str(apk_path), package_name, logger, serial=serial)

            threading.Thread(target=_do_device_test, daemon=True).start()
            self._json({"started": True, "token": token})
            return

        # ── /new-folder ──────────────────────────────────────────────────────
        if path in ("/new_folder", "/new-folder"):
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            rel = (payload.get("path") or "").strip("/\\")
            if not sid or not rel: self._err("session/path manquant", 400); return
            try:
                _, target = _safe_target(sid, rel)
                target.mkdir(parents=True, exist_ok=True)
                self._json({"created": rel})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /rename ──────────────────────────────────────────────────────────
        if path == "/rename":
            payload  = self._read_json_body()
            sid      = payload.get("session") or CURRENT_SESSION
            rel      = (payload.get("path") or "").strip("/\\")
            new_rel  = (payload.get("newPath") or "").strip("/\\")
            if not sid or not rel or not new_rel: self._err("Paramètres manquants", 400); return
            try:
                sdir, src_p = _safe_target(sid, rel)
                _, dst_p    = _safe_target(sid, new_rel)
                if not src_p.exists(): self._err("Source introuvable", 404); return
                if dst_p.exists(): self._err("Destination déjà existante", 409); return
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                src_p.rename(dst_p)
                self._json({"renamed": new_rel})
            except PermissionError as e:
                self._err(e, 400)
            except Exception as e:
                self._err(e, 500)
            return

        # ── /duplicate ────────────────────────────────────────────────────────
        if path == "/duplicate":
            payload  = self._read_json_body()
            sid      = payload.get("session") or CURRENT_SESSION
            rel      = (payload.get("path") or "").strip("/\\")
            new_rel  = (payload.get("newPath") or "").strip("/\\")
            if not sid or not rel or not new_rel: self._err("Paramètres manquants", 400); return
            try:
                sdir, src_p = _safe_target(sid, rel)
                _, dst_p    = _safe_target(sid, new_rel)
                if not src_p.exists(): self._err("Source introuvable", 404); return
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                if src_p.is_dir():
                    shutil.copytree(str(src_p), str(dst_p))
                else:
                    shutil.copy2(str(src_p), str(dst_p))
                self._json({"duplicated": new_rel})
            except PermissionError as e:
                self._err(e, 400)
            except Exception as e:
                self._err(e, 500)
            return

        # ── /scan-urls ────────────────────────────────────────────────────────
        if path == "/scan-urls":
            sid = CURRENT_SESSION
            if not sid: self._err("Aucun projet chargé", 400); return
            try:
                sdir_v3 = source_dir(sid)
                sdir_v2 = session_dir(sid) / "decompiled"
                src_dir = sdir_v3 if sdir_v3.exists() else sdir_v2
                url_counts = {}
                for smali_f in src_dir.rglob("*.smali"):
                    try:
                        content = smali_f.read_text(encoding="utf-8", errors="ignore")
                        for url in re.findall(r'https?://[^\s"\'\\>]+', content):
                            url = url.rstrip(".,;)")
                            if len(url) > 8:
                                url_counts[url] = url_counts.get(url, 0) + 1
                    except Exception:
                        pass
                urls_sorted = sorted(url_counts.items(), key=lambda x: -x[1])
                self._json({"urls": [{"url": u, "count": c} for u, c in urls_sorted[:50]]})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /sign-manual-cmd ──────────────────────────────────────────────────
        if path == "/sign-manual-cmd":
            payload  = self._read_json_body()
            apk_name = payload.get("apkName", "")
            ks_type  = payload.get("keystoreType", "debug")  # "debug" or "custom"
            if not apk_name: self._err("apkName manquant", 400); return
            apk_path = OUTPUT_DIR / apk_name
            if not apk_path.exists(): self._err(f"APK introuvable: {apk_name}", 404); return
            bt = find_build_tools_version()
            bt_path = str(SDK_DIR / "build-tools" / bt) if bt else ""
            ks_custom = TOOLS_DIR / "mon.keystore"
            ks_dbg    = TOOLS_DIR / "debug.keystore"

            # Choisir le keystore selon ks_type
            if ks_type == "custom" and ks_custom.exists():
                ks_used  = str(ks_custom)
                ks_alias = "monapp"
            else:
                ks_used  = str(ks_dbg)
                ks_alias = "androiddebugkey"

            zipalign_exe  = os.path.join(bt_path, "zipalign.exe")  if bt_path else ""
            apksigner_bat = os.path.join(bt_path, "apksigner.bat") if bt_path else ""

            # Noms des fichiers intermédiaires/sortie
            # Nettoyer les suffixes _signed/_aligned pour éviter _signed_signed_aligned etc.
            apk_base = apk_path.stem
            for suffix in ("_signed", "_aligned", "_signed_aligned", "_aligned_signed"):
                if apk_base.endswith(suffix):
                    apk_base = apk_base[:-len(suffix)]
                    break
            aligned_apk = str(OUTPUT_DIR / (apk_base + "_aligned.apk"))
            signed_apk  = str(OUTPUT_DIR / (apk_base + "_release_signed.apk"))

            # Construire le script .bat temporaire
            bat_lines = [
                "@echo off",
                "chcp 65001 >nul",
                "setlocal EnableDelayedExpansion",
                "title APK Factory - Signature manuelle",
                "color 0A",
                f'cd /d "{str(BASE_DIR)}"',
                "echo.",
                "echo  ========================================================",
                "echo    APK Factory - Signature manuelle",
                "echo  ========================================================",
                "echo.",
                f'echo  APK source   : {str(apk_path)}',
                f'echo  APK aligne   : {aligned_apk}',
                f'echo  APK signe    : {signed_apk}',
                f'echo  Keystore     : {ks_used}',
                f'echo  Alias        : {ks_alias}',
                f'echo  Type         : {"DEBUG (pass: android)" if ks_type != "custom" else "CUSTOM (votre mot de passe)"}',
                "echo.",
            ]

            # Etape 0 : créer un keystore custom si absent
            if ks_type == "custom" and not ks_custom.exists():
                bat_lines += [
                    "echo  ── ETAPE 0 : Creer le keystore de production ─────────────",
                    "echo.",
                    f'echo    keytool -genkey -v -keystore "{ks_used}" -alias {ks_alias} -keyalg RSA -keysize 2048 -validity 10000',
                    "echo.",
                    "echo  Copie-colle la commande ci-dessus pour creer le keystore, puis relance ce script.",
                    "echo.",
                ]

            # Etape 1 : zipalign
            if zipalign_exe and os.path.exists(zipalign_exe):
                bat_lines += [
                    "echo  ── ETAPE 1 : Zipalign ──────────────────────────────────────",
                    "echo.",
                    f'"{zipalign_exe}" -f -v 4 "{str(apk_path)}" "{aligned_apk}"',
                    "if not exist \"%~dp0\" goto :eof",
                    "if not exist \"" + aligned_apk + "\" (",
                    "    echo [ECHEC] zipalign a echoue",
                    "    goto :end",
                    ")",
                    "echo [OK] Zipalign termine",
                    "echo.",
                ]
            else:
                bat_lines += [
                    "echo  [WARN] zipalign introuvable - copie directe...",
                    f'copy /Y "{str(apk_path)}" "{aligned_apk}" >nul',
                    "echo.",
                ]

            # Etape 2 : signature
            # Pour le keystore DEBUG : mot de passe fixe "android" — pas de saisie
            # Pour un keystore CUSTOM : demander le mot de passe interactivement
            is_debug_ks = (ks_type != "custom")
            debug_pass  = "android"  # mot de passe standard du debug.keystore Android

            if apksigner_bat and os.path.exists(apksigner_bat):
                bat_lines += [
                    "echo  ── ETAPE 2 : Signature ────────────────────────────────────",
                    "echo.",
                ]
                if is_debug_ks:
                    bat_lines += [
                        f'echo  Keystore DEBUG - mot de passe fixe : android',
                        f'set "KS_PASS=android"',
                    ]
                else:
                    bat_lines += [
                        "echo  Entrez le mot de passe de VOTRE keystore (celui choisi a sa creation) :",
                        f'set /p "KS_PASS=Mot de passe keystore [{ks_alias}] : "',
                    ]

                # Commande apksigner sur UNE SEULE LIGNE — évite les problèmes de ^ avec espaces/parenthèses
                # L'APK aligné est l'entrée ; on supprime l'ancien signé s'il existe
                # Commande apksigner sur UNE SEULE LIGNE
                if is_debug_ks:
                    ks_pass_val = "android"
                else:
                    ks_pass_val = "!KS_PASS!"

                bat_lines += [
                    f'if exist "{signed_apk}" del /q "{signed_apk}"',
                    # IMPORTANT : apksigner.bat est lui-même un script .bat.
                    # L'appeler SANS "call" transfère le contrôle de façon
                    # permanente au sous-script — le script appelant ne
                    # reprend jamais la main après (comportement standard de
                    # cmd.exe). C'est ce qui causait l'arrêt silencieux juste
                    # après les WARNING Java de apksigner, sans jamais
                    # afficher "[OK] Signature reussie" ni passer à l'étape 3.
                    f'call "{apksigner_bat}" sign --ks "{ks_used}" --ks-pass pass:{ks_pass_val} --ks-key-alias {ks_alias} --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true --out "{signed_apk}" "{aligned_apk}"',
                    f'if not exist "{signed_apk}" (',
                    "    echo.",
                    "    echo [ECHEC] apksigner n a pas produit le fichier de sortie.",
                    "    echo         - Verifiez le mot de passe et le keystore",
                    "    goto :end",
                    ")",
                    "echo [OK] Signature reussie",
                ]

                # Etape 3 : vérification
                bat_lines += [
                    "echo.",
                    "echo  ── ETAPE 3 : Verification ──────────────────────────────────",
                    "echo.",
                    f'call "{apksigner_bat}" verify --verbose "{signed_apk}"',
                    "echo.",
                    "echo  ========================================================",
                    f'echo  [OK] APK signe pret :',
                    f'echo      {signed_apk}',
                    "echo  ========================================================",
                ]
            else:
                bat_lines += [
                    "echo  [ERREUR] apksigner introuvable.",
                    "echo  Relance launcher.bat pour installer le SDK Android (zipalign + apksigner).",
                ]

            bat_lines += [
                "",
                ":end",
                "echo.",
                "pause",
                "exit /b 0",
            ]

            bat_content = "\r\n".join(bat_lines) + "\r\n"
            bat_path = WORK_DIR / f"sign_manual_{apk_name}.bat"
            bat_path.write_text(bat_content, encoding="utf-8")

            # Ouvrir le CMD avec le script
            cmd_opened = False
            if sys.platform == "win32":
                try:
                    subprocess.Popen(
                        ["cmd.exe", "/c", "start", "cmd.exe", "/k", str(bat_path)],
                        shell=True,
                        creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0,
                    )
                    cmd_opened = True
                except Exception as e:
                    print(f"[WARN] Impossible d'ouvrir CMD: {e}")

            cmd_info = {
                "apkPath":        str(apk_path),
                "keystoreUsed":   ks_used,
                "keystoreAlias":  ks_alias,
                "zipalign":       zipalign_exe if bt_path else None,
                "apksigner":      apksigner_bat if bt_path else None,
                "hasCustomKs":    ks_custom.exists(),
                "buildTools":     bt,
                "batPath":        str(bat_path),
                "cmdOpened":      cmd_opened,
                "alignedApk":     aligned_apk,
                "signedApk":      signed_apk,
                "commands": {
                    "step0_create_ks": f'keytool -genkey -v -keystore "{ks_used}" -alias {ks_alias} -keyalg RSA -keysize 2048 -validity 10000' if ks_type == "custom" and not ks_custom.exists() else None,
                    "step1_zipalign":  f'"{zipalign_exe}" -f -v 4 "{str(apk_path)}" "{aligned_apk}"' if zipalign_exe else None,
                    "step2_sign":      f'"{apksigner_bat}" sign --ks "{ks_used}" --ks-pass pass:VOTRE_MOT_DE_PASSE --ks-key-alias {ks_alias} --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true --out "{signed_apk}" "{aligned_apk}"' if apksigner_bat else None,
                    "step3_verify":    f'"{apksigner_bat}" verify --verbose "{signed_apk}"' if apksigner_bat else None,
                }
            }
            self._json(cmd_info)
            return

        # ── /bug-log : ajout d'une entrée — soit signalement manuel via le
        # formulaire de l'UI (source="user", défaut), soit détection
        # automatique côté front (ex: outil manquant détecté par checkTools()
        # en JS, qui envoie explicitement source="system").
        if path == "/bug-log":
            payload = self._read_json_body()
            title = (payload.get("title") or "").strip()
            detail = (payload.get("detail") or "").strip()
            severity = payload.get("severity") or "error"
            source = payload.get("source") if payload.get("source") in ("user", "system") else "user"
            if not title:
                self._err("Le titre du bug est requis", 400); return
            entry = buglog_add(severity, title, detail, source=source)
            self._json({"added": True, "entry": entry, "health": buglog_compute_health()})
            return

        # ── /bug-log-resolve : marque une entrée comme résolue (sans la supprimer) ──
        if path == "/bug-log-resolve":
            payload = self._read_json_body()
            entry_id = payload.get("id")
            if not entry_id:
                self._err("id manquant", 400); return
            with _buglog_lock:
                entries = _buglog_read()
                for e in entries:
                    if e.get("id") == entry_id:
                        e["resolved"] = True
                _buglog_write(entries)
            self._json({"resolved": entry_id, "health": buglog_compute_health()})
            return

        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    port = 7842

    # ── Nettoyage du journal au démarrage ────────────────────────────────────
    # Les erreurs/avertissements "system" du run précédent ne sont plus
    # pertinents après un redémarrage (le problème a peut-être été corrigé).
    # On supprime uniquement les entrées système non résolues ; les entrées
    # signalées manuellement par l'utilisateur (source="user") sont conservées.
    try:
        with _buglog_lock:
            existing = _buglog_read()
            kept = [e for e in existing
                    if e.get("source") == "user" or e.get("resolved")]
            _buglog_write(kept)
            cleared = len(existing) - len(kept)
            if cleared:
                print(f"[buglog] {cleared} erreur(s) système effacée(s) au démarrage")
    except Exception as _e:
        print(f"[buglog] nettoyage démarrage ignoré: {_e}")

    # BUG-ENV-03 — écouter sur 0.0.0.0 (toutes interfaces IPv4) plutôt que 127.0.0.1 :
    # sur Windows 11 avec IPv6 activé, "localhost" résout en ::1 (IPv6) mais le serveur
    # n'écoutait que sur 127.0.0.1 (IPv4) → connexion refusée dans le navigateur.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"""
╔══════════════════════════════════════════════════╗
║      APK Factory v3 - Serveur local              ║
╠══════════════════════════════════════════════════╣
║  URL: http://localhost:{port}                      ║
║                                                  ║
║  ✨ Mode SCRATCH : génère APK sans template      ║
║     POST /build-scratch  → APK direct            ║
║     POST /create-scratch-session → Mode Dev      ║
║                                                  ║
║  🔧 Mode Template (décompile existant) :         ║
║     POST /import + POST /recompile               ║
║                                                  ║
║  Arrêter: Ctrl+C                                 ║
╚══════════════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServeur arrêté.")
