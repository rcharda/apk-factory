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

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

# ---------------------------------------------------------------------
# IMPORTANT : setup.js installe les composants téléchargés (Node.js, JDK,
# Android SDK, React Native CLI, Python embarqué, etc.) dans un dossier
# INSCRIPTIBLE côté utilisateur (%APPDATA%\<app>\tools), pas dans
# resources\tools (lecture seule une fois le programme installé sans
# droits admin). server.py doit lire le MÊME dossier, sinon il ne trouve
# jamais rien de ce que setup.js vient d'installer.
#
# main.js transmet APKF_TOOLS_DIR (= setupManager.ROOT) au process Python
# au moment du spawn — voir startPythonServer() dans main.js.
# ---------------------------------------------------------------------
_env_tools_dir = os.environ.get("APKF_TOOLS_DIR")
TOOLS_DIR = Path(_env_tools_dir) if _env_tools_dir else (BASE_DIR / "tools")

# ---------------------------------------------------------------------
# ESPACES DE TRAVAIL PRÊTS PAR TYPE D'APK ("templates/<type>/webroot/")
# ---------------------------------------------------------------------
# Chaque type d'APK (scratch, cordova, flutter, reactnative) a son propre
# dossier-modèle déjà arrangé (index.html + css/style.css + js/app.js +
# img/), stocké UNE FOIS sur disque à côté de server.py. Au lieu de générer
# le contenu du webroot à la volée à chaque création de projet, on COPIE
# ce dossier-modèle tel quel dans le webroot du nouveau projet. L'IA n'a
# alors qu'à écraser/compléter des fichiers qui existent déjà à un chemin
# connu d'avance — jamais de dossier vide, jamais de structure à deviner.
# Ces dossiers ne sont PAS des sessions : ils ne sont jamais modifiés ici,
# uniquement lus/copiés (template en lecture seule, source de vérité).
TEMPLATES_DIR = BASE_DIR / "templates"


def stage_webroot_from_template(apk_type: str, dest_dir: Path, logger=None):
    """
    Copie templates/<apk_type>/webroot/ vers dest_dir (le webroot réel du
    nouveau projet : assets/ en scratch, www/ en cordova, assets/www/ en
    flutter/reactnative...). dest_dir est créé si besoin ; s'il existe déjà
    avec du contenu (ex: régénération), il est d'abord vidé pour repartir
    d'un modèle propre et cohérent.

    Retourne True si la copie a eu lieu, False si le dossier-modèle est
    introuvable (dans ce cas l'appelant doit garder son ancien fallback
    codé en dur, pour ne jamais bloquer une création de projet).
    """
    src = TEMPLATES_DIR / apk_type / "webroot"
    if not src.is_dir():
        if logger:
            logger.log(f"⚠ Dossier-modèle introuvable pour '{apk_type}' ({src}) — "
                        f"fallback sur le squelette généré en mémoire.")
        return False
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(src, dest_dir)
    if logger:
        logger.log(f"✅ Espace de travail '{apk_type}' initialisé depuis templates/{apk_type}/webroot/ "
                    f"→ {dest_dir.name}/ (structure déjà prête, plus qu'à insérer les fichiers)")
    return True


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
HEX_EDIT_MAX_SIZE = 1_500_000  # taille max pour l'édition hexadécimale via navigateur


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

# BUG CORRIGÉ (concurrence) : les routes /build-scratch, /build-native,
# /build-twa, /cordova-generate, /flutter-generate, /rn-generate,
# /build-cordova, /build-flutter, /build-rn faisaient toutes un
# "if op.status == 'building': refuser" DANS LE HANDLER HTTP, mais ne
# posaient le flag "building" qu'à l'intérieur du thread de fond
# (do_build_*/do_generate_*), démarré juste après. Le serveur tourne en
# ThreadingHTTPServer : deux requêtes quasi simultanées sur le même
# endpoint pouvaient donc TOUTES LES DEUX lire un statut "libre" avant
# qu'aucun thread n'ait eu le temps de poser "building" — et démarrer
# chacune un build. Pire, plusieurs do_build_* faisaient en plus
# `logger = OPS[token] = OpLogger()` : le second thread démarré
# réassignait OPS[token] vers un NOUVEL objet, faisant perdre toute trace
# (logs, résultat final) du premier build, qui continuait pourtant de
# tourner en tâche de fond. `try_reserve_op` ferme cette fenêtre : le
# contrôle ET la réservation ("building") se font atomiquement sous
# verrou, dans le thread du handler HTTP, avant même de démarrer le
# thread de build — donc jamais deux builds concurrents sur le même token.
_OPS_START_LOCK = threading.Lock()

def try_reserve_op(token):
    """Retourne le logger réservé (status déjà passé à 'building') si ce
    token était libre, sinon None si un build est déjà en cours pour ce
    token. À utiliser à la place de 'op = get_op(token); if op.status ==
    "building": ...' dans tout handler qui démarre un thread de build."""
    with _OPS_START_LOCK:
        op = get_op(token)
        if op.status == "building":
            return None
        op.lines = []
        op.result_file = None
        op.status = "building"
        return op


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

# BUG CORRIGÉ : le téléchargement de secours du SDK Android (déclenché ici
# quand apksigner est introuvable au moment de signer) utilisait une SEULE
# URL google.com via urllib.request.urlretrieve, SANS timeout et SANS aucun
# miroir de secours — exactement le même problème que celui corrigé côté
# setup.js (Electron). Un seul lien mort ou une coupure réseau plantait tout
# le build/signature sans recours. Cette fonction reprend la même stratégie
# à 3 niveaux que downloadWithFallback() dans setup.js : plusieurs miroirs,
# puis curl, puis PowerShell, avec un message temps réel via logger.log() à
# chaque tentative pour que le client sache ce qui se passe.
def _download_with_fallback(urls, dest_path, logger, timeout=120):
    import urllib.request
    last_err = None

    for i, url in enumerate(urls):
        try:
            logger.log(f"Miroir {i + 1}/{len(urls)} : {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "APKFactoryPro-Server"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            if Path(dest_path).stat().st_size < 1024:
                raise Exception("Fichier téléchargé anormalement petit (lien probablement mort)")
            logger.log(f"OK — {Path(dest_path).stat().st_size / 1024 / 1024:.1f} Mo")
            return url
        except Exception as e:
            logger.log(f"❌ Échec miroir {i + 1}/{len(urls)} : {e}")
            last_err = e
            try: Path(dest_path).unlink(missing_ok=True)
            except Exception: pass

    for i, url in enumerate(urls):
        try:
            logger.log(f"Tentative curl ({i + 1}/{len(urls)})...")
            r = subprocess.run(
                ["curl", "-L", "--fail", "--ssl-no-revoke", "--max-redirs", "10", "-o", str(dest_path), url],
                capture_output=True, timeout=timeout
            )
            if r.returncode == 0 and Path(dest_path).stat().st_size >= 1024:
                logger.log(f"OK via curl — {Path(dest_path).stat().st_size / 1024 / 1024:.1f} Mo")
                return url
            raise Exception("curl a échoué ou fichier trop petit")
        except Exception as e:
            logger.log(f"❌ curl échoué ({i + 1}/{len(urls)}) : {e}")
            last_err = e
            try: Path(dest_path).unlink(missing_ok=True)
            except Exception: pass

    for i, url in enumerate(urls):
        try:
            logger.log(f"Tentative PowerShell ({i + 1}/{len(urls)})...")
            ps_cmd = (
                "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                f"Invoke-WebRequest -Uri '{url}' -OutFile '{dest_path}' -UseBasicParsing -MaximumRedirection 10"
            )
            r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                                capture_output=True, timeout=timeout)
            if r.returncode == 0 and Path(dest_path).stat().st_size >= 1024:
                logger.log(f"OK via PowerShell — {Path(dest_path).stat().st_size / 1024 / 1024:.1f} Mo")
                return url
            raise Exception("PowerShell a échoué ou fichier trop petit")
        except Exception as e:
            logger.log(f"❌ PowerShell échoué ({i + 1}/{len(urls)}) : {e}")
            last_err = e
            try: Path(dest_path).unlink(missing_ok=True)
            except Exception: pass

    raise Exception(f"Tous les téléchargements ont échoué ({len(urls)} miroir(s) × 3 méthodes). Dernière erreur : {last_err}")

def _candidates(name):
    # setup.js installe certains outils "file" (ex: apktool) dans un SOUS-DOSSIER
    # portant le même nom que l'outil : tools/apktool/apktool.jar. On teste donc
    # aussi TOOLS_DIR/name/name.jar (et .bat/.exe) avant les formes à plat, pour
    # matcher exactement la disposition produite par setup.js.
    yield TOOLS_DIR / name / (name + ".jar")
    yield TOOLS_DIR / name / (name + ".bat")
    yield TOOLS_DIR / name / (name + ".exe")
    yield TOOLS_DIR / name
    yield TOOLS_DIR / (name + ".jar")
    yield TOOLS_DIR / (name + ".bat")
    yield TOOLS_DIR / (name + ".exe")
    # BUG-KS-JDK — le JDK téléchargé par setup.js vit dans tools/jdk/bin/
    # (java.exe, keytool.exe, jarsigner.exe...). Cet emplacement n'était
    # JAMAIS testé ici : seuls build-tools/platform-tools (Android SDK)
    # étaient couverts, donc find_tool("keytool") ratait systématiquement
    # le keytool du JDK pourtant installé, et create_keystore_python()
    # retombait à tort sur le fallback pip 'cryptography' (qui échoue sans
    # accès internet pour ce Python embarqué). On cherche donc aussi dans
    # tools/jdk/bin/ et tools/jdk*/bin/ (au cas où le dossier est versionné,
    # ex: jdk-17.0.9).
    jdk_bin = TOOLS_DIR / "jdk" / "bin"
    if jdk_bin.exists():
        for n in (name, name + ".exe"):
            yield jdk_bin / n
    if TOOLS_DIR.exists():
        try:
            for d in TOOLS_DIR.iterdir():
                if d.is_dir() and d.name.lower().startswith("jdk") and d.name != "jdk":
                    for n in (name, name + ".exe"):
                        yield d / "bin" / n
        except Exception:
            pass
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
        # .is_file() (pas .exists()) : un DOSSIER portant le nom de l'outil
        # (ex: tools/apktool/ contenant apktool.jar) ne doit jamais être
        # retourné comme si c'était le binaire lui-même.
        if c.is_file(): return str(c)
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


def make_splash_png(src_bytes, canvas_w, canvas_h, bg_color=(255, 255, 255, 255)):
    """Génère une image plein écran canvas_w×canvas_h avec le logo/icône
    fourni centré et redimensionné pour occuper ~50% de la plus petite
    dimension du canvas, sur un fond uni — convention visuelle standard
    des générateurs de splash screen (flutter_native_splash, cordova-
    splash) : le logo n'est jamais étiré en plein écran. Utilisé pour
    brancher le splash screen par densité sur Cordova/Flutter/React
    Native, qui ne le géraient jusqu'ici pas (seule l'icône l'était)."""
    if not PIL_AVAILABLE or not src_bytes:
        return _make_solid_png(canvas_w, canvas_h, *bg_color[:3], bg_color[3] if len(bg_color) > 3 else 255)
    try:
        logo = Image.open(io.BytesIO(src_bytes)).convert("RGBA")
        target = max(1, int(min(canvas_w, canvas_h) * 0.5))
        logo.thumbnail((target, target), Image.LANCZOS)
        canvas = Image.new("RGBA", (canvas_w, canvas_h), bg_color)
        x = (canvas_w - logo.width) // 2
        y = (canvas_h - logo.height) // 2
        canvas.paste(logo, (x, y), logo)
        buf = io.BytesIO(); canvas.save(buf, format="PNG"); return buf.getvalue()
    except Exception:
        return _make_solid_png(canvas_w, canvas_h, *bg_color[:3], bg_color[3] if len(bg_color) > 3 else 255)


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
                 permissions=None, fullscreen=False, immersive=False, lock_task=False):
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
    fullscreen: si False (défaut) — l'app reste dans les limites normales de
                l'écran (barre de statut / barre de navigation visibles,
                aucun flag plein écran / edge-to-edge). Si True — comportement
                immersif d'origine (masque les barres système).
    immersive: si True — cache la barre de navigation (boutons Retour/Accueil/
               Récents ou bande gestuelle) en mode "immersive sticky". Un swipe
               depuis le bord la fait réapparaître temporairement — ce n'est
               PAS un verrou, juste une dissimulation visuelle. Ne touche pas
               à la barre de statut (indépendant de `fullscreen`).
    lock_task: si True — appelle Activity.startLockTask() au lancement
               ("screen pinning"). Sur un appareil sans droits Device Owner,
               Android affiche une confirmation système la première fois
               ("Épingler cette appli ?"). Une fois épinglée, Accueil et
               Récents sont réellement désactivés jusqu'à ce que l'utilisateur
               fasse le geste de déverrouillage (maintenir Retour + Récents,
               selon la version d'Android).
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

    if fullscreen:
        fullscreen_block = '''
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

    invoke-virtual {p0}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    const/4 v1, 0x0
    invoke-virtual {v0, v1}, Landroid/view/Window;->setDecorFitsSystemWindows(Z)V
    goto :fullscreen_done

    :use_legacy_fullscreen
    invoke-virtual {p0}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    const v1, 0x400
    invoke-virtual {v0, v1}, Landroid/view/Window;->addFlags(I)V

    :fullscreen_done'''
    else:
        # -- Reste dans les limites normales de l'écran --
        # On ne pose aucun flag plein écran / edge-to-edge : le système
        # réserve nativement l'espace de la barre de statut et de la barre
        # de navigation, et le contenu de la WebView ne passe jamais sous
        # ces barres. C'est le comportement par défaut souhaité (cf. case
        # à cocher "Plein écran" dans le builder — décochée par défaut).
        fullscreen_block = ""

    # -- Fragment réutilisable : masque la barre de navigation (Retour/
    # Accueil/Récents) en mode immersive sticky. N'utilise QUE v0/v1 pour
    # rester injectable aussi bien dans onCreate (5 locaux dispo) que dans
    # onWindowFocusChanged (2 locaux dispo) sans conflit avec p0/p1.
    #   - API >= 30 : WindowInsetsController.hide(navigationBars()) +
    #     BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE (0x2) — méthode moderne.
    #   - API < 30  : ancien View.setSystemUiVisibility() avec les flags
    #     LAYOUT_STABLE(0x100) | LAYOUT_HIDE_NAVIGATION(0x200) |
    #     HIDE_NAVIGATION(0x002) | IMMERSIVE_STICKY(0x1000) = 0x1302.
    _immersive_apply_fragment = '''
    sget v0, Landroid/os/Build$VERSION;->SDK_INT:I
    const/16 v1, 0x1e
    if-lt v0, v1, :use_legacy_immersive

    invoke-virtual {p0}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    invoke-virtual {v0}, Landroid/view/Window;->getInsetsController()Landroid/view/WindowInsetsController;
    move-result-object v0
    if-eqz v0, :immersive_done
    invoke-static {}, Landroid/view/WindowInsets$Type;->navigationBars()I
    move-result v1
    invoke-interface {v0, v1}, Landroid/view/WindowInsetsController;->hide(I)V
    const/4 v1, 0x2
    invoke-interface {v0, v1}, Landroid/view/WindowInsetsController;->setSystemBarsBehavior(I)V
    goto :immersive_done

    :use_legacy_immersive
    invoke-virtual {p0}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    invoke-virtual {v0}, Landroid/view/Window;->getDecorView()Landroid/view/View;
    move-result-object v0
    const v1, 0x1302
    invoke-virtual {v0, v1}, Landroid/view/View;->setSystemUiVisibility(I)V

    :immersive_done'''

    if immersive:
        immersive_block = _immersive_apply_fragment
        immersive_refocus_method = f'''
# Ré-applique le mode immersif quand la fenêtre reprend le focus — sans ça,
# ouvrir une autre appli puis revenir fait réapparaître la barre de nav
# de façon définitive sur certains appareils/versions.
.method public onWindowFocusChanged(Z)V
    .registers 4
    .param p1, "hasFocus"
    invoke-super {{p0, p1}}, Landroid/app/Activity;->onWindowFocusChanged(Z)V
    if-eqz p1, :focus_not_gained
{_immersive_apply_fragment}
    :focus_not_gained
    return-void
.end method
'''
    else:
        immersive_block = ""
        immersive_refocus_method = ""

    if lock_task:
        # startLockTask() ("screen pinning") est disponible depuis API 21
        # sans aucun droit spécial — sur un appareil non Device Owner,
        # Android affiche une confirmation système au premier appel. On
        # capture l'exception (rare, ex. politique appareil qui l'interdit)
        # pour ne jamais faire planter l'app au lancement.
        lock_task_block = '''
    :try_start_locktask
    invoke-virtual {p0}, Landroid/app/Activity;->startLockTask()V
    :try_end_locktask
    .catch Ljava/lang/Exception; {:try_start_locktask .. :try_end_locktask} :catch_locktask
    goto :locktask_done
    :catch_locktask
    move-exception v0
    :locktask_done'''
    else:
        lock_task_block = ""

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
{fullscreen_block}
{immersive_block}
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
    invoke-virtual {v1, v2}, Landroid/webkit/WebSettings;->setAllowFileAccessFromFileURLs(Z)V

    # setAllowUniversalAccessFromFileURLs(true)
    invoke-virtual {v1, v2}, Landroid/webkit/WebSettings;->setAllowUniversalAccessFromFileURLs(Z)V
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

    # -- Listener d'insets (edge-to-edge fix, cf. InternalInsetsListener) --
    # Doit être enregistré avant setContentView pour être appelé dès le
    # premier attach de la WebView à la fenêtre (dispatch automatique des
    # WindowInsets par le système à ce moment-là).
    new-instance v1, L{pkg_smali}/InternalInsetsListener;
    invoke-direct {{v1}}, L{pkg_smali}/InternalInsetsListener;-><init>()V
    invoke-virtual {{v0, v1}}, Landroid/view/View;->setOnApplyWindowInsetsListener(Landroid/view/View$OnApplyWindowInsetsListener;)V

    # -- Afficher le WebView --
    invoke-virtual {{p0, v0}}, Landroid/app/Activity;->setContentView(Landroid/view/View;)V
{lock_task_block}
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
{immersive_refocus_method}
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


def _smali_insetslistener(package):
    """
    Sous-classe de View.OnApplyWindowInsetsListener — FIX EDGE-TO-EDGE.
    Depuis Android 15 (targetSdk 35), le système force l'affichage
    edge-to-edge : impossible de désactiver via setDecorFitsSystemWindows.
    Sans cette classe, le contenu de la WebView (header du chat, boutons,
    barre de nav de la page web) passe sous la barre de statut système et
    devient inatteignable au clic. On applique donc un padding-top/bottom
    égal aux insets système (barre de statut + barre de navigation) sur la
    WebView elle-même, à chaque dispatch de WindowInsets.
    On utilise volontairement les méthodes DÉPRÉCIÉES getSystemWindowInset*
    plutôt que WindowInsets.Type (API 30+) : elles restent pleinement
    fonctionnelles sur toutes les API (21 à 35+, calculées automatiquement
    par le système pour la compatibilité) et évitent tout branchement
    conditionnel par version dans ce smali généré.
    """
    pkg_smali = package.replace(".", "/")
    return f'''.class public L{pkg_smali}/InternalInsetsListener;
.super Ljava/lang/Object;
.source "InternalInsetsListener.java"
.implements Landroid/view/View$OnApplyWindowInsetsListener;

.method public constructor <init>()V
    .registers 1
    invoke-direct {{p0}}, Ljava/lang/Object;-><init>()V
    return-void
.end method

# onApplyWindowInsets — applique le padding top/bottom sur la vue (WebView)
# pour que son contenu ne passe plus sous la barre de statut / de nav.
.method public onApplyWindowInsets(Landroid/view/View;Landroid/view/WindowInsets;)Landroid/view/WindowInsets;
    .registers 11
    .param p1, "v"
    .param p2, "insets"

    invoke-virtual {{p2}}, Landroid/view/WindowInsets;->getSystemWindowInsetTop()I
    move-result v0
    invoke-virtual {{p2}}, Landroid/view/WindowInsets;->getSystemWindowInsetBottom()I
    move-result v1
    invoke-virtual {{p2}}, Landroid/view/WindowInsets;->getSystemWindowInsetLeft()I
    move-result v2
    invoke-virtual {{p2}}, Landroid/view/WindowInsets;->getSystemWindowInsetRight()I
    move-result v3

    # -- DEBUG : log les valeurs d'insets calculées (à retirer une fois le
    #    fix confirmé). Filtre logcat : "InsetsListener"
    const-string v4, "InsetsListener"
    new-instance v5, Ljava/lang/StringBuilder;
    invoke-direct {{v5}}, Ljava/lang/StringBuilder;-><init>()V
    const-string v6, "onApplyWindowInsets appele - top="
    invoke-virtual {{v5, v6}}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {{v5, v0}}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;
    const-string v6, " bottom="
    invoke-virtual {{v5, v6}}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {{v5, v1}}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;
    invoke-virtual {{v5}}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;
    move-result-object v7
    invoke-static {{v4, v7}}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I

    invoke-virtual {{p1, v2, v0, v3, v1}}, Landroid/view/View;->setPadding(IIII)V

    return-object p2
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
doNotCompress:
- resources.arsc
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
                "hasDecompiled": any((d / name).exists() for name in PROJECT_ROOT_SUBDIRS),
                "origin": meta.get("origin", "scratch"),  # "scratch" | "decompile" | "cordova" | "flutter" | "reactnative"
            })
    return out

def session_in_active_build(sid):
    """True si un OPS[token] est actuellement 'building' sur cette session
    précise. Utilisé pour empêcher toute suppression du dossier d'une
    session en plein build."""
    for op in OPS.values():
        if op.status == "building" and op.session == sid:
            return True
    return False

def delete_session(sid, force=False):
    """
    BUG CORRIGÉ : cette fonction supprimait le dossier d'une session sans
    jamais vérifier si un build était en cours dessus (apktool/gradle/
    flutter/cordova en train d'écrire ou de lire dans ce même dossier) —
    un DELETE /session déclenché pendant une compilation pouvait faire
    disparaître les fichiers sous les pieds du subprocess en cours
    (échec cryptique, voire pire : sortie partielle silencieuse). On
    refuse maintenant la suppression tant qu'un OPS[token] actif pointe
    sur cette session, sauf si force=True est explicitement demandé par
    l'appelant (ex: nettoyage interne après un build terminé/en erreur).
    """
    if not force and session_in_active_build(sid):
        raise RuntimeError(
            f"Session {sid} en cours de compilation — suppression refusée. "
            f"Attends la fin du build (ou son échec) avant de la supprimer."
        )
    sd = session_dir(sid)
    if sd.exists(): shutil.rmtree(sd)

def build_project_overview(sid):
    """Vue d'ensemble condensée d'une session, pensée pour l'agent IA.

    Objectif : en UN SEUL appel HTTP, l'agent obtient tout le contexte dont
    il a besoin pour raisonner sur le projet (type de pipeline, arborescence,
    fichiers de config déjà lisibles, état de santé) — au lieu d'enchaîner
    une dizaine d'appels /tree puis /file en devinant les chemins. Sert de
    point d'entrée systématique avant toute modification autonome.
    """
    sd = session_dir(sid)
    meta = {}
    meta_f = sd / "session.json"
    if meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    root = resolve_session_root(sid)
    origin = meta.get("origin", "scratch")

    try:
        tree = list_tree_all(sid)
    except Exception:
        tree = []
    tree_files = [t for t in tree if t.get("type") == "file"]

    # Fichiers de config qu'on tente de lire en clair pour donner du contexte
    # immédiat, sans que l'agent ait à deviner leur chemin exact selon le
    # pipeline (scratch/décompilé/cordova/flutter/react native).
    CANDIDATE_CONFIG_FILES = [
        "AndroidManifest.xml",
        "app/src/main/AndroidManifest.xml",
        "android/app/src/main/AndroidManifest.xml",
        "platforms/android/app/src/main/AndroidManifest.xml",
        "build.gradle", "app/build.gradle", "android/app/build.gradle",
        "config.xml",                     # Cordova
        "pubspec.yaml",                   # Flutter
        "package.json",                   # React Native
        "apktool.yml",                    # décompilé (apktool)
    ]
    configs = {}
    for rel in CANDIDATE_CONFIG_FILES:
        try:
            p = (root / rel)
            if p.is_file() and p.stat().st_size < 40_000:
                configs[rel] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    smali_count = 0
    try:
        smali_count = len(scan_smali_facts(sid, max_facts=2000))
    except Exception:
        pass

    health = {}
    try:
        health = buglog_compute_health()
    except Exception:
        pass

    # CORRECTIF PERFORMANCE IA : jusqu'ici l'agent ne découvrait un
    # assets/index.html vide/manquant qu'APRÈS un build_project complet
    # (plusieurs minutes de compilation Gradle/apktool), via l'erreur
    # "Compilation annulée : ... est vide" — un aller-retour entièrement
    # gaspillé alors que get_project_overview est explicitement l'outil que
    # l'agent appelle EN PREMIER sur toute session. On réutilise donc ici la
    # même fonction faisant autorité (enforce_project_entrypoint, déjà
    # utilisée avant chaque build et par GET /entrypoint) pour exposer
    # directement l'état du point d'entrée dans l'overview : l'agent voit
    # ainsi "entrypoint.contentMissing" dès son tout premier appel, avant
    # même d'envisager build_project, et peut écrire le vrai contenu tout
    # de suite au lieu de le découvrir après un build raté.
    entrypoint = {"kind": "unknown", "activeIndexPath": None, "removed": [], "contentMissing": False}
    try:
        entrypoint = enforce_project_entrypoint(sid, kind_hint=(origin or None))
    except Exception:
        pass

    MAX_TREE_ITEMS = 800  # on tronque pour ne pas noyer le modèle de contexte
    return {
        "session": sid,
        "origin": origin,
        "appName": meta.get("appName"),
        "package": meta.get("package") or meta.get("packageOld"),
        "created": meta.get("created"),
        "rootExists": root.exists(),
        "fileCount": len(tree_files),
        "dirCount": max(0, len(tree) - len(tree_files)),
        "tree": tree[:MAX_TREE_ITEMS],
        "treeTruncated": len(tree) > MAX_TREE_ITEMS,
        "configFiles": configs,
        "smaliFactsCount": smali_count,
        "health": health,
        "entrypoint": entrypoint,
    }

def write_hybrid_session_meta(sid, origin, config):
    """Écrit session.json pour une session Cordova/Flutter/React Native/Native,
    afin qu'elle apparaisse dans /sessions (panneau Sessions) et soit
    reconnue par l'explorer/config-panel comme une session normale,
    au même titre que scratch/template/décompilé."""
    meta = {
        "created": time.time(),
        "package": (config.get("packageName") or "").strip(),
        "packageOld": (config.get("packageName") or "").strip(),
        "appName": (config.get("appName") or "MonApp").strip(),
        "origin": origin,  # "cordova" | "flutter" | "reactnative" | "native"
    }
    (session_dir(sid) / "session.json").write_text(json.dumps(meta), encoding="utf-8")

PROJECT_ROOT_SUBDIRS = [
    "source",            # scratch / template (v3)
    "decompiled",        # legacy (v2)
    "cordova_project",
    "flutter_project",
    "rn_project",
    # CORRECTIF : "native_project" (voir native_project_dir) manquait ici.
    # Conséquence concrète : resolve_session_root() ne trouvait AUCUN de ces
    # noms pour une session native, retombait sur source_dir(sid) qui
    # n'existe pas pour ce pipeline, et /tree|/file renvoyaient donc un
    # projet vide/introuvable pour tout APK natif généré par do_build_native
    # — impossible à parcourir/éditer après coup dans l'explorateur.
    "native_project",
]

def resolve_session_root(sid):
    """Retourne le dossier racine du projet éditable pour une session,
    quelle que soit son origine (scratch, template, décompilé, cordova,
    flutter, react native). Retombe sur source_dir(sid) si rien n'existe
    encore (comportement historique)."""
    sd = session_dir(sid)
    for name in PROJECT_ROOT_SUBDIRS:
        p = sd / name
        if p.exists():
            return p
    return source_dir(sid)

def _safe_target(sid, rel_path):
    sdir = resolve_session_root(sid).resolve()
    rel_path = (rel_path or "").lstrip("/\\")
    target = (sdir / rel_path).resolve()
    if not str(target).startswith(str(sdir)):
        raise PermissionError("Chemin invalide")
    return sdir, target

def list_tree_all(sid):
    sdir = resolve_session_root(sid)
    if not sdir.exists(): return []
    items = []
    for p in sorted(sdir.rglob('*'), key=lambda x: str(x).lower()):
        rel = str(p.relative_to(sdir)).replace('\\', '/')
        items.append({'path': rel, 'type': 'dir' if p.is_dir() else 'file',
                      'size': p.stat().st_size if p.is_file() else 0})
    return items

# ---------------------------------------------------------------------
# /template-tree : liste le VRAI contenu, tel qu'il existe RÉELLEMENT sur
# le disque dans templates/<type>/(webroot|...), SANS avoir besoin d'une
# session ni d'un projet créé. Sert à afficher dans l'explorateur de gauche
# les vrais dossiers/sous-dossiers et les vrais chemins que l'IA utilisera
# pour injecter ses fichiers — dès le chargement de la page, avant même la
# première création de projet — plutôt qu'une liste devinée côté client.
# ---------------------------------------------------------------------
TEMPLATE_TREE_ROOTS = {
    'scratch':     TEMPLATES_DIR / 'scratch' / 'webroot',
    'cordova':     TEMPLATES_DIR / 'cordova' / 'webroot',
    'flutter':     TEMPLATES_DIR / 'flutter' / 'webroot',
    'reactnative': TEMPLATES_DIR / 'reactnative' / 'webroot',
    'native':      TEMPLATES_DIR / 'native',
}

# Où ce contenu-modèle atterrit RÉELLEMENT une fois un projet généré (voir
# stage_webroot_from_template / stage_native_skeleton) — c'est le vrai
# préfixe de chemin que l'IA doit utiliser dans write_file pour ce type.
TEMPLATE_MOUNT_PREFIX = {
    'scratch':     'assets/',
    'cordova':     'www/',
    'flutter':     'assets/www/',
    'reactnative': 'android/app/src/main/assets/www/',
    'native':      'app/src/main/ (kotlin/<package>/MainActivity.kt + res/)',
    'twa':         None,  # pas de dossier de fichiers pour TWA
}

def list_template_tree(apk_type):
    root = TEMPLATE_TREE_ROOTS.get(apk_type)
    if not root or not root.is_dir():
        return {'exists': False, 'items': []}
    items = []
    for p in sorted(root.rglob('*'), key=lambda x: str(x).lower()):
        rel = str(p.relative_to(root)).replace('\\', '/')
        items.append({'path': rel, 'type': 'dir' if p.is_dir() else 'file',
                      'size': p.stat().st_size if p.is_file() else 0})
    return {'exists': True, 'items': items}

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

def _snapshot_history(sid, rel_path, data_bytes):
    """Conserve une copie horodatée de chaque écriture de fichier dans
    session_dir(sid)/.history/, pour permettre à /export-zip de reconstituer
    TOUT l'historique de fichiers générés pendant la session (pas juste
    l'état final). Best-effort : une erreur ici ne doit jamais casser
    l'écriture réelle du fichier.
    """
    try:
        hist_dir = session_dir(sid) / ".history"
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_rel = rel_path.replace("\\", "/").strip("/")
        dest = hist_dir / f"{ts}__{new_session_id()[:8]}" / safe_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data_bytes)
    except Exception:
        pass

_GRADLE_JAVA_ORIGINS = ("cordova", "flutter", "reactnative")

def _session_origin(sid):
    """Lit l'origine d'une session depuis session.json ('cordova', 'flutter',
    'reactnative', ou '' pour un projet scratch/template/natif décompilé qui
    passe par apktool sur un arbre smali)."""
    try:
        meta_path = session_dir(sid) / "session.json"
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8")).get("origin", "")
    except Exception:
        pass
    return ""

# =============================================================
# ANTI PAGE-BLANCHE — un SEUL dossier "racine web" actif par projet
# =============================================================
# Chaque type d'APK charge son HTML/CSS/JS depuis UN SEUL emplacement bien
# précis (assets/ pour scratch, www/ pour Cordova/React Native,
# assets/www/ pour un Flutter-WebView, AUCUN pour un Flutter natif). Le bug
# de page blanche vient systématiquement du même schéma : un fichier
# index.html/style.css/app.js écrit (par l'IA ou à la main) dans le MAUVAIS
# dossier reste un orphelin silencieux — la WebView ne le charge jamais,
# mais rien ne prévient personne. `enforce_project_entrypoint` corrige ça de
# façon systématique : dès qu'on connaît le type réel du projet, elle
# SUPPRIME activement ces 4 noms de fichiers dans tous les AUTRES dossiers
# racines possibles, et signale (sans jamais écrire de fichier à la place)
# si le dossier actif n'a pas de contenu réel — c'est _assert_entrypoint_ready
# qui bloque ensuite la compilation dans ce cas, au lieu de laisser passer un
# APK dont l'unique "contenu" serait une page de secours générée.
_WEBROOT_ENTRY_NAMES = ("index.html", "style.css", "app.js", "script.js")
_WEBROOT_CANDIDATE_PREFIXES = ["", "www/", "assets/", "assets/www/", "android/app/src/main/assets/www/"]

def _flutter_uses_webview(root):
    """Lit pubspec.yaml + lib/main.dart pour savoir si CE projet Flutter
    utilise réellement une WebView, plutôt que de le supposer dès que
    pubspec.yaml existe (cause du bug : un Flutter NATIF avec de vrais
    écrans Dart n'a aucune WebView, donc aucun dossier web actif)."""
    try:
        pubspec = (root / "pubspec.yaml")
        main_dart = (root / "lib" / "main.dart")
        pubspec_text = pubspec.read_text(encoding="utf-8", errors="ignore") if pubspec.exists() else ""
        main_dart_text = main_dart.read_text(encoding="utf-8", errors="ignore") if main_dart.exists() else ""
        if "webview_flutter" in pubspec_text:
            return True
        if re.search(r'WebView(Widget|Controller)?\s*\(', main_dart_text):
            return True
    except Exception:
        pass
    return False

def enforce_project_entrypoint(sid, kind_hint=None, logger=None):
    """Détermine le SEUL dossier racine web actif pour cette session selon
    son type réel, supprime les fichiers index.html/style.css/app.js/
    script.js orphelins dans tous les AUTRES dossiers candidats, et détecte
    si le dossier actif contient un contenu réel (sans jamais écrire de
    fichier de secours à la place — voir contentMissing dans le rapport
    retourné). Appelée après chaque génération de projet (create_scratch_session,
    generate_cordova_project, generate_flutter_project,
    generate_react_native_project...) et avant chaque recompilation, pour
    rattraper aussi les modifications faites après coup par l'IA/le client.
    Ne touche JAMAIS à un fichier autre que ces 4 noms précis : aucun risque
    pour le code natif (lib/, android/, smali/, src/...).

    Retourne un dict décrivant le résultat — c'est LA réponse faisant
    autorité sur "quel est le bon chemin", exposée aussi via GET /entrypoint
    pour que l'agent IA la lise au lieu de la redéviner de son côté :
      {
        "kind": "scratch" | "cordova" | "reactnative" | "flutter-webview" | "flutter-native" | "unknown",
        "activeIndexPath": "assets/index.html" | "www/index.html" | "assets/www/index.html" | None,
        "removed": [...chemins supprimés...],
        "contentMissing": bool,
      }
    """
    def _log(msg):
        if logger is not None:
            try: logger.log(msg)
            except Exception: pass

    report = {"kind": "unknown", "activeIndexPath": None, "removed": [], "contentMissing": False}
    try:
        root = resolve_session_root(sid)
        if not root.exists():
            return report
        origin = kind_hint if kind_hint is not None else _session_origin(sid)

        if origin == "cordova":
            active_prefix = "www/"; report["kind"] = "cordova"
        elif origin == "reactnative":
            # BUG CORRIGÉ : generate_react_native_project() et
            # _apply_config_reactnative() écrivent TOUS LES DEUX dans
            # android/app/src/main/assets/www/ (seul dossier réellement
            # empaqueté par Gradle et chargé par la WebView RN via
            # file:///android_asset/www/index.html) — jamais dans un
            # "www/" à la racine du projet. Cette fonction surveillait
            # jusqu'ici le mauvais dossier (racine), donc le vrai fichier
            # chargé par l'app n'était ni vérifié ni réparé en cas de
            # contenu vide/cassé : page blanche silencieuse.
            active_prefix = "android/app/src/main/assets/www/"
            report["kind"] = "reactnative"
        elif origin == "flutter":
            uses_wv = _flutter_uses_webview(root)
            active_prefix = "assets/www/" if uses_wv else None
            report["kind"] = "flutter-webview" if uses_wv else "flutter-native"
        elif origin == "":
            # BUG CORRIGÉ : le mode scratch recouvre en réalité 3
            # sous-types différents (HTML collé → assets/index.html ;
            # site zippé → assets/www/index.html ; URL distante → aucun
            # fichier local), mais cette fonction supposait TOUJOURS
            # "assets/" comme dossier actif. Pour un projet en mode
            # "site zip", ça faisait traiter assets/www/index.html — le
            # fichier réellement chargé par la WebView compilée — comme
            # un orphelin dans le mauvais dossier, et le SUPPRIMER : page
            # blanche garantie. On lit maintenant le vrai chemin loadUrl()
            # déjà compilé dans MainActivity.smali (seule source fiable de
            # ce que l'app va réellement charger) au lieu de le deviner.
            report["kind"] = "scratch"
            active_prefix = "assets/"
            try:
                smali_matches = list(root.glob("smali/**/MainActivity.smali"))
                if smali_matches:
                    smali_txt = smali_matches[0].read_text(encoding="utf-8", errors="ignore")
                    if "android_asset/www/index.html" in smali_txt:
                        active_prefix = "assets/www/"
                    elif "android_asset/index.html" in smali_txt:
                        active_prefix = "assets/"
                    else:
                        # Mode URL distante : aucun webroot local à charger,
                        # rien à supprimer ni de fallback à écrire.
                        active_prefix = None
            except Exception:
                pass
        else:
            # native / twa / origine inconnue : pipeline dédié, pas de
            # dossier "racine web" géré par cette fonction.
            return report

        removed = []
        for prefix in _WEBROOT_CANDIDATE_PREFIXES:
            if prefix == active_prefix:
                continue
            for name in _WEBROOT_ENTRY_NAMES:
                p = (root / prefix / name) if prefix else (root / name)
                try:
                    if p.is_file():
                        p.unlink()
                        removed.append(str(p.relative_to(root)).replace("\\", "/"))
                except Exception:
                    pass
        report["removed"] = removed
        if removed:
            _log(f"🧹 Fichiers webview orphelins supprimés (mauvais dossier, cause habituelle de page blanche) : {', '.join(removed)}")

        if active_prefix is not None:
            index_rel = f"{active_prefix}index.html" if active_prefix else "index.html"
            report["activeIndexPath"] = index_rel
            index_path = root / index_rel
            # BUG CORRIGÉ : cette fonction écrivait auparavant une page de
            # secours "⚠ Contenu manquant" directement dans index.html dès
            # que le contenu était vide/absent. Ce fichier devenait alors un
            # contenu de projet réel et pouvait être empaqueté tel quel dans
            # l'APK si le client compilait sans s'en rendre compte — donc un
            # vrai bug (APK "réussi" qui n'affiche que l'avertissement), pas
            # un garde-fou. On se contente maintenant de CONSTATER l'absence
            # de contenu réel (report["contentMissing"]) sans jamais écrire
            # de fichier ; c'est _assert_entrypoint_ready qui bloque ensuite
            # la compilation en levant une erreur explicite.
            content_missing = True
            if index_path.is_file():
                try:
                    txt = index_path.read_text(encoding="utf-8", errors="ignore")
                    m = re.search(r'<body[^>]*>([\s\S]*?)</body>', txt, re.I)
                    inner = re.sub(r'<!--[\s\S]*?-->', '', m.group(1) if m else '').strip()
                    content_missing = not inner
                except Exception:
                    content_missing = False  # fichier illisible : on n'y touche pas
            report["contentMissing"] = content_missing
            if content_missing:
                _log(f"⚠ {index_rel} manquant/vide — aucun contenu réel détecté. Chemin correct à éditer : {index_rel}")
            else:
                _log(f"✅ Dossier racine web confirmé : {index_rel}")
        else:
            # Flutter natif : aucune WebView attendue — juste un contrôle
            # de bon sens sur le vrai point d'entrée Dart, à titre informatif.
            main_dart = root / "lib" / "main.dart"
            report["activeIndexPath"] = None
            if main_dart.is_file():
                try:
                    src = main_dart.read_text(encoding="utf-8", errors="ignore")
                    if "runApp(" not in src:
                        _log("⚠ lib/main.dart ne contient pas d'appel runApp(...) — l'app affichera probablement un écran blanc. Chemin correct à éditer : lib/main.dart (et lib/screens/*.dart).")
                    else:
                        _log("✅ Projet Flutter natif confirmé — chemin correct à éditer : lib/main.dart (et lib/screens/*.dart), PAS de fichier HTML.")
                except Exception:
                    pass
    except Exception as e:
        _log(f"⚠ Vérification de la structure du projet ignorée (non bloquant) : {e}")
    return report

def _assert_entrypoint_ready(sid, kind_hint, logger):
    """
    Appelle enforce_project_entrypoint puis lève une erreur claire si le
    contenu réel est manquant (contentMissing=True) — À N'UTILISER QU'AU
    MOMENT DE COMPILER, jamais juste après une génération "sans build" où
    le client/l'IA doit encore avoir la main pour écrire le vrai contenu.
    BUG DE FOND corrigé ici : sans ce garde-fou, un build pouvait se
    terminer avec status="done" et produire un APK installable sans aucun
    contenu réel — un échec silencieux déguisé en succès, pour TOUS les
    types de projet (scratch, cordova, flutter-webview, react native).
    enforce_project_entrypoint ne génère plus de page de secours à écrire
    dans le projet (cela créait elle-même un bug : un APK "réussi" dont
    l'unique contenu aurait été cette page générée) ; elle se contente de
    constater l'absence de contenu, et c'est cette fonction qui bloque la
    compilation en conséquence. Retourne le report si tout va bien (pour
    usage éventuel par l'appelant).
    """
    report = enforce_project_entrypoint(sid, kind_hint=kind_hint, logger=logger)
    if report.get("contentMissing"):
        path_hint = report.get("activeIndexPath") or "le fichier d'entrée du projet"
        raise RuntimeError(
            f"Compilation annulée : {path_hint} est vide ou n'a jamais été rempli avec le vrai "
            f"contenu de l'app. Écris le vrai contenu dans {path_hint} (via write_file) avant "
            f"de relancer la compilation."
        )
    return report

# BUG DE FOND corrigé ici (voir aussi le garde-fou jumeau côté client dans
# agent-engine.js/enforceApkPathMatch) : rien côté SERVEUR n'empêchait
# d'écrire un fichier propre à un framework (pubspec.yaml, *.dart,
# config.xml, package.json+App.js) dans une session dont l'origin réel
# (session.json) est différent. Le garde-fou client peut être contourné
# (appel direct à l'API, extension désactivée, désync du cache
# localStorage) : on rejoue donc la même vérification ici, qui FAIT
# AUTORITÉ, indépendamment de ce que croit le client.
_FRAMEWORK_MARKER_CHECKS = {
    "flutter": lambda p: p == "pubspec.yaml" or p.endswith("/pubspec.yaml") or p.endswith(".dart"),
    "cordova": lambda p: p == "config.xml" or p.endswith("/config.xml"),
    "reactnative": lambda p: (
        p == "package.json" or p.endswith("/package.json")
        or re.search(r'(^|/)App\.(js|tsx|jsx)$', p) is not None
    ),
}

def _enforce_framework_marker(sid, rel_path):
    clean = str(rel_path).replace("\\", "/").lstrip("/")
    origin = _session_origin(sid)
    for fw_mode, is_marker in _FRAMEWORK_MARKER_CHECKS.items():
        if fw_mode == origin:
            continue
        if is_marker(clean):
            raise ValueError(
                f"Fichier NON écrit : '{rel_path}' — ce fichier est propre au framework '{fw_mode}', "
                f"mais cette session est de type '{origin or 'inconnu'}'. L'écrire ici produirait un projet "
                f"hybride incohérent qui ne compilera jamais correctement. Crée d'abord une VRAIE session "
                f"'{fw_mode}' (create_project côté agent, ou le bouton dédié côté interface), puis réécris ce "
                f"fichier — et tous les autres fichiers {fw_mode} déjà prévus — dans cette nouvelle session."
            )

def cleanup_mismatched_framework_files(sid, logger=None):
    """Répare une session déjà polluée par CE bug précis (fichiers d'un
    framework écrits avant que le garde-fou n'existe, ou avant qu'il ne
    couvre ce cas). Parcourt l'arbre de la session et SUPPRIME tout fichier
    qui matche un marqueur de framework différent de l'origin réel de la
    session (voir _FRAMEWORK_MARKER_CHECKS) — jamais l'inverse (on ne
    touche jamais aux fichiers du BON framework). Retourne la liste des
    chemins supprimés, pour affichage/logs. Sans risque pour le code
    natif/smali : seuls .dart, pubspec.yaml, config.xml, package.json et
    App.js/tsx/jsx sont concernés, jamais autre chose.
    """
    def _log(msg):
        if logger is not None:
            try: logger.log(msg)
            except Exception: pass

    origin = _session_origin(sid)
    root = resolve_session_root(sid)
    removed = []
    if not root.exists():
        return removed
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except Exception:
            continue
        for fw_mode, is_marker in _FRAMEWORK_MARKER_CHECKS.items():
            if fw_mode == origin:
                continue
            if is_marker(rel):
                try:
                    p.unlink()
                    removed.append(rel)
                except Exception:
                    pass
                break
    if removed:
        _log(f"🧹 Nettoyage session incohérente : {len(removed)} fichier(s) d'un autre framework supprimé(s) : {', '.join(removed[:20])}" + (" ..." if len(removed) > 20 else ""))
    return removed

def write_file_safe(sid, rel_path, content):
    _enforce_framework_marker(sid, rel_path)
    if str(rel_path).endswith('.smali'):
        smali_errors = _smali_quick_check(rel_path, content)
        if smali_errors:
            raise ValueError(
                "Syntaxe smali invalide dans '" + str(rel_path) + "' — fichier NON écrit :\n- "
                + "\n- ".join(smali_errors)
            )
    # BUG constaté : l'IA écrivait parfois un fichier .java (mise en page
    # Gradle type app/src/main/java/...) dans une session scratch/template/
    # native-décompilée, dont le pipeline de compilation est EXCLUSIVEMENT
    # apktool sur un arbre smali (recompile_session) — il n'existe aucun
    # compilateur Java/Kotlin pour ce type de session. Le fichier .java est
    # alors ignoré par apktool et la compilation échoue avec un message
    # générique, sans que l'IA comprenne pourquoi. Seules les sessions
    # cordova/flutter/reactnative utilisent un vrai pipeline Gradle
    # (_recompile_hybrid_session) où .java/.kt sont légitimes.
    if str(rel_path).endswith(('.java', '.kt')) and _session_origin(sid) not in _GRADLE_JAVA_ORIGINS:
        raise ValueError(
            "Fichier NON écrit : '" + str(rel_path) + "' — ce type de projet (scratch/template/natif "
            "décompilé) compile UNIQUEMENT via apktool sur du smali, il n'existe aucun compilateur "
            "Java/Kotlin ici. N'écris jamais de .java/.kt dans ce projet : édite le .smali existant "
            "(cherche-le avec search_project ou get_smali_facts) au lieu de créer un fichier Java. "
            "Le Java/Kotlin n'est utilisable que dans un projet créé en mode cordova/flutter/reactnative."
        )
    # BUG constaté (suite du précédent) : après un refus de .java/.kt, l'IA
    # continuait parfois dans la même impasse en écrivant des layouts natifs
    # (res/layout/*.xml, res/menu/*.xml...) — tout aussi inertes sans
    # Activity Kotlin/Java pour les inflater, donc du temps perdu sur une
    # architecture impossible ici. Pour ce type de session, TOUTE
    # fonctionnalité (y compris un jeu) doit être codée en HTML5/CSS/JS
    # (Canvas pour la partie graphique) dans le webroot actif — on bloque
    # ces fichiers aussi et on redirige explicitement vers la bonne approche
    # au lieu de laisser l'IA s'entêter sur du natif inutilisable.
    if re.match(r'^res/(layout|menu|navigation)/', str(rel_path).replace('\\', '/')) and _session_origin(sid) not in _GRADLE_JAVA_ORIGINS:
        raise ValueError(
            "Fichier NON écrit : '" + str(rel_path) + "' — ce type de projet n'a aucune Activity "
            "Kotlin/Java pour inflater un layout natif, ce fichier serait donc totalement inerte. "
            "Pour CE type de projet, toute fonctionnalité (y compris un jeu) doit être codée en "
            "HTML5/CSS/JS dans le webroot actif de la WebView (assets/index.html ou assets/www/index.html "
            "selon le projet — utilise get_project_overview pour connaître le bon chemin) ; utilise "
            "l'élément <canvas> + JavaScript pour la partie graphique/jeu plutôt qu'un layout Android natif."
        )
    sdir, target = _safe_target(sid, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _snapshot_history(sid, rel_path, content.encode("utf-8"))

# -------------------------------------------------------------------------
# Validation rapide de syntaxe smali AVANT écriture disque.
# Objectif : attraper l'erreur la plus fréquente commise par l'IA quand elle
# écrit du smali "à la main" — utiliser une syntaxe de tableau façon Java
# (ex: `new-array v0, {"a", "b"}` ou `sput-object {"x","y"}, ...`), qui n'existe
# PAS en smali. En smali, les accolades { } ne sont légales que dans deux cas :
#   1) juste après un opcode invoke-*  → une liste de registres, ex: {p0, v0}
#   2) à l'intérieur d'un bloc .array-data / .end array-data
# Toute autre paire d'accolades (notamment contenant des guillemets, des
# nombres flottants ou des virgules hors invoke-*) est presque toujours une
# hallucination de syntaxe Java par l'IA, qui casse la compilation apktool
# beaucoup plus tard avec un message cryptique ("no viable alternative at
# input '{'"). On le détecte ici, tout de suite, pour renvoyer un message
# clair à l'IA qui pourra se corriger elle-même au lieu de relancer un build
# complet pour rien.
_SMALI_INVOKE_RE = re.compile(r'\b(invoke-[a-z/-]+(?:/range)?)\s*\{')
_SMALI_BRACE_RE  = re.compile(r'\{[^{}]*\}')

def _smali_quick_check(rel_path, content):
    if not rel_path.endswith('.smali'):
        return None
    errors = []

    # 1) Équilibre global des accolades / parenthèses.
    if content.count('{') != content.count('}'):
        errors.append("Accolades déséquilibrées ({ et } ne sont pas en nombre égal).")

    # 2) Chaque paire d'accolades doit soit suivre un opcode invoke-*, soit
    #    être une liste de registres/pseudo-registres (v0, p1, ...), soit
    #    apparaître dans un bloc .array-data.
    in_array_data = False
    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = raw.split('#', 1)[0]  # ignore les commentaires
        stripped = line.strip()
        if stripped.startswith('.array-data'):
            in_array_data = True
            continue
        if stripped.startswith('.end array-data'):
            in_array_data = False
            continue
        if in_array_data or '{' not in line:
            continue
        # Détecte les accolades doublées/imbriquées (ex: `{{p0, v0}}`) —
        # séquelle fréquente de l'IA qui recopie par erreur l'échappement
        # `{{ }}` des f-strings Python vues dans server.py. La regex
        # ci-dessous ne matche que la paire la PLUS INTERNE ; des accolades
        # collées les unes aux autres passent donc inaperçues si on ne les
        # vérifie pas explicitement ici.
        if '{{' in line or '}}' in line:
            errors.append(
                f"Ligne {lineno}: accolades doublées détectées (`{{{{` ou `}}}}`) : "
                f"`{stripped}`. En smali réel il ne faut JAMAIS doubler les accolades "
                "(pas d'échappement façon f-string Python) — une seule paire, ex: "
                "invoke-virtual {p0, v0}, ..."
            )
            continue
        for m in _SMALI_BRACE_RE.finditer(line):
            brace_content = m.group(0)
            before = line[:m.start()].rstrip()
            is_after_invoke = bool(_SMALI_INVOKE_RE.search(before + '{'))
            looks_like_reg_list = re.fullmatch(
                r'\{\s*(?:[vp]\d+\s*(?:,\s*[vp]\d+\s*)*)?\}', brace_content
            ) is not None
            if not is_after_invoke and not looks_like_reg_list:
                errors.append(
                    f"Ligne {lineno}: syntaxe d'accolades invalide en smali : `{brace_content.strip()}`. "
                    "Les accolades ne sont autorisées qu'immédiatement après un opcode invoke-* "
                    "(liste de registres, ex: invoke-static {v0, v1}, ...) ou dans un bloc "
                    ".array-data / .end array-data. Un littéral de tableau façon Java "
                    "(ex: {\"a\", \"b\"}) n'existe PAS en smali — utilise filled-new-array + "
                    "move-result-object pour un petit tableau fixe, ou un bloc "
                    ".array-data pour des données constantes."
                )
    return errors or None

def write_binary_safe(sid, rel_path, data_bytes):
    sdir, target = _safe_target(sid, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data_bytes)
    _snapshot_history(sid, rel_path, data_bytes)

def read_binary_raw(sid, rel_path):
    sdir, target = _safe_target(sid, rel_path)
    if not target.is_file(): raise FileNotFoundError("Fichier introuvable")
    return target.read_bytes()


# =============================================================
# RECHERCHE TEXTE GLOBALE — "control total" sur tout le projet
# =============================================================
# Parcourt tous les fichiers texte de la session (smali, xml, json, etc.)
# et retourne chaque ligne correspondante avec son numéro, pour permettre
# à l'utilisateur de naviguer/éditer directement comme dans un IDE.
SEARCH_MAX_RESULTS = 500
SEARCH_MAX_FILE_SIZE = 4_000_000

def search_content(sid, query, use_regex=False, case_sensitive=False, max_results=SEARCH_MAX_RESULTS):
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    if not sdir.exists() or not query:
        return {"results": [], "truncated": False, "filesScanned": 0}

    if use_regex:
        try:
            pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Regex invalide: {e}")
    else:
        needle = query if case_sensitive else query.lower()
        pattern = None

    results = []
    files_scanned = 0
    truncated = False

    for f in sorted(sdir.rglob("*")):
        if len(results) >= max_results:
            truncated = True
            break
        if not f.is_file(): continue
        if f.suffix.lower() not in TEXT_EXTENSIONS and f.suffix != "": continue
        try:
            if f.stat().st_size > SEARCH_MAX_FILE_SIZE: continue
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files_scanned += 1
        rel = str(f.relative_to(sdir)).replace("\\", "/")
        for lineno, line in enumerate(text.split("\n"), start=1):
            hay = line if case_sensitive else line.lower()
            if use_regex:
                m = pattern.search(line)
                if not m: continue
                col = m.start()
            else:
                col = hay.find(needle)
                if col < 0: continue
            snippet = line.strip()
            if len(snippet) > 240: snippet = snippet[:240] + "…"
            results.append({"path": rel, "line": lineno, "col": col, "text": snippet})
            if len(results) >= max_results:
                truncated = True
                break

    return {"results": results, "truncated": truncated, "filesScanned": files_scanned}

def rename_with_refs(sid, rel, new_rel):
    """Renomme un fichier/dossier puis remplace toute occurrence de l'ancien
    nom (et de l'ancien chemin relatif) dans tous les fichiers texte du projet
    (smali, xml, json, html...) par le nouveau nom — pour ne rien casser
    (AndroidManifest, code smali qui référence assets/xxx.png, etc.).

    Sécurités contre les noms courts/génériques (ex: "1.png") :
    - Le nom seul n'est remplacé qu'en frontière de mot (regex \\b), donc
      "21.png" ou "mon1.png" ne sont jamais touchés par erreur.
    - Si un AUTRE fichier du projet porte exactement le même nom (très
      courant pour les ressources Android dupliquées dans
      drawable-mdpi/hdpi/xhdpi...), on ne remplace QUE le chemin complet
      (ex: "res/drawable-hdpi/icon.png"), jamais le nom seul tout court,
      pour ne pas renommer par erreur les références visant l'homonyme.
    """
    sdir, src_p = _safe_target(sid, rel)
    _, dst_p    = _safe_target(sid, new_rel)
    if not src_p.exists():
        raise FileNotFoundError("Source introuvable")
    if dst_p.exists():
        raise FileExistsError("Destination déjà existante")

    old_name = src_p.name
    new_name = dst_p.name
    old_rel_fwd = rel.replace("\\", "/")
    new_rel_fwd = new_rel.replace("\\", "/")

    # Détecte les homonymes AVANT de déplacer le fichier (sinon il ne se
    # trouverait plus à son ancien chemin pour la comparaison).
    name_ambiguous = False
    if old_name != new_name:
        try:
            for f in sdir.rglob(old_name):
                if f.is_file() and f.resolve() != src_p.resolve():
                    name_ambiguous = True
                    break
        except OSError:
            pass

    dst_p.parent.mkdir(parents=True, exist_ok=True)
    src_p.rename(dst_p)

    refs_updated = 0
    if old_name == new_name:
        return {"refsUpdated": refs_updated, "nameAmbiguous": False}

    path_pat = re.compile(re.escape(old_rel_fwd))
    name_pat = re.compile(r'\b' + re.escape(old_name) + r'\b')

    for f in sdir.rglob("*"):
        if not f.is_file(): continue
        if f.suffix.lower() not in TEXT_EXTENSIONS and f.suffix != "": continue
        try:
            if f.stat().st_size > MAX_TEXT_SIZE: continue
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if old_name not in text and old_rel_fwd not in text:
            continue
        new_text = text
        if old_rel_fwd != old_name:
            new_text = path_pat.sub(new_rel_fwd, new_text)
        if not name_ambiguous:
            new_text = name_pat.sub(new_name, new_text)
        if new_text != text:
            try:
                f.write_text(new_text, encoding="utf-8")
                refs_updated += 1
            except OSError:
                continue
    return {"refsUpdated": refs_updated, "nameAmbiguous": name_ambiguous}


# =============================================================
# ESPACE DE TRAVAIL UNIVERSEL — détection du type d'APK décompilé
# =============================================================
# But : accepter N'IMPORTE QUEL APK (peu importe avec quoi il a été
# construit à l'origine) dans un seul espace de travail générique, puis
# dire au client (et à l'IA) avec QUOI ce projet a été fait, pour orienter
# la suite (quels outils installer, quels fichiers éditer). Détection
# 100% déterministe (empreintes de fichiers connues) — aucune IA requise
# pour CETTE étape ; l'IA prend ensuite le relais uniquement pour vérifier
# précisément quels outils manquent (check_missing_components) et chercher
# ceux qui ne sont pas dans le registre connu (search_missing_component,
# GitHub/miroirs) — jamais l'inverse.
def detect_decompiled_apk_type(sid):
    sdir = source_dir(sid)
    if not sdir.exists():
        return {"type": "none", "requiredTools": [], "markersFound": {}}

    assets_www      = sdir / "assets" / "www"
    has_www         = assets_www.exists()
    has_cordova_js  = (assets_www / "cordova.js").exists() or (assets_www / "cordova_plugins.js").exists()
    has_flutter_ast = (sdir / "assets" / "flutter_assets").exists()
    has_flutter_lib = (sdir / "lib").exists() and any(sdir.glob("lib/*/libflutter.so"))
    has_rn_bundle   = (sdir / "assets" / "index.android.bundle").exists()
    smali_root      = sdir / "smali"
    has_cordova_pkg = (smali_root / "org" / "apache" / "cordova").exists()
    has_flutter_pkg = (smali_root / "io" / "flutter").exists()
    has_react_pkg   = (smali_root / "com" / "facebook" / "react").exists()
    has_twa_pkg     = (smali_root / "com" / "google" / "androidbrowserhelper").exists()
    has_assetlinks  = (sdir / "res" / "raw").exists() and any((sdir / "res" / "raw").glob("assetlinks*"))

    if has_flutter_ast or has_flutter_lib or has_flutter_pkg:
        apk_type = "flutter"
    elif has_rn_bundle or has_react_pkg:
        apk_type = "reactnative"
    elif has_cordova_js or has_cordova_pkg:
        apk_type = "cordova"
    elif has_twa_pkg or has_assetlinks:
        apk_type = "twa"
    elif has_www:
        apk_type = "webview_custom"   # ex: apps "Site → App" maison, comme ce projet-ci
    else:
        apk_type = "native"

    LABELS = {
        "flutter":        "Flutter",
        "reactnative":    "React Native",
        "cordova":        "Cordova",
        "twa":            "Trusted Web Activity (TWA)",
        "webview_custom": "WebView personnalisée (type Site → App)",
        "native":         "Natif Java/Kotlin",
    }
    REQUIRED_TOOLS = {
        "flutter":        ["JDK", "Android SDK", "Gradle", "Flutter SDK"],
        "reactnative":    ["JDK", "Android SDK", "Gradle", "Node.js", "React Native CLI"],
        "cordova":        ["JDK", "Android SDK", "Gradle", "Node.js", "Cordova CLI"],
        "twa":            ["JDK", "Android SDK", "Gradle", "Bubblewrap"],
        "webview_custom": ["JDK", "Android SDK", "Gradle", "Apktool"],
        "native":         ["JDK", "Android SDK", "Gradle", "Kotlin"],
    }

    return {
        "type": apk_type,
        "label": LABELS[apk_type],
        "requiredTools": REQUIRED_TOOLS[apk_type],
        "markersFound": {
            "hasWww": has_www, "hasCordovaJs": has_cordova_js,
            "hasFlutterAssets": has_flutter_ast, "hasFlutterLib": has_flutter_lib,
            "hasReactBundle": has_rn_bundle, "hasTwaMarkers": has_twa_pkg or has_assetlinks,
        },
    }


# =============================================================
# SUPPRESSION FORCÉE + DÉPENDANCES — 100% déterministe, SANS IA
# =============================================================
# Contexte : le bouton "Supprimer avec l'IA" délègue la suppression à
# l'agent, qui peut se tromper (oublier d'appeler delete_path sur le bon
# fichier, ou croire avoir supprimé alors que non — cas observé : un
# fichier smali watermark laissé intact alors que l'IA annonçait l'avoir
# supprimé). Cette fonction ne dépend d'aucun modèle : elle supprime le
# fichier pour de vrai (vérifié après coup), puis scanne tout le projet
# en texte brut (comme search_content/rename_with_refs) pour retrouver
# les dépendances, avec 2 comportements garantis :
#  1) Nettoyage AUTOMATIQUE et sûr, mais UNIQUEMENT pour un motif smali
#     bien identifié et sans ambiguïté : "instancier puis exécuter
#     immédiatement" (new-instance + invoke-direct <init> + invoke du
#     Runnable via post/postDelayed/run/execute) — le cas typique des
#     classes watermark comme "MainActivity$g" (Runnable jetable posté
#     puis jamais réutilisé). Ce motif est retiré en bloc car il ne
#     laisse aucun registre "orphelin" derrière lui.
#  2) Pour tout le reste (référence trouvée mais motif non reconnu),
#     on ne devine PAS — on liste précisément fichier + ligne pour revue
#     manuelle ou via le badge ✨ IA, plutôt que de risquer de casser la
#     compilation avec une suppression de ligne aveugle.
def _extract_smali_class_name(text):
    m = re.search(r'^\.class[ \t]+(?:\S+[ \t]+)*(L[\w/$]+;)[ \t]*$', text, re.M)
    return m.group(1) if m else None

def force_delete_with_refs(sid, rel):
    sdir, target = _safe_target(sid, rel)
    if not target.exists():
        raise FileNotFoundError("Fichier ou dossier introuvable")

    is_dir = target.is_dir()
    old_name = target.name
    old_rel_fwd = rel.replace("\\", "/")

    class_slash = None
    class_dot = None
    if not is_dir and target.suffix.lower() == ".smali":
        try:
            txt = target.read_text(encoding="utf-8")
            class_slash = _extract_smali_class_name(txt)
            if class_slash:
                class_dot = class_slash[1:-1].replace("/", ".")
        except (UnicodeDecodeError, OSError):
            pass

    # 1) Suppression réelle — jamais déléguée à l'IA, jamais "supposée faite".
    if is_dir:
        shutil.rmtree(target)
    else:
        target.unlink()
    deleted_confirmed = not target.exists()

    # 2) Motif sûr de nettoyage auto (Runnable smali instancié + exécuté
    #    une seule fois, résultat jamais réutilisé ensuite).
    runnable_block_re = None
    if class_slash:
        cs = re.escape(class_slash)
        runnable_block_re = re.compile(
            r'[ \t]*new-instance (v\d+|p\d+), ' + cs + r'[ \t]*\r?\n'
            r'[ \t]*invoke-direct \{[^}\n]*\}, ' + cs + r'-><init>\([^)]*\)V[ \t]*\r?\n'
            r'(?:[ \t]*\r?\n)?'
            r'[ \t]*invoke-(?:virtual|interface) \{[^}\n]*\1[^}\n]*\}, [\w/$;.<>]+->(?:post|postDelayed|run|execute)\([^)]*\)[A-Za-z\[\];]*[ \t]*\r?\n?'
        )

    references = []
    auto_cleaned = []

    for f in sdir.rglob("*"):
        if not f.is_file(): continue
        if f.suffix.lower() not in TEXT_EXTENSIONS and f.suffix != "": continue
        try:
            if f.stat().st_size > MAX_TEXT_SIZE: continue
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        matched = (old_name in text or old_rel_fwd in text or
                   (class_slash and class_slash in text) or
                   (class_dot and class_dot in text))
        if not matched:
            continue

        rel_f = str(f.relative_to(sdir)).replace("\\", "/")

        cleaned_text, n = (runnable_block_re.subn('', text) if runnable_block_re else (text, 0))
        if n:
            try:
                f.write_text(cleaned_text, encoding="utf-8")
                auto_cleaned.append({"path": rel_f, "blocksRemoved": n})
                text = cleaned_text
            except OSError:
                pass

        for lineno, line in enumerate(text.split("\n"), start=1):
            if (old_name in line or old_rel_fwd in line or
                    (class_slash and class_slash in line) or
                    (class_dot and class_dot in line)):
                snippet = line.strip()
                if len(snippet) > 240: snippet = snippet[:240] + "…"
                references.append({"path": rel_f, "line": lineno, "text": snippet})

    return {
        "deleted": rel,
        "deletedConfirmed": deleted_confirmed,
        "classDetected": class_slash,
        "autoCleaned": auto_cleaned,
        "remainingReferences": references,
    }


def replace_in_file_line(sid, rel_path, line_no, old_text, new_text):
    """Remplace le contenu d'une ligne précise (vérifie l'ancien contenu pour éviter
    d'écraser un fichier modifié entre-temps par autre chose)."""
    sdir, target = _safe_target(sid, rel_path)
    if not target.is_file(): raise FileNotFoundError("Fichier introuvable")
    lines = target.read_text(encoding="utf-8").split("\n")
    idx = line_no - 1
    if idx < 0 or idx >= len(lines):
        raise IndexError("Numéro de ligne hors limites")
    if old_text is not None and lines[idx].strip() != old_text.strip():
        raise ValueError("Le contenu de la ligne a changé depuis le scan — relancez une recherche.")
    lines[idx] = new_text
    new_content = "\n".join(lines)
    # Même garde-fou que write_file_safe : replace_line était le seul chemin
    # d'écriture smali qui ne passait PAS par _smali_quick_check, ce qui
    # laissait passer une ligne du style d'un littéral de tableau Java
    # (ex: `{"a","b"}`) directement dans MainActivity.smali et ne cassait la
    # compilation apktool que bien plus tard, avec un message cryptique.
    if str(rel_path).endswith('.smali'):
        smali_errors = _smali_quick_check(rel_path, new_content)
        if smali_errors:
            raise ValueError(
                "Syntaxe smali invalide (ligne " + str(line_no) + " de '" + str(rel_path)
                + "') — fichier NON modifié :\n- " + "\n- ".join(smali_errors)
            )
    if str(rel_path).endswith(('.java', '.kt')) and _session_origin(sid) not in _GRADLE_JAVA_ORIGINS:
        raise ValueError(
            "Fichier NON modifié : '" + str(rel_path) + "' — ce type de projet compile uniquement via "
            "apktool sur du smali, aucun compilateur Java/Kotlin disponible ici. Édite le .smali existant "
            "à la place."
        )
    target.write_text(new_content, encoding="utf-8")
    _snapshot_history(sid, rel_path, new_content.encode("utf-8"))


# =============================================================
# SMALI FACILE — édition guidée sans connaître la syntaxe smali
# =============================================================
# Détecte automatiquement, dans tous les .smali du projet, les valeurs
# "courantes" qu'un utilisateur non-développeur voudrait modifier :
#   - couleurs codées en dur (const/4, const-string "#RRGGBB(AA)")
#   - chaînes de texte (const-string "...")
#   - durées d'animation/délai (const-wide/16, const/16, const ... suivi d'un
#     contexte d'appel setDuration/postDelayed/sleep)
#   - URLs codées en dur (const-string "http(s)://...")
# Chaque "fact" porte un identifiant stable basé sur fichier+ligne, vérifié
# avant écriture pour éviter de corrompre une ligne qui a changé entre-temps.

_RE_CONST_STRING = re.compile(r'^(\s*)const-string(?:/jumbo)?\s+(v\d+|p\d+),\s*"((?:[^"\\]|\\.)*)"\s*$')
_RE_CONST_NUM = re.compile(r'^(\s*)const(?:/4|/16|/high16|-wide(?:/16|/32|/high16)?)?\s+(v\d+|p\d+),\s*(-?0x[0-9a-fA-F]+|-?\d+)L?\s*$')
_HEX_COLOR_RE = re.compile(r'^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_URL_RE = re.compile(r'^https?://', re.IGNORECASE)
_DURATION_HINT_RE = re.compile(r'setDuration|postDelayed|Thread;->sleep|startDelay|setStartDelay', re.IGNORECASE)

def _smali_unescape(s):
    return s.encode("utf-8").decode("unicode_escape", errors="replace") if "\\" in s else s

def _smali_escape(s):
    out = []
    for ch in s:
        if ch == '"': out.append('\\"')
        elif ch == '\\': out.append('\\\\')
        elif ch == '\n': out.append('\\n')
        elif ch == '\t': out.append('\\t')
        else: out.append(ch)
    return "".join(out)

def scan_smali_facts(sid, max_facts=800):
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    facts = []
    if not sdir.exists():
        return facts

    for f in sorted(sdir.rglob("*.smali")):
        if len(facts) >= max_facts: break
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(f.relative_to(sdir)).replace("\\", "/")
        lines = text.split("\n")
        for i, raw_line in enumerate(lines):
            if len(facts) >= max_facts: break
            lineno = i + 1

            m = _RE_CONST_STRING.match(raw_line)
            if m:
                _, reg, raw_val = m.groups()
                val = _smali_unescape(raw_val)
                if _URL_RE.match(val):
                    ftype, label = "url", "URL codée en dur"
                elif _HEX_COLOR_RE.match(val):
                    ftype, label = "color", "Couleur (texte)"
                else:
                    ftype, label = "text", "Texte"
                facts.append({
                    "id": f"{rel}:{lineno}",
                    "type": ftype, "label": label,
                    "file": rel, "line": lineno,
                    "value": val,
                    "origLine": raw_line.strip(),
                    "context": _smali_context(lines, i),
                })
                continue

            m = _RE_CONST_NUM.match(raw_line)
            if m:
                _, reg, num = m.groups()
                try:
                    n = int(num, 16) if num.lower().startswith(("0x", "-0x")) else int(num)
                except ValueError:
                    continue
                # Couleur ARGB en hex (valeurs négatives = bit de signe ARGB)
                is_color_like = num.lower().startswith(("0x", "-0x")) and (n == 0 or abs(n) > 0xFF)
                ctx = "\n".join(lines[max(0, i - 1):min(len(lines), i + 4)])
                if is_color_like and re.search(r'setColor|setBackgroundColor|setTextColor|Color;->', ctx):
                    facts.append({
                        "id": f"{rel}:{lineno}",
                        "type": "color_int", "label": "Couleur (ARGB)",
                        "file": rel, "line": lineno,
                        "value": f"#{(n & 0xFFFFFFFF):08X}",
                        "origLine": raw_line.strip(),
                        "context": _smali_context(lines, i),
                    })
                elif _DURATION_HINT_RE.search(ctx) and 0 <= n <= 600000:
                    facts.append({
                        "id": f"{rel}:{lineno}",
                        "type": "duration", "label": "Durée (ms)",
                        "file": rel, "line": lineno,
                        "value": str(n),
                        "origLine": raw_line.strip(),
                        "context": _smali_context(lines, i),
                    })
                continue

    return facts

def _smali_context(lines, i):
    start, end = max(0, i - 1), min(len(lines), i + 2)
    return "\n".join(l.strip() for l in lines[start:end] if l.strip())

def apply_smali_fact(sid, fact_id, new_value):
    rel, lineno_s = fact_id.rsplit(":", 1)
    lineno = int(lineno_s)
    sdir, target = _safe_target(sid, rel)
    if not target.is_file(): raise FileNotFoundError("Fichier introuvable")
    lines = target.read_text(encoding="utf-8").split("\n")
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        raise IndexError("Ligne hors limites — relancez un scan")
    raw_line = lines[idx]

    m = _RE_CONST_STRING.match(raw_line)
    if m:
        indent, reg, _ = m.groups()
        lines[idx] = f'{indent}const-string {reg}, "{_smali_escape(new_value)}"'
        target.write_text("\n".join(lines), encoding="utf-8")
        return

    m = _RE_CONST_NUM.match(raw_line)
    if m:
        indent, reg, _ = m.groups()
        nv = new_value.strip()
        # Couleur hex (#AARRGGBB / #RRGGBB) → entier signé 32 bits pour const/const-wide
        if nv.startswith("#"):
            hexpart = nv[1:]
            if len(hexpart) == 6: hexpart = "FF" + hexpart  # opacité pleine par défaut
            n = int(hexpart, 16)
            if n & 0x80000000:
                n = n - 0x100000000
            num_repr = hex(n) if n >= 0 else "-0x%x" % (-n)
            is_wide = "-wide" in raw_line
        else:
            n = int(nv)
            num_repr = str(n)
            is_wide = "-wide" in raw_line
        opcode_m = re.match(r'^\s*(const\S*)', raw_line)
        opcode = opcode_m.group(1) if opcode_m else "const"
        suffix = "L" if is_wide and not nv.startswith("#") else ""
        lines[idx] = f'{indent}{opcode} {reg}, {num_repr}{suffix}'
        target.write_text("\n".join(lines), encoding="utf-8")
        return

    raise ValueError("Ligne non reconnue (le fichier a peut-être changé) — relancez un scan")


# =============================================================
# MODE PROFONDEUR — désactiver TOUT un bloc affiché (pas juste son texte)
# =============================================================
# Vider un const-string (apply_smali_fact avec "") ne supprime que le texte :
# le popup/dialogue qui l'affiche (fond, boutons, marges...) reste visible,
# juste vide. Ici on va plus loin : on repère la méthode smali qui ENGLOBE
# la ligne de texte cliquée (typiquement la méthode qui construit et
# affiche le dialogue/bandeau), et on la neutralise en lui injectant un
# "return-void" tout au début — la méthode ne fait alors plus RIEN du tout
# quand elle est appelée (plus de dialogue, plus de fond blanc, plus de
# boutons), sans toucher au reste de la classe ni risquer de casser le
# build (aucune ligne n'est supprimée, seulement une short-circuit ajoutée).
# Limite assumée : uniquement sûr pour une méthode de retour void (V) —
# pour tout le reste, on refuse et on renvoie une explication claire plutôt
# que de deviner une valeur de retour.
_RE_METHOD_START = re.compile(r'^\s*\.method\b.*\)([\[A-Za-z][\w/$;\[]*)\s*$')
_RE_METHOD_END = re.compile(r'^\s*\.end method\s*$')
_RE_LOCALS_OR_REGISTERS = re.compile(r'^\s*\.(locals|registers)\s+\d+\s*$')

def find_enclosing_smali_method(lines, at_idx):
    """Retourne (start_idx, end_idx, return_type) de la méthode .method/.end
    method qui contient la ligne at_idx (index 0-based), ou None."""
    start = None
    for i in range(at_idx, -1, -1):
        if _RE_METHOD_END.match(lines[i]) and i != at_idx:
            break  # on a dépassé la méthode précédente sans trouver de .method : pas englobé
        m = _RE_METHOD_START.match(lines[i])
        if m:
            start = i
            ret_type = m.group(1)
            break
    if start is None:
        return None
    end = None
    for i in range(start, len(lines)):
        if _RE_METHOD_END.match(lines[i]):
            end = i
            break
    if end is None or end < at_idx:
        return None
    return start, end, ret_type

def disable_ui_fact_block(sid, fact_id):
    """Neutralise toute la méthode smali qui affiche l'élément détecté par
    /ui-facts (id au format 'chemin/relatif.smali:ligne'), pour désactiver
    tout le bloc visuel (popup, fond, boutons...) et pas juste son texte."""
    if fact_id.startswith("strres::") or fact_id.startswith("layout::"):
        raise ValueError(
            "Cette action n'est possible que pour un texte détecté dans le code (smali) — "
            "ce texte vient de strings.xml ou d'un layout, qui n'a pas de méthode à désactiver ici."
        )
    rel, lineno_s = fact_id.rsplit(":", 1)
    lineno = int(lineno_s)
    sdir, target = _safe_target(sid, rel)
    if not target.is_file():
        raise FileNotFoundError("Fichier introuvable — relancez un scan")
    lines = target.read_text(encoding="utf-8").split("\n")
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        raise IndexError("Ligne hors limites — relancez un scan")

    found = find_enclosing_smali_method(lines, idx)
    if not found:
        raise ValueError("Impossible de repérer la méthode englobante — relancez un scan")
    start, end, ret_type = found

    if ret_type != "V":
        raise ValueError(
            "Cette méthode retourne une valeur (pas void) : la désactiver automatiquement "
            "risquerait de casser le build. Utilise plutôt \"Supprimer\" pour vider le texte, "
            "ou modifie-la manuellement dans l'éditeur."
        )

    # Déjà désactivée ? (idempotent, évite les doublons si l'action est cliquée 2x)
    body_start = start + 1
    for i in range(body_start, end):
        if lines[i].strip() == "return-void":
            return {"alreadyDisabled": True, "file": rel, "methodLine": start + 1}
        if lines[i].strip() and not (_RE_LOCALS_OR_REGISTERS.match(lines[i]) or lines[i].strip().startswith(".param") or lines[i].strip().startswith(".annotation") or lines[i].strip().startswith(".end annotation")):
            break

    # Insère "return-void" juste après la directive .locals/.registers
    # (obligatoire en tout premier dans le corps d'une méthode smali).
    insert_at = body_start
    for i in range(body_start, end):
        if _RE_LOCALS_OR_REGISTERS.match(lines[i]):
            insert_at = i + 1
            break
    indent = "    "
    lines.insert(insert_at, f"{indent}return-void")
    target.write_text("\n".join(lines), encoding="utf-8")
    method_sig = lines[start].strip()
    return {"alreadyDisabled": False, "file": rel, "methodLine": start + 1, "method": method_sig}


# =============================================================
# MODE PROFONDEUR — bouton dev "Désactiver TOUTES les popups en dur"
# =============================================================
# Contrairement à disable_ui_fact_block (une popup ciblée à la fois), ceci
# scanne TOUT le projet smali pour repérer chaque méthode qui affiche un
# dialogue/popup codé en dur (AlertDialog, Dialog, DialogFragment,
# PopupWindow...) et neutralise chacune d'un coup — même mécanisme sûr :
# injection d'un "return-void" en tête de méthode, uniquement si la méthode
# est void, jamais de suppression de ligne. Une méthode déjà neutralisée
# ou non-void est simplement listée à part (revue manuelle), jamais devinée.
_RE_POPUP_SHOW_CALL = re.compile(
    r'invoke-\w+\s+\{[^}]*\},\s*L[\w/$]*(?:Dialog|PopupWindow)[\w/$]*;->show\w*\('
)

def scan_popup_show_methods(sid):
    """Repère, dans tout le projet, chaque méthode smali qui contient au
    moins un appel show()/showNow()/showAsDropDown() sur un Dialog/
    AlertDialog/DialogFragment/PopupWindow. Une méthode par entrée (dédupe
    si plusieurs appels dans la même méthode)."""
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    found = []
    seen = set()
    if not sdir.exists():
        return found
    for f in sorted(sdir.rglob("*.smali")):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "show" not in text:
            continue  # évite de re-splitter en lignes les fichiers sans intérêt
        rel = str(f.relative_to(sdir)).replace("\\", "/")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not _RE_POPUP_SHOW_CALL.search(line):
                continue
            enclosing = find_enclosing_smali_method(lines, i)
            if not enclosing:
                continue
            start, end, ret_type = enclosing
            key = (rel, start)
            if key in seen:
                continue
            seen.add(key)
            found.append({"file": rel, "callLine": i + 1, "methodStart": start,
                          "methodEnd": end, "retType": ret_type,
                          "method": lines[start].strip()})
    return found

_RE_LOCALS_DIRECTIVE = re.compile(r'^(\s*)\.locals\s+(\d+)\s*$')
_RE_REGISTERS_DIRECTIVE = re.compile(r'^(\s*)\.registers\s+(\d+)\s*$')

def _return_stub_for_type(ret_type, reg="v0"):
    """Construit les instructions smali d'un retour "par défaut" correct
    pour un type de retour donné. Un simple 'return-void' inséré dans une
    méthode déclarée non-void est rejeté par le vérifieur Dalvik (crash au
    lancement) : il faut un 'return'/'return-wide'/'return-object' avec la
    bonne valeur par défaut (0 / false / null)."""
    if ret_type == "V":
        return ["return-void"]
    if ret_type in ("J", "D"):
        return [f"const-wide/16 {reg}, 0x0", f"return-wide {reg}"]
    if ret_type in ("Z", "B", "S", "C", "I", "F"):
        return [f"const/4 {reg}, 0x0", f"return {reg}"]
    # Type objet (Lpkg/Class;) ou tableau ([...) : on retourne null.
    return [f"const/4 {reg}, 0x0", f"return-object {reg}"]

def disable_all_popup_methods(sid, force=False):
    """Neutralise chaque méthode repérée par scan_popup_show_methods.

    - Méthode void, pas déjà désactivée -> 'disabled' ('return-void' en
      tête, comportement historique inchangé).
    - Méthode déjà neutralisée -> 'already_disabled'.
    - Méthode non-void :
        - force=False (par défaut) -> 'skipped_nonvoid', listée à part
          pour revue manuelle, rien n'est touché (comportement historique).
        - force=True -> on tente un retour par défaut du bon type
          ('disabled_forced'), en augmentant '.locals' d'une unité si
          besoin pour disposer d'un registre local. Seul le cas '.locals'
          est géré : une méthode en '.registers' (registres bruts, params
          compris) est trop risquée à recalculer ici et reste
          'skipped_nonvoid' même avec force=True.

    Chaque décision (désactivée / déjà désactivée / ignorée / forcée /
    erreur) est à la fois renvoyée dans `results` et loguée côté serveur
    (print, visible dans les logs du process) pour audit.

    Traite les méthodes d'un même fichier du bas vers le haut pour que les
    numéros de ligne déjà calculés restent valides après chaque insertion."""
    methods = scan_popup_show_methods(sid)
    by_file = {}
    for m in methods:
        by_file.setdefault(m["file"], []).append(m)

    results = []
    for rel, ms in by_file.items():
        _, target = _safe_target(sid, rel)
        if not target.is_file():
            for m in ms:
                results.append({"file": rel, "method": m["method"], "status": "error", "error": "Fichier introuvable"})
                print(f"[popups] ERREUR {rel} :: {m['method']} -> fichier introuvable")
            continue
        lines = target.read_text(encoding="utf-8").split("\n")
        changed = False
        # Du bas vers le haut : une insertion plus bas ne décale jamais les
        # méthodes situées au-dessus, donc pas besoin de recalculer.
        for m in sorted(ms, key=lambda x: -x["methodStart"]):
            start, end, ret_type = m["methodStart"], m["methodEnd"], m["retType"]
            if start >= len(lines) or end >= len(lines) or not _RE_METHOD_START.match(lines[start]):
                results.append({"file": rel, "method": m["method"], "status": "error", "error": "Le fichier a changé — relancez le scan"})
                print(f"[popups] ERREUR {rel} :: {m['method']} -> fichier modifié depuis le scan")
                continue

            body_start = start + 1
            already = any(lines[i].strip() == "return-void" for i in range(body_start, min(body_start + 3, end)))
            if already:
                results.append({"file": rel, "method": m["method"], "status": "already_disabled"})
                print(f"[popups] DEJA DESACTIVEE {rel}:{start+1} :: {m['method']}")
                continue

            if ret_type != "V":
                if not force:
                    results.append({"file": rel, "method": m["method"], "status": "skipped_nonvoid", "retType": ret_type})
                    print(f"[popups] IGNOREE (non-void, retour={ret_type}) {rel}:{start+1} :: {m['method']}")
                    continue
                # force=True : on ne gère que le cas '.locals'. '.registers'
                # mélange params+locaux et est trop risqué à recalculer ici.
                locals_idx, locals_n = None, None
                for i in range(body_start, end):
                    lm = _RE_LOCALS_DIRECTIVE.match(lines[i])
                    if lm:
                        locals_idx, locals_n = i, int(lm.group(2))
                        break
                    if _RE_REGISTERS_DIRECTIVE.match(lines[i]):
                        break
                if locals_idx is None:
                    results.append({"file": rel, "method": m["method"], "status": "skipped_nonvoid", "retType": ret_type,
                                     "error": "force impossible (méthode en .registers, pas .locals)"})
                    print(f"[popups] IGNOREE MEME AVEC FORCE (.registers, retour={ret_type}) {rel}:{start+1} :: {m['method']}")
                    continue
                if locals_n < 1:
                    lines[locals_idx] = re.sub(r'\.locals\s+\d+', '.locals 1', lines[locals_idx])
                stub = _return_stub_for_type(ret_type, "v0")
                insert_at = locals_idx + 1
                for j, instr in enumerate(stub):
                    lines.insert(insert_at + j, f"    {instr}")
                changed = True
                results.append({"file": rel, "method": m["method"], "methodLine": start + 1, "status": "disabled_forced", "retType": ret_type})
                print(f"[popups] DESACTIVEE (FORCEE, retour={ret_type} -> valeur par défaut) {rel}:{start+1} :: {m['method']}")
                continue

            insert_at = body_start
            for i in range(body_start, end):
                if _RE_LOCALS_OR_REGISTERS.match(lines[i]):
                    insert_at = i + 1
                    break
            lines.insert(insert_at, "    return-void")
            changed = True
            results.append({"file": rel, "method": m["method"], "methodLine": start + 1, "status": "disabled"})
            print(f"[popups] DESACTIVEE {rel}:{start+1} :: {m['method']}")
        if changed:
            target.write_text("\n".join(lines), encoding="utf-8")
    return results


# =============================================================
# MODE PROFONDEUR — vue visuelle de TOUT le texte/boutons affichés
# =============================================================
# Contrairement à scan_smali_facts (constantes brutes dans le bytecode),
# ce scan couvre les deux autres sources de texte affiché à l'écran :
#   - res/values*/strings.xml   → tout le texte "officiel" de l'app
#   - res/layout*/*.xml         → les libellés écrits en dur directement
#     dans un layout (android:text="...") sur un bouton/label/champ, sans
#     passer par strings.xml
# Chaque item est identifiable et modifiable individuellement, et le
# fichier n'est jamais réécrit intégralement : seule la ligne concernée
# est remplacée, pour ne jamais risquer de casser le XML autour.

_RE_STRING_ENTRY = re.compile(r'<string\s+name="([^"]+)"([^>]*)>(.*?)</string>', re.S)
_RE_LAYOUT_TEXT_ATTR = re.compile(r'android:text\s*=\s*"([^"]*)"')
_RE_LAYOUT_TAG_OPEN = re.compile(r'<([A-Za-z][\w.]*)\b')

# Un APK décompilé fusionne dans un SEUL res/values/strings.xml des milliers
# d'entrées venant des bibliothèques (Material Components, AppCompat, Google
# Play Services, ExoPlayer...) en plus des vraies chaînes de l'app. Sans
# filtre, ces entrées de bibliothèque (souvent 1000+) remplissent le budget
# max_facts et noient les textes propres à l'app (dont un éventuel bandeau
# "Important Notice" / watermark injecté par le build) qui n'apparaissent
# alors JAMAIS dans le Mode Profondeur. On exclut donc par préfixe de nom
# les familles connues de bibliothèques — ce qui laisse remonter en
# priorité les chaînes réellement propres à l'app.
_LIBRARY_STRING_NAME_PREFIXES = (
    "mtrl_", "m3_", "m3c_", "abc_", "androidx_", "common_google_",
    "firebase_", "exo_", "cast_", "gcm_", "status_bar_", "search_menu_",
    "bottom_sheet_", "character_counter_", "clear_text_", "error_icon_",
    "hide_bottom_view_on_scroll_", "icon_content_description", "item_view_role_description",
    "password_toggle_", "path_password_", "appbar_scrolling_view_behavior",
)

def _is_library_string_name(name):
    return any(name.startswith(p) for p in _LIBRARY_STRING_NAME_PREFIXES)

# Tags Android correspondant à un "bouton" au sens large, pour affichage
# groupé côté client (icône différente, filtre "Boutons" à part).
_BUTTON_TAGS = {"Button", "ImageButton", "ToggleButton", "Switch", "RadioButton",
                "CheckBox", "FloatingActionButton", "MaterialButton",
                "AppCompatButton", "AppCompatImageButton"}

def _ui_values_dirs(sdir):
    res = sdir / "res"
    if not res.exists():
        return []
    return sorted(res.glob("values*"))

def scan_string_res_facts(sid, max_facts=1500):
    """Scanne res/values*/strings.xml : un fact par <string name=...>...</string>."""
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    facts = []
    if not sdir.exists():
        return facts
    for vd in _ui_values_dirs(sdir):
        f = vd / "strings.xml"
        if not f.exists() or len(facts) >= max_facts:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(f.relative_to(sdir)).replace("\\", "/")
        for m in _RE_STRING_ENTRY.finditer(text):
            if len(facts) >= max_facts:
                break
            name, attrs, raw_val = m.groups()
            if _is_library_string_name(name):
                continue  # bruit de bibliothèque (Material/AppCompat/...) : jamais du contenu de l'app
            # Retire les balises de mise en forme (<b>, <i>...) pour l'affichage,
            # mais on réécrit toujours le contenu BRUT tel quel à l'application.
            display_val = re.sub(r'</?[a-zA-Z][^>]*>', '', raw_val).strip()
            facts.append({
                "id": f"strres::{rel}::{name}",
                "type": "string_res",
                "label": "Texte (strings.xml)",
                "group": name,
                "file": rel,
                "value": display_val,
                "translatable": ('translatable="false"' not in attrs),
            })
    return facts

def scan_layout_text_facts(sid, max_facts=1500):
    """Scanne res/layout*/*.xml : un fact par android:text="litéral" (pas
    @string/xxx, déjà couvert par scan_string_res_facts), avec le tag de
    l'élément (Button, TextView, EditText...) pour grouper visuellement
    par type côté client."""
    sdir_v3 = source_dir(sid)
    sdir_v2 = session_dir(sid) / "decompiled"
    sdir = sdir_v3 if sdir_v3.exists() else sdir_v2
    facts = []
    if not sdir.exists():
        return facts
    res = sdir / "res"
    if not res.exists():
        return facts
    layout_dirs = sorted(res.glob("layout*"))
    for ld in layout_dirs:
        if not ld.is_dir():
            continue
        for f in sorted(ld.glob("*.xml")):
            if len(facts) >= max_facts:
                break
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(f.relative_to(sdir)).replace("\\", "/")
            lines = text.split("\n")
            # Pour retrouver le tag Android (Button/TextView/...) de l'élément
            # qui contient chaque android:text, on remonte depuis la ligne du
            # match jusqu'à la dernière balise ouvrante rencontrée au-dessus.
            for i, line in enumerate(lines):
                if len(facts) >= max_facts:
                    break
                m = _RE_LAYOUT_TEXT_ATTR.search(line)
                if not m:
                    continue
                val = m.group(1)
                if val.startswith("@string/") or val.startswith("@android:string/") or not val.strip():
                    continue  # référence ou vide : rien à éditer ici
                tag = "Vue"
                for j in range(i, -1, -1):
                    tm = _RE_LAYOUT_TAG_OPEN.search(lines[j])
                    if tm:
                        tag = tm.group(1).split(".")[-1]
                        break
                facts.append({
                    "id": f"layout::{rel}::{i + 1}",
                    "type": "layout_button" if tag in _BUTTON_TAGS else "layout_text",
                    "label": f"{'Bouton' if tag in _BUTTON_TAGS else 'Texte'} ({tag})",
                    "group": tag,
                    "file": rel,
                    "line": i + 1,
                    "value": val,
                })
    return facts

def scan_ui_facts(sid, max_facts=2500):
    """Combine strings.xml + layouts + textes smali bruts en une seule liste,
    pour le panneau "Mode Profondeur" : TOUT le texte/boutons affichés dans
    l'APK, visuellement, modifiable ou supprimable sans casser le build.

    Ordre volontaire : smali puis layouts D'ABORD, strings.xml en dernier.
    Raison : strings.xml d'un APK décompilé peut contenir des milliers
    d'entrées de bibliothèque (déjà filtrées par nom quand reconnues, mais
    des variantes inconnues peuvent rester) alors que le texte propre à
    l'app (dont un éventuel bandeau/watermark injecté au build) vit presque
    toujours dans le smali ou les layouts de l'app. En les scannant en
    premier, ils ne sont jamais tronqués par le plafond max_facts."""
    facts = []
    facts += scan_layout_text_facts(sid, max_facts=max_facts)
    if len(facts) < max_facts:
        smali_texts = [f for f in scan_smali_facts(sid, max_facts=max_facts - len(facts)) if f["type"] == "text"]
        facts += smali_texts
    if len(facts) < max_facts:
        facts += scan_string_res_facts(sid, max_facts=max_facts - len(facts))
    return facts[:max_facts]

def apply_ui_fact(sid, fact_id, new_value):
    """Applique l'édition d'un fact du Mode Profondeur. Dispatch selon le
    préfixe de l'id vers la bonne stratégie d'écriture (strings.xml,
    layout XML, ou smali — réutilise apply_smali_fact pour ce dernier)."""
    if fact_id.startswith("strres::"):
        _, rel, name = fact_id.split("::", 2)
        _, target = _safe_target(sid, rel)
        if not target.is_file():
            raise FileNotFoundError("Fichier strings.xml introuvable — relancez un scan")
        text = target.read_text(encoding="utf-8")
        def _repl(m):
            nm, attrs, _old = m.groups()
            if nm != name:
                return m.group(0)
            esc_val = (new_value.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;").replace("'", "\\'"))
            return f'<string name="{nm}"{attrs}>{esc_val}</string>'
        new_text, n = _RE_STRING_ENTRY.subn(_repl, text)
        if n == 0 or name not in text:
            raise ValueError(f"Entrée '{name}' introuvable dans {rel} — relancez un scan")
        target.write_text(new_text, encoding="utf-8")
        return
    if fact_id.startswith("layout::"):
        _, rel, lineno_s = fact_id.split("::", 2)
        lineno = int(lineno_s)
        _, target = _safe_target(sid, rel)
        if not target.is_file():
            raise FileNotFoundError("Fichier layout introuvable — relancez un scan")
        lines = target.read_text(encoding="utf-8").split("\n")
        idx = lineno - 1
        if idx < 0 or idx >= len(lines):
            raise IndexError("Ligne hors limites — relancez un scan")
        esc_val = (new_value.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;").replace('"', "&quot;"))
        new_line, n = _RE_LAYOUT_TEXT_ATTR.subn(f'android:text="{esc_val}"', lines[idx], count=1)
        if n == 0:
            raise ValueError("android:text introuvable sur cette ligne — relancez un scan")
        lines[idx] = new_line
        target.write_text("\n".join(lines), encoding="utf-8")
        return
    # Sinon : fact de type smali (const-string texte), même mécanisme que
    # l'onglet Smali Facile.
    apply_smali_fact(sid, fact_id, new_value)


# =============================================================
# AXML (Android Binary XML) — édition SÛRE du string pool
# =============================================================
# Format ResXMLTree (compilé par aapt) : un en-tête racine (type=0x0003),
# immédiatement suivi du chunk ResStringPool (type=0x0001), puis d'un chunk
# resource-map et des noeuds namespace/élément qui référencent les chaînes
# par INDEX (jamais par offset).
#
# Tant qu'on ne change ni le nombre ni l'ordre des chaînes, tous les index
# utilisés ailleurs dans le fichier restent valides à l'identique. On peut
# donc remplacer le texte d'une chaîne en toute sécurité : il suffit de
# reconstruire le bloc de données du string pool (offsets + octets encodés)
# et de propager la nouvelle taille du chunk dans l'en-tête racine. Le reste
# du fichier (resource map, arbre XML) est recopié tel quel, jamais touché.

RES_STRING_POOL_TYPE = 0x0001
UTF8_FLAG = 1 << 8

class AxmlError(Exception):
    pass

def is_axml(data: bytes) -> bool:
    if len(data) < 8: return False
    try:
        chunk_type, header_size, size = struct.unpack_from('<HHI', data, 0)
    except struct.error:
        return False
    return chunk_type == 0x0003 and header_size == 8 and size == len(data)

def _axml_read_len_utf8(buf, pos):
    b0 = buf[pos]
    if b0 & 0x80:
        return ((b0 & 0x7F) << 8) | buf[pos + 1], pos + 2
    return b0, pos + 1

def _axml_enc_len_utf8(n):
    if n > 0x7FFF: raise AxmlError("Chaîne trop longue pour l'encodage UTF-8 du string pool")
    if n > 0x7F: return bytes([0x80 | (n >> 8), n & 0xFF])
    return bytes([n])

def _axml_decode_utf8_string(buf, pos):
    _utf16_len, pos = _axml_read_len_utf8(buf, pos)          # longueur en unités UTF-16 (non utilisée pour décoder)
    byte_len, pos = _axml_read_len_utf8(buf, pos)
    s = buf[pos:pos + byte_len].decode('utf-8', errors='replace')
    return s, pos + byte_len + 1                               # +1 = terminateur \x00

def _axml_encode_utf8_string(s: str) -> bytes:
    raw = s.encode('utf-8')
    utf16_len = len(s.encode('utf-16-le')) // 2
    return _axml_enc_len_utf8(utf16_len) + _axml_enc_len_utf8(len(raw)) + raw + b'\x00'

def _axml_read_len_utf16(buf, pos):
    v0 = struct.unpack_from('<H', buf, pos)[0]
    if v0 & 0x8000:
        v1 = struct.unpack_from('<H', buf, pos + 2)[0]
        return ((v0 & 0x7FFF) << 16) | v1, pos + 4
    return v0, pos + 2

def _axml_enc_len_utf16(n):
    if n > 0x7FFFFFFF: raise AxmlError("Chaîne trop longue pour l'encodage UTF-16 du string pool")
    if n > 0x7FFF: return struct.pack('<HH', 0x8000 | (n >> 16), n & 0xFFFF)
    return struct.pack('<H', n)

def _axml_decode_utf16_string(buf, pos):
    char_len, pos = _axml_read_len_utf16(buf, pos)
    byte_len = char_len * 2
    s = buf[pos:pos + byte_len].decode('utf-16-le', errors='replace')
    return s, pos + byte_len + 2                               # +2 = terminateur \x00\x00

def _axml_encode_utf16_string(s: str) -> bytes:
    raw = s.encode('utf-16-le')
    return _axml_enc_len_utf16(len(raw) // 2) + raw + b'\x00\x00'

def read_axml_strings(data: bytes):
    """Extrait les chaînes du string pool sans rien modifier."""
    if not is_axml(data):
        raise AxmlError("En-tête XML binaire (AXML) non reconnu")
    sp_off = 8
    chunk_type, header_size, sp_size = struct.unpack_from('<HHI', data, sp_off)
    if chunk_type != RES_STRING_POOL_TYPE:
        raise AxmlError("String pool introuvable juste après l'en-tête racine")
    string_count, style_count, flags, strings_start, styles_start = \
        struct.unpack_from('<IIIII', data, sp_off + 8)
    utf8 = bool(flags & UTF8_FLAG)
    offsets = struct.unpack_from(f'<{string_count}I', data, sp_off + 28)
    base = sp_off + strings_start
    strings = []
    for off in offsets:
        s, _ = (_axml_decode_utf8_string(data, base + off) if utf8
                 else _axml_decode_utf16_string(data, base + off))
        strings.append(s)
    return {"strings": strings, "utf8": utf8, "styleCount": style_count}

def rewrite_axml_strings(data: bytes, new_strings: list) -> bytes:
    """Remplace le TEXTE des chaînes du string pool sans changer leur nombre
    ni leur ordre — les index référencés ailleurs dans l'arbre restent donc
    valides et le fichier ne peut pas être corrompu structurellement."""
    if not is_axml(data):
        raise AxmlError("En-tête XML binaire (AXML) non reconnu")
    sp_off = 8
    chunk_type, header_size, sp_size = struct.unpack_from('<HHI', data, sp_off)
    if chunk_type != RES_STRING_POOL_TYPE:
        raise AxmlError("String pool introuvable")
    string_count, style_count, flags, strings_start, styles_start = \
        struct.unpack_from('<IIIII', data, sp_off + 8)
    if len(new_strings) != string_count:
        raise AxmlError(
            f"Le nombre de chaînes doit rester exactement {string_count} "
            f"(reçu {len(new_strings)}) — ajouter/supprimer une chaîne décalerait "
            f"tous les index référencés dans l'arbre XML et corromprait le fichier."
        )
    utf8 = bool(flags & UTF8_FLAG)
    encode = _axml_encode_utf8_string if utf8 else _axml_encode_utf16_string

    encoded = [encode(s) for s in new_strings]
    offsets, str_data = [], bytearray()
    for enc in encoded:
        offsets.append(len(str_data))
        str_data += enc
    while len(str_data) % 4 != 0:
        str_data += b'\x00'

    style_off_bytes = b''
    style_block = b''
    new_styles_start = 0
    if style_count > 0:
        style_off_bytes = data[sp_off + 28 + 4 * string_count: sp_off + 28 + 4 * string_count + 4 * style_count]
        old_style_start = sp_off + styles_start
        old_chunk_end = sp_off + sp_size
        style_block = bytes(data[old_style_start:old_chunk_end])
        new_styles_start = 28 + 4 * string_count + 4 * style_count + len(str_data)

    new_strings_start = 28 + 4 * string_count + 4 * style_count
    body_no_pad = (new_strings_start + len(str_data) + len(style_block))
    pad = (-body_no_pad) % 4
    new_sp_size = body_no_pad + pad

    sp_header = struct.pack('<HHI', RES_STRING_POOL_TYPE, 28, new_sp_size)
    sp_body = struct.pack('<IIIII', string_count, style_count, flags, new_strings_start, new_styles_start)
    offsets_bytes = struct.pack(f'<{string_count}I', *offsets)

    new_sp_chunk = (sp_header + sp_body + offsets_bytes + style_off_bytes +
                     bytes(str_data) + style_block + (b'\x00' * pad))

    rest = data[sp_off + sp_size:]                 # tout le reste du fichier, INCHANGÉ
    new_total_size = 8 + len(new_sp_chunk) + len(rest)

    out = bytearray()
    out += data[0:4]                                 # type + headerSize racine (inchangés)
    out += struct.pack('<I', new_total_size)          # taille racine recalculée
    out += new_sp_chunk
    out += rest
    return bytes(out)


# =============================================================
# CRÉATION DU PROJET DE ZÉRO (SANS TEMPLATE)
# =============================================================
_SCRATCH_SKELETON_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>App</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
  <div id="app">
    <h1>Nouvelle app</h1>
    <p>Remplace ce contenu (assets/index.html, assets/style.css, assets/app.js).</p>
  </div>
  <script src="app.js"></script>
</body>
</html>
"""

_SCRATCH_SKELETON_CSS = """body { margin: 0; font-family: sans-serif; padding: 16px; }
#app { max-width: 100%; }
"""

_SCRATCH_SKELETON_JS = """// app.js — logique de l'app scratch
console.log("app.js chargé");
"""


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
    # Plein écran : décoché par défaut dans le builder → l'app reste dans
    # les limites normales de l'écran (barres système visibles).
    fullscreen   = bool(config.get("fullscreen", False))
    # Immersif : cache la barre de navigation (Retour/Accueil/Récents).
    immersive    = bool(config.get("immersive", False))
    # Screen pinning : verrouille l'app à l'écran (startLockTask()).
    lock_task    = bool(config.get("lockTask", False))

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
        _smali_main(package, wv_arg, wv_mode, orientation, permissions=permissions, fullscreen=fullscreen, immersive=immersive, lock_task=lock_task), encoding="utf-8"
    )
    (smali_pkg / "InternalWebViewClient.smali").write_text(
        _smali_webviewclient(package), encoding="utf-8"
    )
    (smali_pkg / "InternalWebChromeClient.smali").write_text(
        _smali_webchromeclient(package), encoding="utf-8"
    )
    (smali_pkg / "InternalInsetsListener.smali").write_text(
        _smali_insetslistener(package), encoding="utf-8"
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

    if mode == "html":
        # SQUELETTE SYSTÉMATIQUE — même si html_inline est vide (cas où l'IA
        # va écrire index.html/style.css/app.js ensuite via write_file), on
        # ne laisse JAMAIS assets/ vide : c'est ça qui causait la page
        # "Contenu manquant" quand la session était créée puis le fichier
        # jamais/mal écrit avant un build. L'IA sait désormais d'avance que
        # ces 3 fichiers existent déjà à ces chemins exacts et n'a qu'à les
        # REMPLACER (write_file), jamais à les créer de zéro.
        if html_inline:
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
        else:
            if not stage_webroot_from_template("scratch", assets_dir, logger):
                assets_dir.mkdir(parents=True, exist_ok=True)
                (assets_dir / "index.html").write_text(_SCRATCH_SKELETON_HTML, encoding="utf-8")
                (assets_dir / "style.css").write_text(_SCRATCH_SKELETON_CSS, encoding="utf-8")
                (assets_dir / "app.js").write_text(_SCRATCH_SKELETON_JS, encoding="utf-8")
                logger.log("✅ Squelette scratch écrit (assets/index.html + style.css + app.js) — "
                            "l'IA doit les REMPLACER via write_file, pas en créer d'autres.")

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
    enforce_project_entrypoint(sid, kind_hint="", logger=logger)
    logger.log(f"✅ Session créée: {sid}")
    return sid


# =============================================================
# DÉCOMPILATION (mode Dev — conservé pour APK existants)
# =============================================================
def _resolve_string_res(decompiled, ref):
    """Résout '@string/xxx' en cherchant dans res/values*/strings.xml.
    Retourne la valeur littérale, ou None si non trouvée / si ce n'était
    déjà pas une référence @string/."""
    if not ref or not ref.startswith("@string/"):
        return ref
    name = ref.split("/", 1)[1]
    values_dirs = sorted((decompiled / "res").glob("values*")) if (decompiled / "res").exists() else []
    for vd in values_dirs:
        f = vd / "strings.xml"
        if not f.exists():
            continue
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(rf'<string name="{re.escape(name)}"[^>]*>(.*?)</string>', txt, re.S)
        if m:
            val = m.group(1).strip()
            val = re.sub(r'</?[a-zA-Z][^>]*>', '', val)  # retire <b>, <i>, etc.
            return val.replace("\\'", "'").replace("&amp;", "&")
    return None


def extract_apk_identity(decompiled, pkg_old):
    """Relit un APK décompilé (apktool) pour en extraire l'identité réelle
    (nom d'app, version, SDK, orientation, permissions, icône, contenu
    webview) afin de synchroniser tout de suite le panneau de droite
    (Identité / Contenu / Permissions / Assets / Manifest+) avec ce qui est
    VRAIMENT dans l'APK importé comme template — au lieu de laisser les
    valeurs par défaut du formulaire ('MonApp', 'com.example.myapp'...)."""
    info = {
        "appName": None, "packageName": pkg_old,
        "versionName": None, "versionCode": None,
        "minSdk": None, "targetSdk": None,
        "orientation": "unspecified",
        "permissions": [], "customPermissions": "",
        "iconRelPath": None,
        "contentMode": "url", "appUrl": "", "htmlContent": "",
        "fullscreen": False, "immersive": False, "lockTask": False,
    }

    manifest_path = decompiled / "AndroidManifest.xml"
    mc = ""
    if manifest_path.exists():
        try:
            mc = manifest_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            mc = ""

    # ── apktool.yml : version + SDKs (bien plus fiable que le manifest, où
    # aapt les retire souvent après compilation) ─────────────────────────
    yml_path = decompiled / "apktool.yml"
    if yml_path.exists():
        try:
            yml = yml_path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"versionCode:\s*'?([0-9]+)'?", yml)
            if m: info["versionCode"] = m.group(1)
            m = re.search(r"versionName:\s*'([^']*)'", yml) or re.search(r'versionName:\s*"([^"]*)"', yml)
            if m: info["versionName"] = m.group(1)
            m = re.search(r"minSdkVersion:\s*'?([0-9]+)'?", yml)
            if m: info["minSdk"] = m.group(1)
            m = re.search(r"targetSdkVersion:\s*'?([0-9]+)'?", yml)
            if m: info["targetSdk"] = m.group(1)
        except Exception:
            pass

    if mc:
        if not info["versionCode"]:
            m = re.search(r'android:versionCode="([^"]+)"', mc)
            if m: info["versionCode"] = m.group(1)
        if not info["versionName"]:
            m = re.search(r'android:versionName="([^"]+)"', mc)
            if m: info["versionName"] = m.group(1)
        if not info["minSdk"]:
            m = re.search(r'android:minSdkVersion="([^"]+)"', mc)
            if m: info["minSdk"] = m.group(1)
        if not info["targetSdk"]:
            m = re.search(r'android:targetSdkVersion="([^"]+)"', mc)
            if m: info["targetSdk"] = m.group(1)

        # ── Nom de l'app : android:label sur <application>, résolu si
        # c'est une référence @string/... ────────────────────────────
        m = re.search(r'<application\b[^>]*android:label="([^"]+)"', mc)
        if m:
            label = m.group(1)
            resolved = _resolve_string_res(decompiled, label) if label.startswith("@string/") else label
            info["appName"] = resolved or label

        # ── Orientation : première activité avec un LAUNCHER intent-filter,
        # sinon la première activité tout court ────────────────────────
        launcher_block = re.search(
            r'<activity\b[^>]*>(?:(?!</activity>).)*?android\.intent\.category\.LAUNCHER.*?</activity>', mc, re.S)
        block = launcher_block.group(0) if launcher_block else mc
        m = re.search(r'android:screenOrientation="([^"]+)"', block)
        if m: info["orientation"] = m.group(1)

        # ── Permissions ─────────────────────────────────────────────────
        info["permissions"] = sorted(set(re.findall(r'<uses-permission[^>]*android:name="([^"]+)"', mc)))

        # ── Flags d'affichage (best-effort, dépend de FLAG_FULLSCREEN /
        # thème NoActionBar déjà injectés côté smali/manifest) ─────────
        if "FLAG_FULLSCREEN" in mc or 'android:theme="@android:style/Theme.NoTitleBar.Fullscreen"' in mc:
            info["fullscreen"] = True

    if not info["appName"]:
        info["appName"] = pkg_old.rsplit(".", 1)[-1].capitalize() if pkg_old else "MonApp"

    # ── Icône : cherche ic_launcher dans le mipmap/drawable de plus haute
    # densité disponible, pour prévisualisation immédiate côté client ──
    res_dir = decompiled / "res"
    if res_dir.exists():
        density_order = ["xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "anydpi-v26", ""]
        icon_names = ["ic_launcher.png", "ic_launcher_round.png", "icon.png"]
        found = None
        for density in density_order:
            for prefix in ("mipmap", "drawable"):
                folder = res_dir / (f"{prefix}-{density}" if density else prefix)
                if not folder.exists():
                    continue
                for name in icon_names:
                    cand = folder / name
                    if cand.exists():
                        found = cand
                        break
                if found:
                    break
            if found:
                break
        if found:
            info["iconRelPath"] = str(found.relative_to(decompiled)).replace("\\", "/")

    # ── Contenu WebView : si assets/index.html existe déjà dans le
    # template, on bascule le panneau Contenu en mode HTML et on
    # préremplit avec le vrai contenu trouvé ───────────────────────────
    index_candidates = [decompiled / "assets" / "index.html", decompiled / "assets" / "www" / "index.html"]
    found_html = False
    for idx in index_candidates:
        if idx.exists():
            try:
                info["contentMode"] = "html"
                info["htmlContent"] = idx.read_text(encoding="utf-8", errors="ignore")[:200_000]
                found_html = True
            except Exception:
                pass
            break
    if not found_html:
        # Pas de HTML embarqué : cherche une URL codée en dur (loadUrl) dans
        # le smali, sinon laisse le mode URL vide (l'utilisateur la renseigne).
        try:
            for smali_dir in decompiled.glob("smali*"):
                for f in smali_dir.rglob("*.smali"):
                    try:
                        txt = f.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    m = re.search(r'const-string[^"]*"(https?://[^"]+)"', txt)
                    if m:
                        info["appUrl"] = m.group(1)
                        break
                if info["appUrl"]:
                    break
        except Exception:
            pass

    return info


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

    # Support App Bundle (.aab) : converti automatiquement en APK universel
    # via bundletool avant apktool, qui ne sait lire que des APK.
    try:
        with zipfile.ZipFile(template_path) as zf:
            names = zf.namelist()
            if any(n == "BundleConfig.pb" or n.startswith("base/") for n in names):
                logger.log("ℹ App Bundle (.aab) détecté — conversion en APK universel...")
                universal = convert_aab_to_universal_apk(template_path, sd, logger)
                template_path = universal
    except zipfile.BadZipFile:
        pass  # laissé à apktool, qui donnera un message d'échec cohérent plus bas

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

    logger.log("🔎 Extraction de l'identité réelle de l'APK (nom, version, permissions, icône, contenu)...")
    try:
        identity = extract_apk_identity(decompiled, pkg_old)
    except Exception as e:
        logger.log(f"⚠ Extraction identité partielle/échouée : {e}")
        identity = {"appName": pkg_old.rsplit(".", 1)[-1].capitalize() if pkg_old else "MonApp"}

    meta = {
        "created": time.time(),
        "package": pkg_old,
        "packageOld": pkg_old,
        "origin": "decompile",
        "appName": identity.get("appName"),
        "identity": identity,
    }
    (sd / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    logger.log(f"✅ Session prête: {sid} (package: {pkg_old}, app: {identity.get('appName')})")
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
    # Plein écran : décoché par défaut dans le builder → l'app reste dans
    # les limites normales de l'écran (barres système visibles).
    fullscreen   = bool(config.get("fullscreen", False))
    # Immersif : cache la barre de navigation (Retour/Accueil/Récents).
    immersive    = bool(config.get("immersive", False))
    # Screen pinning : verrouille l'app à l'écran (startLockTask()).
    lock_task    = bool(config.get("lockTask", False))

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
                    _smali_main(package_new, wv_arg, wv_mode, orientation, permissions=permissions, fullscreen=fullscreen, immersive=immersive, lock_task=lock_task),
                    encoding="utf-8"
                )
                (new_smali_pkg / "InternalWebViewClient.smali").write_text(
                    _smali_webviewclient(package_new), encoding="utf-8"
                )
                (new_smali_pkg / "InternalWebChromeClient.smali").write_text(
                    _smali_webchromeclient(package_new), encoding="utf-8"
                )
                (new_smali_pkg / "InternalInsetsListener.smali").write_text(
                    _smali_insetslistener(package_new), encoding="utf-8"
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
                    _smali_main(package_new or old_pkg, wv_arg, wv_mode, orientation, permissions=permissions, fullscreen=fullscreen, immersive=immersive, lock_task=lock_task),
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
                # Idem pour InternalInsetsListener (fix edge-to-edge Android 15)
                (smali_pkg / "InternalInsetsListener.smali").write_text(
                    _smali_insetslistener(package_new or old_pkg), encoding="utf-8"
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

    elif origin in ("cordova", "flutter", "reactnative"):
        # Session hybride — patch config.xml / pubspec.yaml+main.dart / App.js,
        # au lieu du pipeline apktool (qui ne s'applique qu'aux APK décompilés).
        proj_root = resolve_session_root(sid)
        hybrid_kwargs = dict(
            app_name=app_name, package_new=package_new, version_code=version_code,
            version_name=version_name, min_sdk=min_sdk, orientation=orientation,
            mode=mode, app_url=app_url, html_inline=html_inline,
            site_zip_bytes=site_zip_bytes, icon_bytes=icon_bytes, splash_bytes=splash_bytes,
            permissions=permissions, logger=logger,
        )
        if origin == "cordova":
            _apply_config_cordova(proj_root, **hybrid_kwargs)
        elif origin == "flutter":
            _apply_config_flutter(proj_root, **hybrid_kwargs)
        else:
            _apply_config_reactnative(proj_root, **hybrid_kwargs)
        meta["package"] = package_new or meta.get("package", "")
        meta["appName"] = app_name
        meta_f.write_text(json.dumps(meta), encoding="utf-8")
        return

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


def _hybrid_extract_sitezip(www_dir, site_zip_bytes, tmp_zip_path, logger):
    """Extraction commune Cordova/Flutter/RN : dézippe dans www_dir, remonte
    d'un niveau si tout est dans un unique sous-dossier (mêmes conventions
    que generate_*_project)."""
    if www_dir.exists():
        shutil.rmtree(www_dir)
    www_dir.mkdir(parents=True)
    tmp_zip_path.write_bytes(site_zip_bytes)
    with zipfile.ZipFile(tmp_zip_path) as zf:
        _safe_extract_zip(zf, www_dir, logger)
    children = list(www_dir.iterdir())
    if len(children) == 1 and children[0].is_dir() and not (www_dir / "index.html").exists():
        inner = children[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(www_dir / item.name))
        inner.rmdir()
    tmp_zip_path.unlink(missing_ok=True)
    if not (www_dir / "index.html").exists():
        raise RuntimeError("Pas d'index.html trouvé à la racine du zip fourni (ni dans un unique sous-dossier).")


def _apply_config_cordova(proj, app_name, package_new, version_code, version_name,
                           min_sdk, orientation, mode, app_url, html_inline,
                           site_zip_bytes, icon_bytes, splash_bytes, permissions, logger):
    """Patch d'un projet Cordova existant (config.xml + www/) — pas de
    régénération via `cordova create`, juste une réécriture des fichiers
    déclaratifs ; `cordova build` relit config.xml à chaque compilation."""
    config_xml_path = proj / "config.xml"
    if not config_xml_path.exists():
        raise RuntimeError("config.xml introuvable dans le projet Cordova — projet corrompu ou incomplet.")

    existing = config_xml_path.read_text(encoding="utf-8")
    m = re.search(r'widget[^>]*\bid="([^"]*)"', existing)
    old_package = m.group(1) if m else "com.example.cordova"
    package = package_new or old_package

    icon_lines = ""
    if icon_bytes:
        icon_lines = "\n".join(
            f'        <icon density="{d}" src="res/icon/android/icon-{d}.png" />'
            for d in CORDOVA_ICON_DENSITIES
        )
        _cordova_write_icons(proj, icon_bytes, logger)
    else:
        # Conserve les balises <icon> existantes si aucune nouvelle icône n'est fournie
        found = re.findall(r'^\s*<icon[^>]*/>\s*$', existing, flags=re.MULTILINE)
        icon_lines = "\n".join(found)

    splash_lines = ""
    if splash_bytes:
        splash_lines = "\n".join(
            f'        <splash density="{d}" src="res/screen/android/screen-{d}.png" />'
            for d in CORDOVA_SPLASH_DENSITIES
        )
        _cordova_write_splash(proj, splash_bytes, logger)
    else:
        # Conserve les balises <splash> existantes si aucun nouveau splash n'est fourni
        found = re.findall(r'^\s*<splash[^>]*/>\s*$', existing, flags=re.MULTILINE)
        splash_lines = "\n".join(found)

    perm_lines = "\n".join(
        f'        <uses-permission android:name="{p}" />'
        for p in permissions if p != "android.permission.INTERNET"  # déjà implicite (access origin="*")
    )
    platform_extra = "\n".join(x for x in (icon_lines, splash_lines, perm_lines) if x)

    config_xml = _cordova_config_xml(package, app_name, version_name, min_sdk, orientation, platform_extra)
    config_xml_path.write_text(config_xml, encoding="utf-8")
    logger.log("✅ config.xml patché (identité + permissions)")

    www_dir = proj / "www"
    if mode == "html" and html_inline:
        www_dir.mkdir(parents=True, exist_ok=True)
        (www_dir / "index.html").write_text(html_inline, encoding="utf-8")
        logger.log("✅ www/index.html mis à jour")
    elif mode == "sitezip" and site_zip_bytes:
        _hybrid_extract_sitezip(www_dir, site_zip_bytes, proj / "site_upload.zip", logger)
        logger.log("✅ Site extrait dans www/")
    elif mode == "url" and app_url:
        # Cordova charge www/index.html localement ; en mode URL distante on
        # remplace ce point d'entrée par une redirection vers l'URL.
        www_dir.mkdir(parents=True, exist_ok=True)
        (www_dir / "index.html").write_text(
            f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<script>location.replace({json.dumps(app_url)});</script></head><body></body></html>',
            encoding="utf-8"
        )
        logger.log("✅ www/index.html mis à jour (redirection vers l'URL)")


def _apply_config_flutter(proj, app_name, package_new, version_code, version_name,
                           min_sdk, orientation, mode, app_url, html_inline,
                           site_zip_bytes, icon_bytes, splash_bytes, permissions, logger):
    """Patch d'un projet Flutter existant (pubspec.yaml + lib/main.dart +
    AndroidManifest.xml) — renommer le package Dart/Gradle après coup étant
    fragile (`flutter pub get` + refactor de dossiers), on ne touche
    l'applicationId/package que s'il change réellement."""
    pubspec_path = proj / "pubspec.yaml"
    if not pubspec_path.exists():
        raise RuntimeError("pubspec.yaml introuvable dans le projet Flutter — projet corrompu ou incomplet.")

    pubspec = pubspec_path.read_text(encoding="utf-8")
    pubspec = re.sub(r'^version:\s*.*$', f"version: {version_name}+{version_code}",
                      pubspec, count=1, flags=re.MULTILINE)

    www_dir = proj / "assets" / "www"
    if mode == "sitezip" and site_zip_bytes:
        _hybrid_extract_sitezip(www_dir, site_zip_bytes, proj / "site_upload.zip", logger)
        asset_dirs = _flutter_collect_asset_dirs(www_dir)
        assets_yaml = "".join(f"    - {d}\n" for d in asset_dirs)
        if re.search(r'^\s*assets:\s*$', pubspec, flags=re.MULTILINE):
            pubspec = re.sub(r'(^\s*assets:\s*\n)(?:^\s*-.*\n)*', r'\1' + assets_yaml, pubspec, count=1, flags=re.MULTILINE)
        elif "uses-material-design: true" in pubspec:
            pubspec = pubspec.replace("uses-material-design: true",
                                       "uses-material-design: true\n\n  assets:\n" + assets_yaml, 1)
        else:
            pubspec += "\nflutter:\n  assets:\n" + assets_yaml
        logger.log(f"✅ Site extrait dans assets/www/ ({len(asset_dirs)} dossier(s) déclaré(s) dans pubspec.yaml)")
    elif mode == "html" and html_inline:
        www_dir.mkdir(parents=True, exist_ok=True)
        (www_dir / "index.html").write_text(html_inline, encoding="utf-8")
        if not re.search(r'assets/www/', pubspec):
            if "uses-material-design: true" in pubspec:
                pubspec = pubspec.replace("uses-material-design: true",
                                           "uses-material-design: true\n\n  assets:\n    - assets/www/\n", 1)
            else:
                pubspec += "\nflutter:\n  assets:\n    - assets/www/\n"
        logger.log("✅ assets/www/index.html mis à jour")

    pubspec_path.write_text(pubspec, encoding="utf-8")
    logger.log("✅ pubspec.yaml patché (version" + (" + assets" if mode in ("sitezip", "html") else "") + ")")

    main_dart_path = proj / "lib" / "main.dart"
    if main_dart_path.exists():
        main_dart = main_dart_path.read_text(encoding="utf-8")
        if mode in ("sitezip", "html"):
            new_call = "..loadFlutterAsset('assets/www/index.html')"
        else:
            new_call = f"..loadRequest(Uri.parse({json.dumps(app_url or 'https://example.com')}))"
        main_dart = re.sub(
            r'\.\.(loadFlutterAsset|loadRequest)\([^;]*\)',
            new_call, main_dart, count=1
        )
        main_dart = re.sub(r'title:\s*[^,]*,', f'title: {json.dumps(app_name)},', main_dart, count=1)
        main_dart_path.write_text(main_dart, encoding="utf-8")
        logger.log("✅ lib/main.dart patché (source + titre)")

    manifest_path = proj / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest_path.exists():
        manifest = manifest_path.read_text(encoding="utf-8")
        manifest = re.sub(r'android:label="[^"]*"', f'android:label="{app_name}"', manifest, count=1)
        for perm in permissions:
            if perm not in manifest:
                manifest = manifest.replace(
                    "<application", f'<uses-permission android:name="{perm}" />\n    <application', 1
                )
        manifest_path.write_text(manifest, encoding="utf-8")
        logger.log("✅ AndroidManifest.xml patché (label + permissions)")

    build_gradle_path = proj / "android" / "app" / "build.gradle"
    if not build_gradle_path.exists():
        build_gradle_path = proj / "android" / "app" / "build.gradle.kts"
    if build_gradle_path.exists() and package_new:
        content = build_gradle_path.read_text(encoding="utf-8")
        content = re.sub(r'applicationId\s*=?\s*"[^"]*"', f'applicationId "{package_new}"', content, count=1)
        build_gradle_path.write_text(content, encoding="utf-8")
        logger.log("⚠ applicationId mis à jour dans build.gradle — le renommage complet des dossiers "
                    "Kotlin/Java (com/example/...) n'est PAS effectué automatiquement pour un projet Flutter "
                    "existant ; édite MainActivity.kt manuellement si le build échoue après changement de package.")

    if icon_bytes:
        res_dir = proj / "android" / "app" / "src" / "main" / "res"
        written = 0
        for density_dir, size in FLUTTER_ICON_DENSITIES.items():
            target_dir = res_dir / density_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            png = make_icon_png(icon_bytes, size)
            (target_dir / "ic_launcher.png").write_bytes(png)
            written += 1
        logger.log(f"✅ Icônes mises à jour ({written} densités)")

    if splash_bytes:
        _flutter_write_splash(proj, splash_bytes, logger)


def _apply_config_reactnative(proj, app_name, package_new, version_code, version_name,
                               min_sdk, orientation, mode, app_url, html_inline,
                               site_zip_bytes, icon_bytes, splash_bytes, permissions, logger):
    """Patch d'un projet React Native existant (App.tsx/App.js + strings.xml
    + build.gradle) — comme pour Flutter, le renommage du package Java après
    `react-native init` n'est pas automatisé (dossiers com/example/... à
    déplacer manuellement)."""
    entry = proj / "App.tsx"
    if not entry.exists():
        entry = proj / "App.js"
    if not entry.exists():
        raise RuntimeError("App.tsx/App.js introuvable dans le projet React Native — projet corrompu ou incomplet.")

    assets_www = proj / "android" / "app" / "src" / "main" / "assets" / "www"
    if mode == "sitezip" and site_zip_bytes:
        _hybrid_extract_sitezip(assets_www, site_zip_bytes, proj / "site_upload.zip", logger)
        logger.log("✅ Site extrait dans android/app/src/main/assets/www/")
    elif mode == "html" and html_inline:
        assets_www.mkdir(parents=True, exist_ok=True)
        (assets_www / "index.html").write_text(html_inline, encoding="utf-8")
        logger.log("✅ assets/www/index.html mis à jour")

    if mode in ("sitezip", "html"):
        webview_source = "{{ uri: 'file:///android_asset/www/index.html' }}"
    else:
        webview_source = f"{{{{ uri: {json.dumps(app_url or 'https://example.com')} }}}}"

    app_source = entry.read_text(encoding="utf-8")
    app_source = re.sub(r"source=\{\{[^}]*\}\}", f"source={webview_source}", app_source, count=1)
    entry.write_text(app_source, encoding="utf-8")
    logger.log(f"✅ {entry.name} patché (source WebView)")

    strings_path = proj / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
    if strings_path.exists():
        content = strings_path.read_text(encoding="utf-8")
        content = re.sub(r'(<string name="app_name">)[^<]*(</string>)', rf'\1{app_name}\2', content, count=1)
        strings_path.write_text(content, encoding="utf-8")
        logger.log("✅ strings.xml patché (app_name)")

    build_gradle_path = proj / "android" / "app" / "build.gradle"
    if build_gradle_path.exists():
        content = build_gradle_path.read_text(encoding="utf-8")
        content = re.sub(r'versionCode\s+\d+', f'versionCode {version_code}', content, count=1)
        content = re.sub(r'versionName\s*"[^"]*"', f'versionName "{version_name}"', content, count=1)
        if package_new:
            content = re.sub(r'applicationId\s+"[^"]*"', f'applicationId "{package_new}"', content, count=1)
            logger.log("⚠ applicationId mis à jour dans build.gradle — le renommage complet des dossiers "
                        "Java (com/example/...) n'est PAS effectué automatiquement ; édite MainActivity.java "
                        "manuellement si le build échoue après changement de package.")
        build_gradle_path.write_text(content, encoding="utf-8")
        logger.log("✅ build.gradle patché (version)")

    manifest_path = proj / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest_path.exists():
        manifest = manifest_path.read_text(encoding="utf-8")
        changed = False
        for perm in permissions:
            if perm not in manifest:
                manifest = manifest.replace(
                    "<application", f'<uses-permission android:name="{perm}" />\n    <application', 1
                )
                changed = True
        if changed:
            manifest_path.write_text(manifest, encoding="utf-8")
            logger.log("✅ AndroidManifest.xml patché (permissions)")

    if icon_bytes:
        anydpi_dir = proj / "android" / "app" / "src" / "main" / "res" / "mipmap-anydpi-v26"
        if anydpi_dir.exists():
            shutil.rmtree(anydpi_dir)
        res_dir = proj / "android" / "app" / "src" / "main" / "res"
        written = 0
        for density_dir, size in RN_ICON_DENSITIES.items():
            target_dir = res_dir / density_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            png = make_icon_png(icon_bytes, size)
            (target_dir / "ic_launcher.png").write_bytes(png)
            (target_dir / "ic_launcher_round.png").write_bytes(png)
            written += 1
        logger.log(f"✅ Icônes mises à jour ({written} densités, icône adaptative désactivée)")

    if splash_bytes:
        _rn_write_splash(proj, splash_bytes, logger)


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
def _detect_min_sdk_from_gradle(android_dir, default):
    """Relit minSdkVersion/minSdk depuis app/build.gradle d'un projet
    natif déjà généré (Cordova/RN) ou le module android/ d'un projet
    Flutter — sert à retrouver la valeur au moment du recompile, où le
    bouton générique 'Compiler' de l'explorer ne renvoie que
    {signing, outName} (pas le config complet passé à la génération)."""
    try:
        gradle_path = Path(android_dir) / "app" / "build.gradle"
        if not gradle_path.exists():
            gradle_path = Path(android_dir) / "build.gradle"
        content = gradle_path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'minSdk(?:Version)?\s*[= ]?\s*(\d+)', content)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return default


def _recompile_hybrid_session(sid, origin, signing, out_name, logger):
    """Recompile une session Cordova/Flutter/React Native déjà générée
    (et potentiellement éditée à la main dans l'explorer) en réutilisant
    EXACTEMENT le même pipeline que /build-cordova, /build-flutter,
    /build-rn appelés avec {"session": sid} (run_gradle_build ou
    run_flutter_build + sign_native_apk) — jamais apktool, qui ne
    comprend rien à ces arborescences de projet natif/Gradle/Dart.
    Appelé depuis recompile_session() dès que session.json indique une
    origine hybride, pour que le bouton générique 'Compiler' fonctionne
    aussi sur ces sessions (avant, il tentait apktool sur un dossier
    'source'/'decompiled' qui n'existe pas pour ces origines et échouait)."""
    logger.log(f"♻ Session hybride détectée (origin={origin}) — pipeline dédié (pas apktool).")
    _assert_entrypoint_ready(sid, origin, logger)

    if origin == "cordova":
        proj = cordova_project_dir(sid)
        if not proj.exists():
            raise RuntimeError(f"Session {sid} : dossier cordova_project introuvable — régénère le projet Cordova.")
        android_dir = proj / "platforms" / "android"
        build_type = "assembleDebug" if (signing or {}).get("mode") == "debug" else "assembleRelease"
        unsigned_apk = run_gradle_build(android_dir, logger, build_type)
        min_sdk = _detect_min_sdk_from_gradle(android_dir, CORDOVA_MIN_SDK_DEFAULT)
        return sign_native_apk(unsigned_apk, signing, android_dir, out_name, min_sdk, logger)

    if origin == "flutter":
        proj = flutter_project_dir(sid)
        if not proj.exists():
            raise RuntimeError(f"Session {sid} : dossier flutter_project introuvable — régénère le projet Flutter.")
        build_type = "debug" if (signing or {}).get("mode") == "debug" else "release"
        unsigned_apk = run_flutter_build(proj, logger, build_type)
        min_sdk = _detect_min_sdk_from_gradle(proj / "android", FLUTTER_MIN_SDK_DEFAULT)
        return sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)

    if origin == "reactnative":
        proj = react_native_project_dir(sid)
        if not proj.exists():
            raise RuntimeError(f"Session {sid} : dossier rn_project introuvable — régénère le projet React Native.")
        android_dir = proj / "android"
        unsigned_apk = run_gradle_build(android_dir, logger, "assembleRelease")
        min_sdk = _detect_min_sdk_from_gradle(android_dir, NATIVE_MIN_SDK_DEFAULT)
        return sign_native_apk(unsigned_apk, signing, android_dir, out_name, min_sdk, logger)

    raise RuntimeError(f"Origine hybride inconnue : {origin}")


def recompile_session(sid, signing, out_name, logger):
    sd = session_dir(sid)

    # BUG — le bouton 'Compiler' (générique) tentait apktool même sur les
    # sessions Cordova/Flutter/React Native, dont le dossier "source"/
    # "decompiled" n'existe jamais (elles vivent dans cordova_project/
    # flutter_project/rn_project) → échec systématique. On lit l'origine
    # dans session.json et on délègue au pipeline natif correspondant.
    meta_path = sd / "session.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            origin = meta.get("origin", "")
            if origin in ("cordova", "flutter", "reactnative"):
                return _recompile_hybrid_session(sid, origin, signing, out_name, logger)
        except Exception as e:
            logger.log(f"⚠ Lecture session.json échouée ({e}) — tentative de compilation apktool par défaut.")

    # Détermine le dossier source
    sdir_v3 = source_dir(sid)
    sdir_v2 = sd / "decompiled"
    src = sdir_v3 if sdir_v3.exists() else sdir_v2
    _assert_entrypoint_ready(sid, "", logger)

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
    """Fallback de dernier recours (utilisé seulement si AUCUN keytool n'a
    été trouvé — voir _candidates()/find_tool ci-dessus, désormais capable
    de détecter tools/jdk/bin/keytool.exe). Sur les machines clientes sans
    connexion internet pour ce Python embarqué, 'pip install' échouera de
    toute façon : on tente donc plusieurs approches et on remonte le VRAI
    message d'erreur de pip (au lieu de l'exception générique) pour que le
    diagnostic soit exploitable."""
    try:
        import cryptography; return True
    except ImportError:
        pass

    logger.log("📦 'cryptography' absent — tentative d'installation (fallback, keytool du JDK non trouvé)...")
    attempts = [
        [sys.executable, "-m", "pip", "install", "cryptography"],
        [sys.executable, "-m", "pip", "install", "--user", "cryptography"],
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "cryptography"],
    ]
    last_stderr = ""
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                logger.log("✅ cryptography installée")
                return True
            last_stderr = (r.stderr or r.stdout or "").strip()[-800:]
        except Exception as e:
            last_stderr = str(e)

    logger.log(f"❌ Impossible d'installer cryptography (pip) : {last_stderr}")
    logger.log("💡 Cause probable : pas de JDK détecté (donc pas de keytool) ET pas "
               "d'accès internet pip sur cette machine. Solution recommandée : installe "
               "le composant 'jdk' depuis l'onglet Composants — le keytool du JDK "
               "permet de générer le keystore SANS dépendre de pip/internet.")
    return False

def _find_custom_keystore():
    """Retourne le nom du 1er fichier .keystore/.jks trouvé dans TOOLS_DIR
    (hors debug.keystore) — corrige le bug où /check ne détectait QUE le
    nom par défaut 'mon.keystore', donc perdait le bouton "Régénérer" dès
    que le client avait choisi un autre nom de fichier keystore."""
    try:
        for f in TOOLS_DIR.glob("*.keystore"):
            if f.name != "debug.keystore":
                return f.name
        for f in TOOLS_DIR.glob("*.jks"):
            return f.name
    except Exception:
        pass
    return None


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
# PIPELINE NATIF (Gradle / Kotlin) — génération d'apps compilées
# from scratch, à côté du pipeline WebView/smali existant ci-dessous.
# N'affecte AUCUNE fonction existante — module 100% additif.
# =============================================================
NATIVE_MIN_SDK_DEFAULT     = 24
NATIVE_TARGET_SDK_DEFAULT  = 34
NATIVE_COMPILE_SDK_DEFAULT = 34
NATIVE_BUILD_TOOLS_VERSION = "34.0.0"
NATIVE_AGP_VERSION         = "8.3.2"
NATIVE_KOTLIN_VERSION      = "1.9.23"

def native_project_dir(sid):
    return WORK_DIR / sid / "native_project"

def stage_native_skeleton(kotlin_dir: Path, res_dir: Path, package: str, logger=None):
    """
    Équivalent de stage_webroot_from_template mais pour le mode natif
    (Kotlin/Java) : copie templates/native/kotlin/MainActivity.kt.tmpl
    (avec substitution __PACKAGE__ → le vrai package) + templates/native/res/
    (layout/activity_main.xml, values/themes.xml) dans le projet Gradle
    généré. Retourne True si la copie a eu lieu, False si le dossier-modèle
    est introuvable (l'appelant garde alors son ancien fallback codé en dur).
    """
    src = TEMPLATES_DIR / "native"
    kt_tmpl = src / "kotlin" / "MainActivity.kt.tmpl"
    if not kt_tmpl.is_file():
        if logger:
            logger.log(f"⚠ Dossier-modèle 'native' introuvable ({src}) — "
                        f"fallback sur le squelette généré en mémoire.")
        return False

    kotlin_dir.mkdir(parents=True, exist_ok=True)
    activity_kt = kt_tmpl.read_text(encoding="utf-8").replace("__PACKAGE__", package)
    (kotlin_dir / "MainActivity.kt").write_text(activity_kt, encoding="utf-8")

    layout_src = src / "res" / "layout" / "activity_main.xml"
    if layout_src.is_file():
        (res_dir / "layout").mkdir(parents=True, exist_ok=True)
        shutil.copy(layout_src, res_dir / "layout" / "activity_main.xml")

    themes_src = src / "res" / "values" / "themes.xml"
    if themes_src.is_file():
        (res_dir / "values").mkdir(parents=True, exist_ok=True)
        shutil.copy(themes_src, res_dir / "values" / "themes.xml")

    if logger:
        logger.log("✅ Espace de travail 'native' initialisé depuis templates/native/ "
                    "(MainActivity.kt + activity_main.xml + themes.xml déjà prêts)")
    return True


def generate_native_project(sid, config, logger):
    """
    Génère un projet Gradle Android minimal et réellement compilable
    (Kotlin natif — pas de WebView). Template 'empty_activity' : une seule
    Activity + un layout. Base extensible : d'autres templates pourront
    s'ajouter plus tard sans changer la structure de cette fonction.
    """
    app_name     = (config.get("appName") or "MyNativeApp").strip()
    package      = normalize_package_name(config.get("packageName"), fallback="com.example.nativeapp")
    version_code = str(config.get("versionCode") or "1")
    version_name = str(config.get("versionName") or "1.0")
    min_sdk      = str(config.get("minSdk") or NATIVE_MIN_SDK_DEFAULT)
    target_sdk   = str(config.get("targetSdk") or NATIVE_TARGET_SDK_DEFAULT)

    proj = native_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    app_dir = proj / "app"
    pkg_path = package.replace(".", "/")
    kotlin_dir = app_dir / "src" / "main" / "kotlin" / pkg_path
    res_dir = app_dir / "src" / "main" / "res"
    kotlin_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "layout").mkdir(parents=True, exist_ok=True)
    (res_dir / "values").mkdir(parents=True, exist_ok=True)

    logger.log(f"📁 Génération du projet natif : {package} ({app_name})")

    (proj / "settings.gradle").write_text(
        'pluginManagement {\n'
        '    repositories {\n'
        '        google()\n'
        '        mavenCentral()\n'
        '        gradlePluginPortal()\n'
        '    }\n'
        '}\n'
        'dependencyResolutionManagement {\n'
        '    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)\n'
        '    repositories {\n'
        '        google()\n'
        '        mavenCentral()\n'
        '    }\n'
        '}\n'
        f'rootProject.name = "{app_name}"\n'
        'include(":app")\n',
        encoding="utf-8"
    )

    (proj / "build.gradle").write_text(
        'plugins {\n'
        f'    id("com.android.application") version "{NATIVE_AGP_VERSION}" apply false\n'
        f'    id("org.jetbrains.kotlin.android") version "{NATIVE_KOTLIN_VERSION}" apply false\n'
        '}\n',
        encoding="utf-8"
    )

    (proj / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx2048m\n"
        "android.useAndroidX=true\n"
        "kotlin.code.style=official\n",
        encoding="utf-8"
    )

    # local.properties : chemin du SDK, forward-slashes (compatible Windows,
    # évite les soucis d'échappement de backslash dans un fichier .properties)
    sdk_path_str = str(SDK_DIR.resolve()).replace("\\", "/")
    (proj / "local.properties").write_text(f"sdk.dir={sdk_path_str}\n", encoding="utf-8")

    (app_dir / "build.gradle").write_text(
        'plugins {\n'
        '    id("com.android.application")\n'
        '    id("org.jetbrains.kotlin.android")\n'
        '}\n\n'
        'android {\n'
        f'    namespace "{package}"\n'
        f'    compileSdk {NATIVE_COMPILE_SDK_DEFAULT}\n'
        f'    buildToolsVersion "{NATIVE_BUILD_TOOLS_VERSION}"\n\n'
        '    defaultConfig {\n'
        f'        applicationId "{package}"\n'
        f'        minSdk {min_sdk}\n'
        f'        targetSdk {target_sdk}\n'
        f'        versionCode {version_code}\n'
        f'        versionName "{version_name}"\n'
        '    }\n\n'
        '    buildTypes {\n'
        '        release {\n'
        '            minifyEnabled false\n'
        '        }\n'
        '    }\n\n'
        '    compileOptions {\n'
        '        sourceCompatibility JavaVersion.VERSION_17\n'
        '        targetCompatibility JavaVersion.VERSION_17\n'
        '    }\n'
        '    kotlinOptions {\n'
        '        jvmTarget = "17"\n'
        '    }\n'
        '}\n\n'
        'dependencies {\n'
        '    implementation "androidx.core:core-ktx:1.13.1"\n'
        '    implementation "androidx.appcompat:appcompat:1.7.0"\n'
        '    implementation "com.google.android.material:material:1.12.0"\n'
        '}\n',
        encoding="utf-8"
    )

    (app_dir / "src" / "main" / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application\n'
        '        android:label="@string/app_name"\n'
        '        android:icon="@mipmap/ic_launcher"\n'
        '        android:allowBackup="true"\n'
        '        android:theme="@style/Theme.Native">\n'
        '        <activity\n'
        '            android:name=".MainActivity"\n'
        '            android:exported="true">\n'
        '            <intent-filter>\n'
        '                <action android:name="android.intent.action.MAIN" />\n'
        '                <category android:name="android.intent.category.LAUNCHER" />\n'
        '            </intent-filter>\n'
        '        </activity>\n'
        '    </application>\n'
        '</manifest>\n',
        encoding="utf-8"
    )

    if not stage_native_skeleton(kotlin_dir, res_dir, package, logger):
        (kotlin_dir / "MainActivity.kt").write_text(
            f'package {package}\n\n'
            'import android.os.Bundle\n'
            'import androidx.appcompat.app.AppCompatActivity\n\n'
            'class MainActivity : AppCompatActivity() {\n'
            '    override fun onCreate(savedInstanceState: Bundle?) {\n'
            '        super.onCreate(savedInstanceState)\n'
            '        setContentView(R.layout.activity_main)\n'
            '    }\n'
            '}\n',
            encoding="utf-8"
        )

        (res_dir / "layout" / "activity_main.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    android:layout_width="match_parent"\n'
            '    android:layout_height="match_parent"\n'
            '    android:orientation="vertical"\n'
            '    android:gravity="center">\n'
            '    <TextView\n'
            '        android:layout_width="wrap_content"\n'
            '        android:layout_height="wrap_content"\n'
            '        android:textSize="20sp"\n'
            '        android:text="@string/app_name" />\n'
            '</LinearLayout>\n',
            encoding="utf-8"
        )

        (res_dir / "values" / "themes.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<resources>\n'
            '    <style name="Theme.Native" parent="Theme.MaterialComponents.DayNight.NoActionBar" />\n'
            '</resources>\n',
            encoding="utf-8"
        )

    (res_dir / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources>\n'
        f'    <string name="app_name">{xml_escape_text(app_name)}</string>\n'
        '</resources>\n',
        encoding="utf-8"
    )

    # BUG CORRIGÉ : un second write_text(themes.xml) inconditionnel se
    # trouvait ici, juste après le bloc `if not stage_native_skeleton(...)`
    # qui vient déjà d'écrire (ou de laisser en place, selon le cas) le bon
    # themes.xml. Ce doublon écrasait donc TOUJOURS le fichier — y compris
    # celui copié depuis templates/native/res/values/themes.xml — avec un
    # thème codé en dur identique par coïncidence aujourd'hui, mais qui
    # aurait silencieusement annulé toute personnalisation future de ce
    # template (couleurs, style parent...) sans aucune erreur ni log pour
    # le signaler. Supprimé : le bloc if/else ci-dessus gère déjà themes.xml
    # correctement dans les deux cas (template présent ou fallback).

    # Icônes : PNG classiques (pas d'adaptive-icon XML pour rester simple),
    # réutilise make_icon_png déjà utilisé par le pipeline WebView.
    icon_bytes = config.get("_iconBytes")
    for density_dir, size in (("mipmap-mdpi", 48), ("mipmap-hdpi", 72), ("mipmap-xhdpi", 96),
                               ("mipmap-xxhdpi", 144), ("mipmap-xxxhdpi", 192)):
        d = res_dir / density_dir
        d.mkdir(parents=True, exist_ok=True)
        try:
            png = make_icon_png(icon_bytes, size) if icon_bytes else _make_solid_png(size, size)
        except Exception:
            png = _make_solid_png(size, size)
        (d / "ic_launcher.png").write_bytes(png)

    logger.log("✅ Structure du projet natif générée.")
    return proj


def _native_gradle_env(logger):
    """
    Construit l'environnement (PATH/JAVA_HOME) pour lancer Gradle avec le
    JDK téléchargé par setup.js si présent, sinon le Java système détecté
    par find_tool (même logique que le reste du serveur).
    """
    env = os.environ.copy()
    jdk_dir = TOOLS_DIR / "jdk"
    java_home = None
    if (jdk_dir / "bin" / "java.exe").exists():
        java_home = jdk_dir
    else:
        java_path = find_tool("java")
        if java_path:
            # remonte de tools/jdk-x/bin/java.exe → tools/jdk-x
            java_home = Path(java_path).parent.parent
    if java_home:
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
        logger.log(f"☕ JAVA_HOME = {java_home}")
    else:
        logger.log("⚠ JDK introuvable — le build Gradle risque d'échouer. Installe le composant 'jdk'.")
    return env


def find_gradle():
    """Gradle téléchargé par setup.js en priorité, sinon Gradle système (PATH)."""
    bundled = TOOLS_DIR / "gradle" / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
    if bundled.exists():
        return str(bundled)
    return shutil.which("gradle")


def find_jadx():
    """jadx téléchargé par setup.js (tools/jadx/bin/jadx.bat) en priorité,
    sinon jadx système (PATH)."""
    bundled = TOOLS_DIR / "jadx" / "bin" / ("jadx.bat" if os.name == "nt" else "jadx")
    if bundled.exists():
        return str(bundled)
    return shutil.which("jadx")


def find_bundletool():
    """bundletool.jar téléchargé par setup.js — pas d'exécutable natif,
    toujours lancé via `java -jar`."""
    bundled = TOOLS_DIR / "bundletool" / "bundletool.jar"
    if bundled.exists():
        return str(bundled)
    return None


def run_gradle_build(project_dir, logger, build_type="assembleRelease"):
    """
    Lance Gradle sur le projet généré et retourne le Path de l'APK produit
    (non signé — la signature est faite séparément via sign_native_apk,
    pour rester cohérent avec le pipeline WebView existant).
    """
    gradle = find_gradle()
    if not gradle:
        raise RuntimeError(
            "Gradle introuvable. Installe le composant 'gradle' depuis "
            "l'écran des composants avant de lancer un build natif."
        )

    # Pré-vérification android.jar (platforms;android-34) : sans ce fichier
    # Gradle échoue toujours, mais avec un message générique et peu clair
    # ("SDK location not found" ou une erreur de résolution de plugin très
    # loin en aval). On le détecte ici pour donner un message actionnable
    # tout de suite plutôt que de laisser l'utilisateur lire 200 lignes de
    # stacktrace Gradle.
    android_jar = SDK_DIR / "platforms" / f"android-{NATIVE_COMPILE_SDK_DEFAULT}" / "android.jar"
    if not android_jar.exists():
        raise RuntimeError(
            f"platforms;android-{NATIVE_COMPILE_SDK_DEFAULT} absent du SDK "
            f"({android_jar} introuvable). Installe/réinstalle le composant "
            "'Android SDK (platform-tools + build-tools + platforms)' depuis "
            "l'écran des composants — le build natif ne peut pas compiler sans "
            "android.jar."
        )

    env = _native_gradle_env(logger)
    logger.log(f"🔨 Compilation Gradle ({build_type})... (peut prendre plusieurs minutes la 1ère fois — téléchargement des dépendances Android)")
    cmd = [gradle, build_type, "--no-daemon", "--console=plain", "-p", str(project_dir)]
    logger.log(f"$ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=str(project_dir), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout Gradle (30 min dépassées) — build annulé.")
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    for line in (r.stderr or "").strip().split("\n"):
        if line.strip(): logger.log("⚠ " + line)
    if r.returncode != 0:
        raise RuntimeError("Échec de la compilation Gradle — voir les logs ci-dessus pour la cause exacte.")

    variant = "release" if "Release" in build_type else "debug"
    apk_dir = project_dir / "app" / "build" / "outputs" / "apk" / variant
    candidates = list(apk_dir.glob("*.apk")) if apk_dir.exists() else []
    if not candidates:
        raise RuntimeError(f"Build Gradle terminé mais aucun APK trouvé dans {apk_dir}")
    logger.log(f"✅ APK compilé : {candidates[0].name}")
    return candidates[0]


def sign_native_apk(unsigned_apk, signing, workdir, out_name, min_sdk, logger):
    """
    Aligne + signe un APK produit par Gradle. Copie volontairement le
    même comportement que recompile_session (signature debug/custom,
    repli jarsigner, vérification finale) sans toucher à cette dernière —
    évite tout risque de régression sur le pipeline WebView existant.
    """
    workdir = Path(workdir)
    aligned_apk = workdir / "aligned_native.apk"
    zipalign = find_tool("zipalign")
    if zipalign:
        logger.log("🔧 Zipalign...")
        aligned_apk.unlink(missing_ok=True)
        ok_zip = run_cmd([zipalign, "-f", "-v", "4", str(unsigned_apk), str(aligned_apk)], logger)
        if not ok_zip or not aligned_apk.exists() or aligned_apk.stat().st_size == 0:
            logger.log("⚠ Zipalign a échoué — utilisation APK non aligné")
            aligned_apk.unlink(missing_ok=True)
            aligned_apk = Path(unsigned_apk)
    else:
        logger.log("⚠ zipalign non trouvé, étape sautée")
        aligned_apk = Path(unsigned_apk)

    final_apk = OUTPUT_DIR / out_name
    signing_mode = (signing or {}).get("mode", "debug")

    if signing_mode == "nosign":
        shutil.copy(aligned_apk, final_apk)
        logger.log(f"✅ APK natif compilé (non signé) : {final_apk.name}")
        return final_apk

    keystore = None
    ks_pass  = "android"
    key_pass = "android"
    alias    = "androiddebugkey"
    ks_is_pkcs12 = False

    if signing_mode == "custom" and signing.get("keystoreB64"):
        keystore = workdir / "custom_native.keystore"
        keystore.write_bytes(base64.b64decode(signing["keystoreB64"]))
        alias    = signing.get("alias")    or alias
        ks_pass  = signing.get("storePass") or ks_pass
        key_pass = signing.get("keyPass")   or ks_pass
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

    if not keystore or not keystore.exists():
        raise RuntimeError(f"Keystore introuvable : {keystore}")

    apksigner = find_tool("apksigner")
    signed_ok = False

    if apksigner and keystore.exists():
        logger.log("✍ Signature avec apksigner...")
        ks_pass_arg,  ks_pass_file  = _pass_arg(ks_pass,  workdir, "ks_native.pass.tmp")
        key_pass_arg, key_pass_file = _pass_arg(key_pass, workdir, "key_native.pass.tmp")
        min_sdk_int = int(min_sdk) if str(min_sdk).isdigit() else 23
        enable_v4 = min_sdk_int >= 30
        cmd = [apksigner, "sign",
               "--ks", str(keystore),
               "--ks-pass", ks_pass_arg, "--key-pass", key_pass_arg,
               "--min-sdk-version", str(min_sdk_int),
               "--v1-signing-enabled", "true",
               "--v2-signing-enabled", "true",
               "--v3-signing-enabled", "true",
               *(["--ks-type", "PKCS12"] if ks_is_pkcs12 else []),
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
        if jarsigner and keystore.exists():
            logger.log("✍ Tentative jarsigner...")
            shutil.copy(aligned_apk, final_apk)
            signed_ok = run_cmd([jarsigner, "-verbose", "-sigalg", "SHA1withRSA",
                                  "-digestalg", "SHA1", "-keystore", str(keystore),
                                  "-storepass", ks_pass, "-keypass", key_pass,
                                  str(final_apk), alias], logger)

    if not signed_ok:
        raise RuntimeError("Aucun signeur trouvé (apksigner/jarsigner).")

    try:
        with zipfile.ZipFile(final_apk) as zf:
            bad = zf.testzip()
            if bad: raise RuntimeError(f"Entrée corrompue: {bad}")
    except zipfile.BadZipFile:
        final_apk.unlink(missing_ok=True)
        raise RuntimeError("APK final corrompu. Relance.")

    if signing_mode == "custom" and keystore.name == "custom_native.keystore":
        try: keystore.unlink()
        except: pass

    logger.log(f"✅ APK natif prêt : {final_apk.name}")
    return final_apk


def do_build_native(config, icon_bytes):
    """
    Orchestration complète du build natif : génération projet → Gradle →
    signature. Utilise son propre token OPS['native'] pour ne jamais
    interférer avec OPS['legacy'] (pipeline WebView) — les deux peuvent
    même tourner en parallèle sans se marcher dessus.
    """
    global CURRENT_SESSION
    logger = OPS["native"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : CURRENT_SESSION n'était mis à jour que par les
    # endpoints /*-generate (session éditable sans build). Les pipelines de
    # build direct (natif/TWA/cordova/reactnative/flutter/nativescript/maui/
    # titanium) créaient une session ici mais ne la propageaient jamais à
    # CURRENT_SESSION : /tree, /apply, /recompile continuaient donc de
    # pointer vers l'ancienne session (souvent scratch) au lieu du projet
    # qu'on vient réellement de construire.
    CURRENT_SESSION = sid
    try:
        if icon_bytes:
            config["_iconBytes"] = icon_bytes
        proj = generate_native_project(sid, config, logger)
        # CORRECTIF : sans ceci, un projet natif n'apparaissait jamais dans
        # /sessions (aucun session.json) — impossible à retrouver dans le
        # panneau "SESSIONS", à rouvrir plus tard via select_session, ou à
        # associer à l'onglet "Natif" de l'explorateur comme les autres
        # types (cordova/flutter/reactnative) le font déjà via
        # write_hybrid_session_meta. Combiné à l'ajout de "native_project"
        # dans PROJECT_ROOT_SUBDIRS ci-dessus, le projet natif redevient
        # listable ET parcourable comme n'importe quel autre type.
        write_hybrid_session_meta(sid, "native", config)
        build_type = "assembleDebug" if (config.get("signing", {}).get("mode") == "debug") else "assembleRelease"
        unsigned_apk = run_gradle_build(proj, logger, build_type)

        app_name     = (config.get("appName") or "MyNativeApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_native.apk"
        min_sdk = config.get("minSdk") or NATIVE_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})

        final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# =============================================================
# PIPELINE TWA (Trusted Web Activity) — "site → app" via bubblewrap
# ---------------------------------------------------------------
# 3e famille de méthode : ni scratch WebView (smali), ni natif Kotlin
# vide — ici on enveloppe un site web EXISTANT dans un vrai projet
# Android généré par l'outil officiel Google (bubblewrap), qui produit
# un vrai APK signable, avec Chrome Custom Tabs en fallback. Réutilise
# le même sign_native_apk que le pipeline natif (--skipSigning côté
# bubblewrap : on gère nous-mêmes zipalign+apksigner, ce qui évite les
# soucis connus de mots de passe interactifs de bubblewrap build).
# =============================================================
TWA_MIN_SDK_DEFAULT = 21

def find_bubblewrap():
    """bubblewrap installé en npm-global par setup.js dans tools/nodejs/."""
    bundled = TOOLS_DIR / "nodejs" / ("bubblewrap.cmd" if os.name == "nt" else "bubblewrap")
    if bundled.exists():
        return str(bundled)
    return shutil.which("bubblewrap")


def twa_project_dir(sid):
    return WORK_DIR / sid / "twa_project"


def _twa_env(logger):
    """Même logique que _native_gradle_env : JDK téléchargé en priorité,
    + ANDROID_HOME/ANDROID_SDK_ROOT pointés vers notre SDK déjà installé
    (platform-tools/build-tools/platforms — cf. composant androidSdk)."""
    env = _native_gradle_env(logger)
    env["ANDROID_HOME"] = str(SDK_DIR)
    env["ANDROID_SDK_ROOT"] = str(SDK_DIR)
    return env


def ensure_bubblewrap_config(logger):
    """
    bubblewrap stocke le chemin JDK/SDK dans ~/.bubblewrap/config.json et,
    s'il est absent, lance un assistant interactif (bloquant en subprocess
    non-TTY → timeout garanti). On l'écrit nous-mêmes via `updateConfig`
    pour pointer vers le JDK et le SDK déjà téléchargés par setup.js,
    donc jamais de première-exécution interactive.
    """
    bubblewrap = find_bubblewrap()
    if not bubblewrap:
        raise RuntimeError(
            "bubblewrap introuvable. Installe le composant 'bubblewrap' depuis "
            "l'écran des composants avant de générer un TWA."
        )
    jdk_dir = TOOLS_DIR / "jdk"
    if not (jdk_dir / "bin" / "java.exe").exists():
        raise RuntimeError("JDK introuvable — installe le composant 'jdk' avant de générer un TWA.")
    if not SDK_DIR.exists():
        raise RuntimeError("Android SDK introuvable — installe le composant 'androidSdk' avant de générer un TWA.")

    env = _twa_env(logger)
    logger.log("⚙ Configuration bubblewrap (JDK + Android SDK)...")
    cmd = [bubblewrap, "updateConfig",
           f"--jdkPath={jdk_dir.resolve()}",
           f"--androidSdkPath={SDK_DIR.resolve()}"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ bubblewrap updateConfig a retourné une erreur (souvent sans gravité, on continue) : " + (r.stderr or "")[:300])


def generate_twa_manifest(sid, config, logger):
    """
    Écrit un twa-manifest.json valide directement (schéma documenté par
    bubblewrap), donc pas besoin de passer par `bubblewrap init` qui est
    interactif et qui télécharge le manifest web. L'utilisateur donne
    juste l'URL du site + les métadonnées ; c'est ensuite `bubblewrap
    update` (régénère le projet Android depuis ce json) puis `build`.
    """
    url = (config.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        raise RuntimeError("URL du site invalide ou manquante (doit commencer par http:// ou https://).")
    parsed = urlparse(url)
    host = parsed.netloc
    if not host:
        raise RuntimeError("Impossible d'extraire le domaine depuis l'URL fournie.")

    package = normalize_package_name(config.get("packageName"), fallback="com.example.twa")
    app_name = (config.get("appName") or "MonApp").strip()
    theme_color = config.get("themeColor") or "#2196F3"
    bg_color = config.get("backgroundColor") or "#FFFFFF"
    start_url = config.get("startUrl") or (parsed.path or "/")
    icon_url = (config.get("iconUrl") or "").strip()
    if not icon_url:
        raise RuntimeError(
            "iconUrl requis : bubblewrap télécharge l'icône depuis une URL publique "
            "(icône hébergée sur ton site, 512x512 px minimum) — pas d'upload local possible pour ce mode."
        )

    proj = twa_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True, exist_ok=True)

    manifest = {
        "packageId": package,
        "host": host,
        "name": app_name,
        "launcherName": app_name[:30],
        "display": "standalone",
        "themeColor": theme_color,
        "navigationColor": theme_color,
        "navigationColorDark": "#000000",
        "backgroundColor": bg_color,
        "backgroundColorDark": "#000000",
        "enableNotifications": bool(config.get("enableNotifications")),
        "startUrl": start_url,
        "iconUrl": icon_url,
        "maskableIconUrl": config.get("maskableIconUrl") or "",
        "monochromeIconUrl": "",
        "splashScreenFadeOutDuration": 300,
        "signingKey": {"path": "", "alias": ""},  # non utilisé : --skipSigning
        "appVersionName": str(config.get("versionName") or "1.0"),
        "appVersionCode": int(str(config.get("versionCode") or "1")),
        "shortcuts": [],
        "generatorApp": "APKFactoryPro",
        "webManifestUrl": config.get("webManifestUrl") or "",
        "fallbackType": "customtabs",
        "features": {
            "locationDelegation": {"enabled": bool(config.get("enableLocation"))},
            "playBilling": {"enabled": False},
        },
        "alphaDependencies": {"enabled": False},
        "enableSiteSettingsShortcut": True,
        "isChromeOSOnly": False,
        "isMetaQuest": False,
        "fullScopeUrl": f"{parsed.scheme}://{host}/",
        "minSdkVersion": int(config.get("minSdk") or TWA_MIN_SDK_DEFAULT),
        "orientation": config.get("orientation") or "default",
        "fingerprints": [],
        "additionalTrustedOrigins": [],
    }
    (proj / "twa-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.log(f"✅ twa-manifest.json généré ({host} → {package})")
    return proj


def run_bubblewrap_build(proj, logger):
    """
    `bubblewrap update` régénère le projet Android natif depuis le json
    (télécharge l'icône, écrit AndroidManifest/gradle...), puis `build
    --skipSigning` compile un APK non signé qu'on signe nous-mêmes.
    """
    bubblewrap = find_bubblewrap()
    env = _twa_env(logger)
    manifest_path = proj / "twa-manifest.json"

    logger.log("🌐 Génération du projet Android depuis le manifest TWA (bubblewrap update)...")
    cmd_update = [bubblewrap, "update", f"--manifest={manifest_path}"]
    r1 = subprocess.run(cmd_update, cwd=str(proj), env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=300)
    for line in (r1.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    for line in (r1.stderr or "").strip().split("\n"):
        if line.strip(): logger.log("⚠ " + line)
    if r1.returncode != 0:
        raise RuntimeError("Échec de la génération du projet TWA (bubblewrap update) — voir logs ci-dessus.")

    logger.log("🔨 Compilation du projet TWA (bubblewrap build --skipSigning)... (peut prendre plusieurs minutes)")
    cmd_build = [bubblewrap, "build", f"--manifest={manifest_path}", "--skipSigning", "--skipPwaValidation"]
    try:
        r2 = subprocess.run(cmd_build, cwd=str(proj), env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout bubblewrap build (30 min dépassées) — build annulé.")
    for line in (r2.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    for line in (r2.stderr or "").strip().split("\n"):
        if line.strip(): logger.log("⚠ " + line)
    if r2.returncode != 0:
        raise RuntimeError("Échec de la compilation TWA (bubblewrap build) — voir logs ci-dessus pour la cause exacte.")

    # Noms observés selon versions de bubblewrap : app-release-unsigned.apk
    # le plus courant avec --skipSigning. On retombe sur un glob si le nom
    # a changé entre versions, en excluant tout ce qui serait déjà signé.
    priority = ["app-release-unsigned.apk", "app-release-unsigned-aligned.apk"]
    for name in priority:
        p = proj / name
        if p.exists():
            logger.log(f"✅ APK TWA compilé : {p.name}")
            return p
    candidates = [p for p in proj.glob("*.apk") if "signed" not in p.name.lower()]
    if not candidates:
        candidates = list(proj.glob("*.apk"))
    if not candidates:
        raise RuntimeError(f"Build bubblewrap terminé mais aucun APK trouvé dans {proj}")
    logger.log(f"✅ APK TWA compilé : {candidates[0].name}")
    return candidates[0]


def do_build_twa(config, icon_bytes):
    """
    Orchestration complète TWA : token OPS['twa'] dédié (indépendant de
    'legacy'/'native'/'jadx', peut tourner en parallèle sans conflit).
    icon_bytes n'est pas utilisé ici (bubblewrap exige une iconUrl
    publique, pas un upload) — gardé au même endroit d'appel que
    do_build_native pour une intégration symétrique côté /build-twa.
    """
    global CURRENT_SESSION
    logger = OPS["twa"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    CURRENT_SESSION = sid
    try:
        ensure_bubblewrap_config(logger)
        proj = generate_twa_manifest(sid, config, logger)
        unsigned_apk = run_bubblewrap_build(proj, logger)

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_twa.apk"
        min_sdk = config.get("minSdk") or TWA_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})

        final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# =============================================================
# PIPELINE CORDOVA — "site → app" via un vrai projet Cordova/Android
# ---------------------------------------------------------------
# 4e famille de méthode : contrairement à TWA (Custom Tabs + digital asset
# links), ici on embarque le site dans une vraie WebView Cordova (offline
# possible, accès aux plugins natifs). Le projet est créé/patché de façon
# 100% non-interactive (pas de `cordova build`, qui peut lancer des
# invites) puis compilé directement avec notre run_gradle_build existant :
# cordova-android génère un projet Gradle standard sous platforms/android,
# donc on réutilise tel quel le même run_gradle_build + sign_native_apk
# que le pipeline natif, sans dupliquer la logique de compilation/signature.
# =============================================================
CORDOVA_MIN_SDK_DEFAULT = 24

CORDOVA_ICON_DENSITIES = {
    "ldpi": 36, "mdpi": 48, "hdpi": 72,
    "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192,
}

# Densités splash natives cordova-android (res/screen/android/screen-{density}.png),
# tailles officielles port/paysage — cf. doc cordova-android "Splash Screen".
CORDOVA_SPLASH_DENSITIES = {
    "port-mdpi": (320, 480), "port-hdpi": (480, 800), "port-xhdpi": (720, 1280),
    "port-xxhdpi": (960, 1600), "port-xxxhdpi": (1280, 1920),
    "land-mdpi": (480, 320), "land-hdpi": (800, 480), "land-xhdpi": (1280, 720),
    "land-xxhdpi": (1600, 960), "land-xxxhdpi": (1920, 1280),
}


def find_cordova():
    """cordova installé en npm-global par setup.js dans tools/nodejs/."""
    bundled = TOOLS_DIR / "nodejs" / ("cordova.cmd" if os.name == "nt" else "cordova")
    if bundled.exists():
        return str(bundled)
    return shutil.which("cordova")


def cordova_project_dir(sid):
    return WORK_DIR / sid / "cordova_project"


def _cordova_config_xml(package, app_name, version_name, min_sdk, orientation, icon_lines):
    """Contenu de config.xml partagé par les 3 modes (url/scratch/template-écrasé)."""
    return f'''<?xml version='1.0' encoding='utf-8'?>
<widget id="{package}" version="{version_name}" xmlns="http://www.w3.org/ns/widgets" xmlns:android="http://schemas.android.com/apk/res/android">
    <name>{app_name}</name>
    <description>{app_name}</description>
    <content src="index.html" />
    <access origin="*" />
    <allow-navigation href="*" />
    <allow-intent href="http://*/*" />
    <allow-intent href="https://*/*" />
    <allow-intent href="tel:*" />
    <allow-intent href="sms:*" />
    <allow-intent href="mailto:*" />
    <preference name="Orientation" value="{orientation}" />
    <preference name="AndroidXEnabled" value="true" />
    <preference name="AndroidPersistentFileLocation" value="Internal" />
    <platform name="android">
        <preference name="android-minSdkVersion" value="{min_sdk}" />
        <preference name="AndroidLaunchMode" value="singleTask" />
{icon_lines}
    </platform>
</widget>
'''


def _cordova_write_icons(proj, icon_bytes, logger):
    icon_dir = proj / "res" / "icon" / "android"
    icon_dir.mkdir(parents=True, exist_ok=True)
    for density, size in CORDOVA_ICON_DENSITIES.items():
        png = make_icon_png(icon_bytes, size)
        (icon_dir / f"icon-{density}.png").write_bytes(png)
    logger.log(f"✅ Icônes générées ({len(CORDOVA_ICON_DENSITIES)} densités)")


def _cordova_write_splash(proj, splash_bytes, logger):
    """Écrit les 10 densités port/land attendues par cordova-android sous
    res/screen/android/screen-{density}.png — même mécanique que
    _cordova_write_icons, jusque là seule l'icône était branchée."""
    screen_dir = proj / "res" / "screen" / "android"
    screen_dir.mkdir(parents=True, exist_ok=True)
    for density, (w, h) in CORDOVA_SPLASH_DENSITIES.items():
        png = make_splash_png(splash_bytes, w, h)
        (screen_dir / f"screen-{density}.png").write_bytes(png)
    logger.log(f"✅ Splash généré ({len(CORDOVA_SPLASH_DENSITIES)} densités)")


def _cordova_scaffold(proj, package, app_name, logger):
    """`cordova create` + `platform add android` — squelette vierge partagé
    par les modes url et scratch (seul le contenu de www/ diffère ensuite)."""
    cordova = find_cordova()
    if not cordova:
        raise RuntimeError(
            "Cordova introuvable. Installe le composant 'cordova' depuis "
            "l'écran des composants avant de générer une app Cordova."
        )
    env = _twa_env(logger)  # même besoin: JDK + ANDROID_HOME (cordova-android compile via Gradle)

    logger.log("📦 Création du projet Cordova (cordova create)...")
    cmd_create = [cordova, "create", str(proj), package, app_name, "--no-telemetry"]
    r = subprocess.run(cmd_create, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=180)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `cordova create` — voir logs ci-dessus.")

    logger.log("🤖 Ajout de la plateforme android (cordova platform add android)...")
    cmd_platform = [cordova, "platform", "add", "android", "--no-telemetry"]
    r = subprocess.run(cmd_platform, cwd=str(proj), env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `cordova platform add android` — voir logs ci-dessus.")
    return cordova, env


def generate_cordova_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger):
    """
    Mode "url" (Site → App) ou "scratch" (site local zippé, embarqué
    hors-ligne) — les deux partagent le même squelette Cordova, seule la
    provenance du contenu de www/ change. Le mode "template" (import d'un
    projet Cordova existant) est géré par generate_cordova_project_from_template.
    """
    source_mode = (config.get("sourceMode") or "url").strip()

    package = normalize_package_name(config.get("packageName"), fallback="com.example.cordova")
    app_name = (config.get("appName") or "MonApp").strip()
    version_name = str(config.get("versionName") or "1.0")
    min_sdk = int(config.get("minSdk") or CORDOVA_MIN_SDK_DEFAULT)
    orientation = config.get("orientation") or "default"

    url = ""
    if source_mode == "scratch":
        pass  # site_zip_bytes optionnel : si absent, fallback sur le dossier-modele cordova (templates/cordova/webroot/)
    else:
        url = (config.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            raise RuntimeError("URL du site invalide ou manquante (doit commencer par http:// ou https://).")

    proj = cordova_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.parent.mkdir(parents=True, exist_ok=True)

    _cordova_scaffold(proj, package, app_name, logger)

    icon_lines = "\n".join(
        f'        <icon density="{d}" src="res/icon/android/icon-{d}.png" />'
        for d in CORDOVA_ICON_DENSITIES
    )
    splash_lines = "\n".join(
        f'        <splash density="{d}" src="res/screen/android/screen-{d}.png" />'
        for d in CORDOVA_SPLASH_DENSITIES
    ) if splash_bytes else ""
    platform_lines = "\n".join(x for x in (icon_lines, splash_lines) if x)
    config_xml = _cordova_config_xml(package, app_name, version_name, min_sdk, orientation, platform_lines)
    (proj / "config.xml").write_text(config_xml, encoding="utf-8")
    logger.log(f"✅ config.xml généré ({package}, minSdk {min_sdk})")

    www_dir = proj / "www"

    if source_mode == "scratch" and not site_zip_bytes:
        # Aucun zip fourni : l'IA compte écrire les fichiers ensuite via
        # write_file — on ne laisse JAMAIS www/ vide, on part du
        # dossier-modèle Cordova déjà arrangé (templates/cordova/webroot/).
        if not stage_webroot_from_template("cordova", www_dir, logger):
            www_dir.mkdir(parents=True, exist_ok=True)
            (www_dir / "index.html").write_text(_SCRATCH_SKELETON_HTML, encoding="utf-8")
            (www_dir / "style.css").write_text(_SCRATCH_SKELETON_CSS, encoding="utf-8")
            (www_dir / "app.js").write_text(_SCRATCH_SKELETON_JS, encoding="utf-8")
            logger.log("✅ Squelette Cordova écrit (www/index.html + style.css + app.js).")
    elif source_mode == "scratch":
        # ── Site local zippé : on écrase le www/ par défaut avec le
        # contenu fourni (même logique que le zip "site complet" du mode
        # Scratch classique — extraction sécurisée + remontée d'un niveau
        # si tout est dans un seul sous-dossier) ─────────────────────────
        logger.log("🗂 Extraction du site local (zip)...")
        shutil.rmtree(www_dir, ignore_errors=True)
        www_dir.mkdir(parents=True, exist_ok=True)
        zpath = proj / "site_upload.zip"
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
        if (www_dir / "index.html").exists():
            logger.log("✅ Site local extrait dans www/ (app 100% hors-ligne)")
        else:
            raise RuntimeError("Pas d'index.html trouvé à la racine du zip fourni (ni dans un unique sous-dossier).")
    else:
        # ── URL distante : redirige vers le site dès que le pont Cordova
        # est prêt (deviceready) ────────────────────────────────────────
        index_html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta http-equiv="Content-Security-Policy" content="default-src * gap: data: blob: 'unsafe-inline' 'unsafe-eval';">
    <title>{app_name}</title>
    <style>html,body{{height:100%;margin:0;background:#fff}}</style>
</head>
<body>
    <script src="cordova.js"></script>
    <script>
        document.addEventListener('deviceready', function () {{
            window.location.href = {json.dumps(url)};
        }}, false);
        // Filet de sécurité si deviceready ne se déclenche pas (rare, mais
        // observé sur certains émulateurs) : on force la navigation après
        // un court délai plutôt que de laisser l'app bloquée sur un écran
        // blanc indéfiniment.
        setTimeout(function () {{
            if (window.location.href.indexOf('index.html') !== -1) {{
                window.location.href = {json.dumps(url)};
            }}
        }}, 3000);
    </script>
</body>
</html>
'''
        (www_dir / "index.html").write_text(index_html, encoding="utf-8")
        logger.log("✅ www/index.html généré (redirection vers le site au deviceready)")

    _cordova_write_icons(proj, icon_bytes, logger)
    if splash_bytes:
        _cordova_write_splash(proj, splash_bytes, logger)
    write_hybrid_session_meta(sid, "cordova", config)
    enforce_project_entrypoint(sid, kind_hint="cordova", logger=logger)
    return proj


def _find_cordova_project_root(extract_dir):
    """
    Cherche config.xml à la racine du zip importé, ou un niveau plus bas
    si tout est dans un unique sous-dossier (cas fréquent d'un zip
    exporté depuis un explorateur de fichiers qui garde le dossier
    parent) — même logique de remontée que pour un zip de site.
    """
    if (extract_dir / "config.xml").exists():
        return extract_dir
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "config.xml").exists():
        return children[0]
    return None


def generate_cordova_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger):
    """
    Mode "template" : importe un projet Cordova EXISTANT (zippé) au lieu
    d'en générer un vierge. Contrairement aux modes url/scratch, on ne
    change PAS le package (widget id) par défaut — un projet Cordova
    importé a potentiellement déjà des plugins/config liés à cet id, le
    changer silencieusement pourrait casser des choses qu'on ne voit pas
    depuis ce zip seul. On ne patch que ce que l'utilisateur a
    explicitement renseigné (nom, version, icône) et on relance
    `cordova prepare android` pour resynchroniser config.xml → le projet
    Android natif AVANT de compiler nous-mêmes avec Gradle (indispensable
    ici : contrairement aux modes url/scratch où config.xml existe avant
    même `platform add`, un projet importé peut avoir platforms/android
    déjà généré à partir d'un config.xml différent de celui qu'on patch).
    """
    if not project_zip_bytes:
        raise RuntimeError("Mode template : un zip du projet Cordova existant est requis.")

    proj = cordova_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.mkdir(parents=True, exist_ok=True)

    logger.log("🗂 Extraction du projet Cordova importé (zip)...")
    zpath = proj.parent / "cordova_template_upload.zip"
    zpath.write_bytes(project_zip_bytes)
    extract_tmp = proj.parent / "cordova_template_extract"
    shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        _safe_extract_zip(zf, extract_tmp, logger)
    zpath.unlink(missing_ok=True)

    root = _find_cordova_project_root(extract_tmp)
    if not root:
        shutil.rmtree(extract_tmp, ignore_errors=True)
        raise RuntimeError(
            "config.xml introuvable dans le zip importé (ni à la racine, ni dans un unique "
            "sous-dossier) — ce n'est pas un projet Cordova valide."
        )
    # Déplace le contenu du projet trouvé vers proj/ (peut être root lui-même ou un sous-dossier)
    for item in root.iterdir():
        shutil.move(str(item), str(proj / item.name))
    shutil.rmtree(extract_tmp, ignore_errors=True)
    logger.log(f"✅ Projet importé ({(proj / 'config.xml').stat().st_size} octets de config.xml)")

    cordova = find_cordova()
    if not cordova:
        raise RuntimeError(
            "Cordova introuvable. Installe le composant 'cordova' depuis "
            "l'écran des composants avant de compiler un projet Cordova importé."
        )
    env = _twa_env(logger)

    if not (proj / "platforms" / "android").exists():
        logger.log("🤖 Le projet importé n'a pas de plateforme android — ajout (cordova platform add android)...")
        r = subprocess.run([cordova, "platform", "add", "android", "--no-telemetry"],
                            cwd=str(proj), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300)
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        if r.returncode != 0:
            logger.log("⚠ " + (r.stderr or "")[:500])
            raise RuntimeError("Échec de `cordova platform add android` sur le projet importé — voir logs ci-dessus.")

    # ── Patch OPTIONNEL de config.xml : uniquement les champs que
    # l'utilisateur a explicitement renseignés (le reste du projet importé
    # — plugins, préférences custom, id — n'est jamais touché) ──────────
    config_xml_path = proj / "config.xml"
    xml_content = config_xml_path.read_text(encoding="utf-8")
    app_name_override = (config.get("appName") or "").strip()
    version_override = (config.get("versionName") or "").strip()
    if app_name_override:
        xml_content = re.sub(r'<name>[^<]*</name>', f'<name>{app_name_override}</name>', xml_content, count=1)
        logger.log(f"✅ Nom de l'app surchargé : {app_name_override}")
    if version_override:
        xml_content = re.sub(r'(<widget[^>]*\bversion=")[^"]*(")', rf'\g<1>{version_override}\g<2>', xml_content, count=1)
        logger.log(f"✅ Version surchargée : {version_override}")
    config_xml_path.write_text(xml_content, encoding="utf-8")

    if icon_bytes:
        _cordova_write_icons(proj, icon_bytes, logger)
    if splash_bytes:
        _cordova_write_splash(proj, splash_bytes, logger)

    # ── cordova prepare android : resynchronise config.xml (+ www/) vers
    # platforms/android — étape indispensable ici puisqu'on ne passe PAS
    # par `cordova build`, seulement par notre propre Gradle ensuite ────
    logger.log("🔄 Resynchronisation config.xml → projet natif (cordova prepare android)...")
    r = subprocess.run([cordova, "prepare", "android", "--no-telemetry"],
                        cwd=str(proj), env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `cordova prepare android` — voir logs ci-dessus.")

    write_hybrid_session_meta(sid, "cordova", config)
    enforce_project_entrypoint(sid, kind_hint="cordova", logger=logger)
    return proj


def do_generate_cordova_session(config, icon_bytes, splash_bytes=None, site_zip_bytes=None, project_zip_bytes=None):
    """
    Génère UNIQUEMENT le projet Cordova (pas de build Gradle) — permet à
    l'utilisateur de l'ouvrir dans l'explorer/éditeur (comme scratch/
    template) et de modifier les fichiers avant de compiler séparément
    via /build-cordova avec {"session": sid}. Token OPS['cordova_gen'].
    """
    global CURRENT_SESSION
    logger = OPS["cordova_gen"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    sid = new_session_id()
    logger.session = sid
    try:
        source_mode = (config.get("sourceMode") or "url").strip()
        if source_mode == "template":
            generate_cordova_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger)
        else:
            generate_cordova_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        # BUG-FIX : sans ceci, CURRENT_SESSION reste sur l'ancienne session
        # (scratch ou autre) et /tree, /apply, /recompile continuent
        # d'afficher/recompiler l'ancien projet au lieu du Cordova généré.
        CURRENT_SESSION = sid
        logger.log(f"✅ Projet Cordova prêt — session {sid} (modifiable dans l'explorer)")
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


def do_build_cordova(config, icon_bytes, splash_bytes=None, site_zip_bytes=None, project_zip_bytes=None):
    """
    Orchestration complète Cordova : token OPS['cordova'] dédié.
    Dispatch sur config['sourceMode'] : 'url' (défaut) / 'scratch' /
    'template'. cordova-android produit un projet Gradle standard sous
    platforms/android — on réutilise run_gradle_build et sign_native_apk
    tels quels (même logique que le pipeline natif), aucune duplication.
    Si config['session'] pointe vers une session déjà générée (via
    /cordova-generate, potentiellement modifiée par l'utilisateur dans
    l'explorer), on saute complètement la (re)génération et on compile
    directement le projet existant tel quel.
    """
    logger = OPS["cordova"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    existing_sid = (config.get("session") or "").strip()
    if existing_sid:
        sid = existing_sid
        logger.session = sid
        try:
            proj = cordova_project_dir(sid)
            if not proj.exists():
                raise RuntimeError(f"Session {sid} introuvable ou projet Cordova absent — régénère.")
            logger.log(f"♻ Compilation depuis la session existante {sid} (fichiers tels que modifiés)")
            _assert_entrypoint_ready(sid, "cordova", logger)
            android_dir = proj / "platforms" / "android"
            build_type = "assembleDebug" if (config.get("signing", {}).get("mode") == "debug") else "assembleRelease"
            unsigned_apk = run_gradle_build(android_dir, logger, build_type)
            app_name = (config.get("appName") or "MonApp").strip()
            version_name = str(config.get("versionName") or "1.0")
            out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_cordova.apk"
            min_sdk = config.get("minSdk") or CORDOVA_MIN_SDK_DEFAULT
            signing = config.get("signing", {"mode": "debug"})
            final_apk = sign_native_apk(unsigned_apk, signing, android_dir, out_name, min_sdk, logger)
            logger.result_file = str(final_apk)
            logger.status = "done"
        except Exception as e:
            import traceback
            logger.log(f"❌ Erreur: {e}")
            logger.log(traceback.format_exc())
            logger.status = "error"
        return
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    global CURRENT_SESSION
    CURRENT_SESSION = sid
    try:
        source_mode = (config.get("sourceMode") or "url").strip()
        if source_mode == "template":
            proj = generate_cordova_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger)
        else:
            proj = generate_cordova_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        _assert_entrypoint_ready(sid, "cordova", logger)
        android_dir = proj / "platforms" / "android"
        build_type = "assembleDebug" if (config.get("signing", {}).get("mode") == "debug") else "assembleRelease"
        unsigned_apk = run_gradle_build(android_dir, logger, build_type)

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_cordova.apk"
        min_sdk = config.get("minSdk") or CORDOVA_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})

        final_apk = sign_native_apk(unsigned_apk, signing, android_dir, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# =============================================================
# PIPELINES BETA — NativeScript / Xamarin.Forms via .NET MAUI / Titanium
# ---------------------------------------------------------------
# ⚠ CONTRAIREMENT À CORDOVA/FLUTTER/REACT NATIVE (rodés sur de vraies
# sessions client), ces 3 pipelines sont neufs et NON VALIDÉS en
# conditions réelles — aucun environnement de test avec les vrais CLI
# (ns/dotnet/titanium) n'était disponible au moment de l'écriture.
# Attends-toi à devoir ajuster ces fonctions après un premier essai réel,
# en donnant les logs d'erreur exacts. Chacune couvre uniquement le cas
# "wrapper WebView" (site distant ou site local zippé), pas une vraie app
# native multi-écrans dans ces frameworks.
# =============================================================

NATIVESCRIPT_MIN_SDK_DEFAULT = 24
MAUI_MIN_SDK_DEFAULT = 24
TITANIUM_MIN_SDK_DEFAULT = 24

_NS_ICON_DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def find_nativescript():
    bundled = TOOLS_DIR / "nodejs" / ("ns.cmd" if os.name == "nt" else "ns")
    if bundled.exists():
        return str(bundled)
    return shutil.which("ns") or shutil.which("tns")


def find_dotnet():
    bundled = TOOLS_DIR / "dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet")
    if bundled.exists():
        return str(bundled)
    return shutil.which("dotnet")


def find_titanium():
    bundled = TOOLS_DIR / "nodejs" / ("titanium.cmd" if os.name == "nt" else "titanium")
    if bundled.exists():
        return str(bundled)
    return shutil.which("titanium")


def nativescript_project_dir(sid):
    return WORK_DIR / sid / "nativescript_project"


def maui_project_dir(sid):
    return WORK_DIR / sid / "maui_project"


def titanium_project_dir(sid):
    return WORK_DIR / sid / "titanium_project"


def _write_webroot_from_config(webroot, config, site_zip_bytes, logger, index_fallback="<html><body>App</body></html>"):
    """Commun aux 3 pipelines : mode 'url' → rien à copier (WebView pointera
    directement vers l'URL) ; mode 'scratch' → dézippe le site fourni dans
    webroot, avec un index.html minimal de secours si le zip est vide."""
    webroot.mkdir(parents=True, exist_ok=True)
    source_mode = (config.get("sourceMode") or "url").strip()
    if source_mode == "scratch" and site_zip_bytes:
        with zipfile.ZipFile(io.BytesIO(site_zip_bytes)) as zf:
            zf.extractall(webroot)
        if not (webroot / "index.html").exists():
            (webroot / "index.html").write_text(index_fallback, encoding="utf-8")
        logger.log(f"✅ Site local extrait dans {webroot}")
    elif source_mode == "scratch":
        (webroot / "index.html").write_text(index_fallback, encoding="utf-8")
        logger.log("⚠ Mode scratch sans site_zip fourni — index.html minimal généré")


# ---- NativeScript --------------------------------------------------

def generate_nativescript_project(sid, config, icon_bytes, site_zip_bytes, logger):
    ns = find_nativescript()
    if not ns:
        raise RuntimeError(
            "NativeScript CLI ('ns') introuvable. Installe le composant "
            "'nativescript' depuis l'écran des composants avant de générer une app NativeScript."
        )
    env = _twa_env(logger)  # JDK + ANDROID_HOME, comme cordova/twa

    app_name = (config.get("appName") or "MonApp").strip()
    package = normalize_package_name(config.get("packageName"), fallback="com.example.nativescript")
    proj = nativescript_project_dir(sid)
    proj.parent.mkdir(parents=True, exist_ok=True)

    logger.log("📦 Création du projet NativeScript (ns create)...")
    cmd = [ns, "create", proj.name, "--template", "@nativescript/template-blank-ts",
           "--path", str(proj.parent), "--appid", package]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0 or not proj.exists():
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `ns create` — voir logs ci-dessus.")

    source_mode = (config.get("sourceMode") or "url").strip()
    url = (config.get("appUrl") or "").strip()
    webroot = proj / "app" / "assets" / "www"
    if source_mode == "scratch":
        _write_webroot_from_config(webroot, config, site_zip_bytes, logger)
        webview_src = "~/assets/www/index.html"
    else:
        webview_src = url or "https://example.com"

    # Page unique : une WebView plein écran (plugin officiel @nativescript/webview).
    (proj / "app" / "main-page.xml").write_text(
        f'<Page xmlns="http://schemas.nativescript.org/tns.xsd">\n'
        f'  <GridLayout>\n'
        f'    <WebView src="{webview_src}" />\n'
        f'  </GridLayout>\n'
        f'</Page>\n', encoding="utf-8")

    pkg_plugin = subprocess.run([ns, "plugin", "add", "@nativescript/webview"],
                                cwd=str(proj), env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180)
    for line in (pkg_plugin.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)

    if icon_bytes:
        res_dir = proj / "App_Resources" / "Android" / "src" / "main" / "res"
        for density, size in _NS_ICON_DENSITIES.items():
            d = res_dir / f"mipmap-{density}"
            d.mkdir(parents=True, exist_ok=True)
            png = make_icon_png(icon_bytes, size)
            (d / "ic_launcher.png").write_bytes(png)
            (d / "ic_launcher_round.png").write_bytes(png)
        logger.log("✅ Icônes NativeScript écrites")

    logger.log(f"✅ Projet NativeScript prêt : {proj}")
    return proj


def do_build_nativescript(config, icon_bytes, splash_bytes=None, site_zip_bytes=None):
    global CURRENT_SESSION
    logger = OPS["nativescript"]
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    CURRENT_SESSION = sid
    try:
        proj = generate_nativescript_project(sid, config, icon_bytes, site_zip_bytes, logger)
        env = _twa_env(logger)
        ns = find_nativescript()
        signing_mode = (config.get("signing") or {}).get("mode", "debug")
        build_flag = "--release" if signing_mode != "debug" else ""
        logger.log("🔨 Compilation NativeScript (ns build android)...")
        cmd = [ns, "build", "android"] + ([build_flag] if build_flag else []) + ["--no-hmr"]
        r = subprocess.run(cmd, cwd=str(proj), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=1800)
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        if r.returncode != 0:
            logger.log("⚠ " + (r.stderr or "")[:800])
            raise RuntimeError("Échec de `ns build android` — voir logs ci-dessus.")

        variant = "release" if build_flag else "debug"
        apk_dir = proj / "platforms" / "android" / "app" / "build" / "outputs" / "apk" / variant
        candidates = list(apk_dir.glob("*.apk")) if apk_dir.exists() else []
        if not candidates:
            raise RuntimeError(f"Build NativeScript terminé mais aucun APK trouvé dans {apk_dir}")
        unsigned_apk = candidates[0]

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_nativescript.apk"
        min_sdk = config.get("minSdk") or NATIVESCRIPT_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})
        final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# ---- Xamarin / .NET MAUI -------------------------------------------

def generate_maui_project(sid, config, icon_bytes, site_zip_bytes, logger):
    dotnet = find_dotnet()
    if not dotnet:
        raise RuntimeError(
            "dotnet introuvable. Installe le composant '.NET SDK + workload maui-android' "
            "depuis l'écran des composants avant de générer une app MAUI."
        )
    app_name = re.sub(r'[^A-Za-z0-9_]', '', (config.get("appName") or "MonApp").strip()) or "MonApp"
    proj = maui_project_dir(sid)
    proj.parent.mkdir(parents=True, exist_ok=True)

    logger.log("📦 Création du projet .NET MAUI (dotnet new maui)...")
    r = subprocess.run([dotnet, "new", "maui", "-n", app_name, "-o", str(proj)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0 or not proj.exists():
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `dotnet new maui` — voir logs ci-dessus (workload 'maui-android' installé ?).")

    source_mode = (config.get("sourceMode") or "url").strip()
    url = (config.get("appUrl") or "").strip()

    if source_mode == "scratch":
        webroot = proj / "Resources" / "Raw" / "www"
        _write_webroot_from_config(webroot, config, site_zip_bytes, logger)
        # Copie les fichiers du site dans le cache local au démarrage, puis
        # charge le index.html local — un WebView MAUI ne peut pas lire les
        # assets embarqués (Resources/Raw) par une simple URI relative, il
        # lui faut un vrai chemin fichier sur disque.
        mainpage_xaml = (
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<ContentPage xmlns="http://schemas.microsoft.com/dotnet/2021/maui"\n'
            '             xmlns:x="http://schemas.microsoft.com/winfx/2009/xaml"\n'
            '             x:Class="' + app_name + '.MainPage">\n'
            '    <WebView x:Name="MainWebView" />\n'
            '</ContentPage>\n')
        mainpage_cs = (
            "namespace " + app_name + ";\n\n"
            "public partial class MainPage : ContentPage\n"
            "{\n"
            "    public MainPage()\n"
            "    {\n"
            "        InitializeComponent();\n"
            "        LoadLocalSite();\n"
            "    }\n\n"
            "    async void LoadLocalSite()\n"
            "    {\n"
            "        var destDir = Path.Combine(FileSystem.CacheDirectory, \"www\");\n"
            "        Directory.CreateDirectory(destDir);\n"
            "        using var stream = await FileSystem.OpenAppPackageFileAsync(\"www/index.html\");\n"
            "        // Copie récursive minimale : seul index.html est garanti ici — les\n"
            "        // assets liés (css/js) doivent être référencés en relatif et seront\n"
            "        // eux aussi sous Resources/Raw/www/ embarqués via MauiAsset (voir .csproj).\n"
            "        using var outFile = File.Create(Path.Combine(destDir, \"index.html\"));\n"
            "        await stream.CopyToAsync(outFile);\n"
            "        MainWebView.Source = new UrlWebViewSource { Url = \"file://\" + Path.Combine(destDir, \"index.html\") };\n"
            "    }\n"
            "}\n")
        (proj / "MainPage.xaml").write_text(mainpage_xaml, encoding="utf-8")
        (proj / "MainPage.xaml.cs").write_text(mainpage_cs, encoding="utf-8")
        logger.log("✅ MainPage générée (WebView → site local copié en cache au démarrage)")
    else:
        mainpage_xaml = (
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<ContentPage xmlns="http://schemas.microsoft.com/dotnet/2021/maui"\n'
            '             xmlns:x="http://schemas.microsoft.com/winfx/2009/xaml"\n'
            '             x:Class="' + app_name + '.MainPage">\n'
            f'    <WebView Source="{url or "https://example.com"}" />\n'
            '</ContentPage>\n')
        (proj / "MainPage.xaml").write_text(mainpage_xaml, encoding="utf-8")
        mainpage_cs = (
            "namespace " + app_name + ";\n\n"
            "public partial class MainPage : ContentPage\n"
            "{\n"
            "    public MainPage()\n"
            "    {\n"
            "        InitializeComponent();\n"
            "    }\n"
            "}\n")
        (proj / "MainPage.xaml.cs").write_text(mainpage_cs, encoding="utf-8")
        logger.log(f"✅ MainPage générée (WebView → {url or 'https://example.com'})")

    if icon_bytes:
        icon_path = proj / "Resources" / "AppIcon" / "appicon.png"
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        icon_path.write_bytes(make_icon_png(icon_bytes, 512))
        logger.log("✅ Icône MAUI écrite (appicon.png)")

    logger.log(f"✅ Projet .NET MAUI prêt : {proj}")
    return proj


def do_build_maui(config, icon_bytes, splash_bytes=None, site_zip_bytes=None):
    global CURRENT_SESSION
    logger = OPS["maui"]
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    CURRENT_SESSION = sid
    try:
        proj = generate_maui_project(sid, config, icon_bytes, site_zip_bytes, logger)
        dotnet = find_dotnet()
        signing_mode = (config.get("signing") or {}).get("mode", "debug")
        build_config = "Release" if signing_mode != "debug" else "Debug"
        tfm = "net8.0-android"
        logger.log(f"🔨 Compilation .NET MAUI ({build_config}, {tfm})...")
        cmd = [dotnet, "build", str(proj), "-f", tfm, "-c", build_config,
               "-p:AndroidPackageFormat=apk"]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800)
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        if r.returncode != 0:
            logger.log("⚠ " + (r.stderr or "")[:800])
            raise RuntimeError("Échec de `dotnet build` — voir logs ci-dessus.")

        apk_dir = proj / "bin" / build_config / tfm
        candidates = list(apk_dir.glob("*-Signed.apk")) or list(apk_dir.glob("*.apk")) if apk_dir.exists() else []
        if not candidates:
            raise RuntimeError(f"Build MAUI terminé mais aucun APK trouvé dans {apk_dir}")
        unsigned_apk = candidates[0]

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_maui.apk"
        min_sdk = config.get("minSdk") or MAUI_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})
        final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# ---- Titanium --------------------------------------------------------

def generate_titanium_project(sid, config, icon_bytes, site_zip_bytes, logger):
    titanium = find_titanium()
    if not titanium:
        raise RuntimeError(
            "Titanium CLI introuvable. Installe le composant 'titanium' depuis "
            "l'écran des composants avant de générer une app Titanium."
        )
    env = _twa_env(logger)
    app_name = (config.get("appName") or "MonApp").strip()
    package = normalize_package_name(config.get("packageName"), fallback="com.example.titanium")
    proj = titanium_project_dir(sid)
    proj.parent.mkdir(parents=True, exist_ok=True)

    logger.log("📦 Création du projet Titanium (titanium create)...")
    cmd = [titanium, "create", "--type", "app", "--name", proj.name, "--id", package,
           "--platforms", "android", "--workspace-dir", str(proj.parent), "--no-prompt", "-f"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0 or not proj.exists():
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `titanium create` — voir logs ci-dessus.")

    source_mode = (config.get("sourceMode") or "url").strip()
    url = (config.get("appUrl") or "").strip()
    resources = proj / "Resources"
    if source_mode == "scratch":
        webroot = resources / "www"
        _write_webroot_from_config(webroot, config, site_zip_bytes, logger)
        app_js = (
            "var win = Ti.UI.createWindow({backgroundColor:'#fff'});\n"
            "var webview = Ti.UI.createWebView({url:'www/index.html'});\n"
            "win.add(webview);\n"
            "win.open();\n")
    else:
        app_js = (
            "var win = Ti.UI.createWindow({backgroundColor:'#fff'});\n"
            f"var webview = Ti.UI.createWebView({{url:'{url or 'https://example.com'}'}});\n"
            "win.add(webview);\n"
            "win.open();\n")
    (resources / "app.js").write_text(app_js, encoding="utf-8")

    if icon_bytes:
        (resources / "android" / "images" / "res-mdpi").mkdir(parents=True, exist_ok=True)
        (resources / "android" / "images" / "res-mdpi" / "appicon.png").write_bytes(make_icon_png(icon_bytes, 48))
        logger.log("✅ Icône Titanium écrite")

    logger.log(f"✅ Projet Titanium prêt : {proj}")
    return proj


def do_build_titanium(config, icon_bytes, splash_bytes=None, site_zip_bytes=None):
    global CURRENT_SESSION
    logger = OPS["titanium"]
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    CURRENT_SESSION = sid
    try:
        proj = generate_titanium_project(sid, config, icon_bytes, site_zip_bytes, logger)
        titanium = find_titanium()
        env = _twa_env(logger)
        logger.log("🔨 Compilation Titanium (titanium build -p android -b)...")
        cmd = [titanium, "build", "-p", "android", "-b", "-d", str(proj)]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=1800)
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        if r.returncode != 0:
            logger.log("⚠ " + (r.stderr or "")[:800])
            raise RuntimeError("Échec de `titanium build` — voir logs ci-dessus.")

        apk_dir = proj / "build" / "android" / "bin"
        candidates = list(apk_dir.glob("*.apk")) if apk_dir.exists() else []
        if not candidates:
            raise RuntimeError(f"Build Titanium terminé mais aucun APK trouvé dans {apk_dir}")
        unsigned_apk = candidates[0]

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_titanium.apk"
        min_sdk = config.get("minSdk") or TITANIUM_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})
        final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# =============================================================
# PIPELINE FLUTTER — "site → app" via un vrai projet Flutter/Dart
# ---------------------------------------------------------------
# 5e famille de méthode : comme Cordova, une vraie WebView native (ici
# via le package webview_flutter), mais avec la toolchain Flutter/Dart
# (flutter create + flutter build apk) plutôt que Gradle appelé
# directement — flutter build apk orchestre lui-même son propre Gradle
# interne, donc pas de réutilisation possible de run_gradle_build ici.
# On réutilise en revanche sign_native_apk : `flutter build apk --release`
# produit par défaut un APK déjà signé avec la config debug (comportement
# du template Flutter par défaut, cf. commentaire dans android/app/build.gradle),
# mais apksigner peut re-signer un APK déjà signé sans problème — donc le
# pipeline reste cohérent avec Cordova/TWA/natif (même clé de sortie, même
# zipalign, même vérification finale) au lieu de garder la signature debug.
# =============================================================
FLUTTER_MIN_SDK_DEFAULT = 21

FLUTTER_ICON_DENSITIES = {
    "mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192,
}

# Densités du logo de splash (drawable-*), pas plein écran : le fond est
# une couleur unie posée par launch_background.xml, le logo est centré
# par-dessus via <bitmap android:gravity="center">.
FLUTTER_SPLASH_DENSITIES = {
    "drawable-mdpi": 192, "drawable-hdpi": 288, "drawable-xhdpi": 384,
    "drawable-xxhdpi": 576, "drawable-xxxhdpi": 768,
}

WEBVIEW_FLUTTER_VERSION = "^4.7.0"


def _flutter_write_splash(proj, splash_bytes, logger, bg_color="#FFFFFF"):
    """Branche le splash natif Android d'un projet Flutter généré par
    `flutter create` : celui-ci scaffold déjà res/drawable/launch_background.xml
    (fond uni, bitmap centré en commentaire) — on écrit le logo par densité
    et on réécrit launch_background.xml pour l'activer réellement, au lieu
    de laisser le splash par défaut (fond blanc uni, sans logo)."""
    res_dir = proj / "android" / "app" / "src" / "main" / "res"
    for density_dir, size in FLUTTER_SPLASH_DENSITIES.items():
        target_dir = res_dir / density_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        png = make_splash_png(splash_bytes, size, size, bg_color=(255, 255, 255, 0))
        (target_dir / "splash_logo.png").write_bytes(png)

    launch_bg_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<layer-list xmlns:android="http://schemas.android.com/apk/res/android">
    <item android:drawable="@color/splashBackground" />
    <item>
        <bitmap
            android:gravity="center"
            android:src="@drawable/splash_logo" />
    </item>
</layer-list>
'''
    drawable_dir = res_dir / "drawable"
    drawable_dir.mkdir(parents=True, exist_ok=True)
    (drawable_dir / "launch_background.xml").write_text(launch_bg_xml, encoding="utf-8")
    drawable_v21_dir = res_dir / "drawable-v21"
    if drawable_v21_dir.exists():
        (drawable_v21_dir / "launch_background.xml").write_text(launch_bg_xml, encoding="utf-8")

    values_dir = res_dir / "values"
    values_dir.mkdir(parents=True, exist_ok=True)
    colors_path = values_dir / "colors.xml"
    if colors_path.exists():
        colors_xml = colors_path.read_text(encoding="utf-8")
        if "splashBackground" not in colors_xml:
            colors_xml = colors_xml.replace(
                "</resources>", f'    <color name="splashBackground">{bg_color}</color>\n</resources>', 1
            )
    else:
        colors_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            f'    <color name="splashBackground">{bg_color}</color>\n</resources>\n'
        )
    colors_path.write_text(colors_xml, encoding="utf-8")

    # Le template par défaut de `flutter create` fait déjà pointer le thème
    # de lancement vers @drawable/launch_background — mais un projet importé
    # (mode template) peut avoir un styles.xml personnalisé qui ne le fait
    # pas, auquel cas les fichiers écrits ci-dessus restent inertes (même
    # limite que le garde-fou React Native ci-dessous).
    styles_path = values_dir / "styles.xml"
    if styles_path.exists() and "launch_background" not in styles_path.read_text(encoding="utf-8"):
        logger.log("⚠ styles.xml ne référence pas @drawable/launch_background — splash écrit mais probablement "
                    "pas actif (projet importé avec un thème de lancement personnalisé). Vérifie le thème appliqué "
                    "à l'activité de lancement dans AndroidManifest.xml.")

    logger.log(f"✅ Splash généré ({len(FLUTTER_SPLASH_DENSITIES)} densités) — launch_background.xml activé")


def find_flutter():
    """flutter téléchargé par setup.js dans tools/flutter/bin/."""
    bundled = TOOLS_DIR / "flutter" / "bin" / ("flutter.bat" if os.name == "nt" else "flutter")
    if bundled.exists():
        return str(bundled)
    return shutil.which("flutter")


def flutter_project_dir(sid):
    return WORK_DIR / sid / "flutter_project"


def _flutter_env(logger):
    """JDK + ANDROID_HOME comme les autres pipelines, + CI=true pour éviter
    le message d'accueil interactif de Flutter (consentement analytics) au
    tout premier lancement, qui sinon peut bloquer un subprocess non-TTY."""
    env = _twa_env(logger)
    env["CI"] = "true"
    return env


def _flutter_org_and_name(package):
    """
    Découpe un identifiant Android complet (com.example.monapp) en
    org (com.example) + nom de projet Flutter valide (snake_case,
    commence par une lettre — contrainte de `flutter create`).
    """
    parts = [p for p in package.split(".") if p]
    if len(parts) >= 2:
        org = ".".join(parts[:-1])
        raw_name = parts[-1]
    else:
        org = "com.example"
        raw_name = parts[0] if parts else "app"
    name = re.sub(r'[^a-z0-9_]', '_', raw_name.lower())
    name = re.sub(r'_+', '_', name).strip('_') or "flutter_app"
    if not re.match(r'^[a-z]', name):
        name = "app_" + name
    return org, name


def _flutter_collect_asset_dirs(www_dir):
    """
    pubspec.yaml ne bundle QUE les fichiers directement listés ou les
    fichiers directs d'un dossier listé (pas de récursion automatique
    comme pour un zip Cordova/www classique) — on énumère donc chaque
    sous-dossier contenant au moins un fichier pour les déclarer un par
    un sous `flutter: assets:`. Chemins retournés relatifs à la racine
    du projet Flutter (ex: "assets/www/", "assets/www/css/"), pas juste
    à assets/ — c'est ce que pubspec.yaml attend.
    """
    proj_root = www_dir.parent.parent  # www_dir = proj/assets/www → proj
    dirs = []
    for root, _, files in os.walk(www_dir):
        if files:
            rel = Path(root).relative_to(proj_root)
            dirs.append(str(rel).replace(os.sep, "/") + "/")
    return sorted(dirs)


def generate_flutter_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger):
    """
    `flutter create` (non-interactif) puis patch minimal : dépendance
    webview_flutter dans pubspec.yaml, lib/main.dart remplacé par une
    simple WebView plein écran chargeant l'URL (mode "url") ou un site
    local embarqué en asset Flutter (mode "scratch", 100% hors-ligne),
    permission INTERNET, label et icônes — même esprit que
    generate_cordova_project. Le mode "template" (import d'un projet
    Flutter existant) est géré par generate_flutter_project_from_template.
    """
    flutter = find_flutter()
    if not flutter:
        raise RuntimeError(
            "Flutter introuvable. Installe le composant 'flutter' depuis "
            "l'écran des composants avant de générer une app Flutter."
        )

    source_mode = (config.get("sourceMode") or "url").strip()

    url = ""
    if source_mode == "scratch":
        pass  # site_zip_bytes optionnel : si absent, fallback sur le dossier-modele flutter (templates/flutter/webroot/)
    else:
        url = (config.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            raise RuntimeError("URL du site invalide ou manquante (doit commencer par http:// ou https://).")

    package = normalize_package_name(config.get("packageName"), fallback="com.example.flutterapp")
    app_name = (config.get("appName") or "MonApp").strip()
    version_name = str(config.get("versionName") or "1.0")
    version_code = str(config.get("versionCode") or "1")
    org, proj_name = _flutter_org_and_name(package)

    proj = flutter_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.parent.mkdir(parents=True, exist_ok=True)

    env = _flutter_env(logger)

    logger.log("📦 Création du projet Flutter (flutter create)...")
    cmd_create = [flutter, "create", "--platforms=android", "--org", org,
                  "--project-name", proj_name, "--no-pub", str(proj)]
    r = subprocess.run(cmd_create, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `flutter create` — voir logs ci-dessus.")

    # ── pubspec.yaml : ajoute webview_flutter + fixe version/build number ──
    pubspec_path = proj / "pubspec.yaml"
    pubspec = pubspec_path.read_text(encoding="utf-8")
    if "webview_flutter" not in pubspec:
        pubspec = pubspec.replace("dependencies:\n", f"dependencies:\n  webview_flutter: {WEBVIEW_FLUTTER_VERSION}\n", 1)
    pubspec = re.sub(r'^version:\s*.*$', f"version: {version_name}+{version_code}", pubspec, count=1, flags=re.MULTILINE)

    www_dir = proj / "assets" / "www"
    if source_mode == "scratch":
        if site_zip_bytes:
            # ── Site local zippé : extrait dans assets/www/ (mêmes
            # conventions d'extraction que le mode scratch Cordova :
            # remontée d'un niveau si tout est dans un sous-dossier) ─────
            logger.log("🗂 Extraction du site local (zip)...")
            www_dir.mkdir(parents=True, exist_ok=True)
            zpath = proj / "site_upload.zip"
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
            if not (www_dir / "index.html").exists():
                raise RuntimeError("Pas d'index.html trouvé à la racine du zip fourni (ni dans un unique sous-dossier).")
        else:
            # Aucun zip fourni : l'IA compte écrire les fichiers ensuite via
            # write_file — on part du dossier-modèle Flutter déjà arrangé
            # (templates/flutter/webroot/), jamais d'assets/www/ vide.
            if not stage_webroot_from_template("flutter", www_dir, logger):
                www_dir.mkdir(parents=True, exist_ok=True)
                (www_dir / "index.html").write_text(_SCRATCH_SKELETON_HTML, encoding="utf-8")
                (www_dir / "style.css").write_text(_SCRATCH_SKELETON_CSS, encoding="utf-8")
                (www_dir / "app.js").write_text(_SCRATCH_SKELETON_JS, encoding="utf-8")
                logger.log("✅ Squelette Flutter écrit (assets/www/index.html + style.css + app.js).")

        asset_dirs = _flutter_collect_asset_dirs(www_dir)
        assets_yaml = "".join(f"    - {d}\n" for d in asset_dirs)
        if "uses-material-design: true" in pubspec:
            pubspec = pubspec.replace(
                "uses-material-design: true",
                "uses-material-design: true\n\n  assets:\n" + assets_yaml,
                1
            )
        else:
            pubspec += "\nflutter:\n  assets:\n" + assets_yaml
        logger.log(f"✅ assets/www/ déclaré dans pubspec.yaml ({len(asset_dirs)} dossier(s), app 100% hors-ligne)")


    pubspec_path.write_text(pubspec, encoding="utf-8")
    logger.log("✅ pubspec.yaml patché (webview_flutter + version" + (" + assets" if source_mode == "scratch" else "") + ")")

    logger.log("📥 Récupération des dépendances (flutter pub get)...")
    r = subprocess.run([flutter, "pub", "get"], cwd=str(proj), env=env, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=300)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `flutter pub get` — voir logs ci-dessus (dépendance webview_flutter introuvable ?).")

    # ── lib/main.dart : remplace le compteur par défaut par une WebView
    # plein écran qui charge directement l'URL du site (mode url) ou
    # l'asset local embarqué (mode scratch, loadFlutterAsset au lieu de
    # loadRequest — aucune requête réseau nécessaire) ────────────────────
    if source_mode == "scratch":
        load_call = "..loadFlutterAsset('assets/www/index.html')"
    else:
        load_call = f"..loadRequest(Uri.parse({json.dumps(url)}))"
    main_dart = f'''import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

void main() {{
  runApp(const MyApp());
}}

class MyApp extends StatelessWidget {{
  const MyApp({{super.key}});

  @override
  Widget build(BuildContext context) {{
    return MaterialApp(
      title: {json.dumps(app_name)},
      debugShowCheckedModeBanner: false,
      home: const WebViewScreen(),
    );
  }}
}}

class WebViewScreen extends StatefulWidget {{
  const WebViewScreen({{super.key}});

  @override
  State<WebViewScreen> createState() => _WebViewScreenState();
}}

class _WebViewScreenState extends State<WebViewScreen> {{
  late final WebViewController _controller;

  @override
  void initState() {{
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFFFFFFFF))
      {load_call};
  }}

  @override
  Widget build(BuildContext context) {{
    return Scaffold(
      body: SafeArea(
        child: WebViewWidget(controller: _controller),
      ),
    );
  }}
}}
'''
    (proj / "lib" / "main.dart").write_text(main_dart, encoding="utf-8")
    logger.log("✅ lib/main.dart généré (WebView plein écran vers " + ("l'asset local" if source_mode == "scratch" else "le site") + ")")

    # ── AndroidManifest.xml : label affiché + permission INTERNET (le
    # template Flutter par défaut ne la déclare pas — sans elle la
    # WebView échoue silencieusement au chargement) ─────────────────────
    manifest_path = proj / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest = re.sub(r'android:label="[^"]*"', f'android:label="{app_name}"', manifest, count=1)
    if "android.permission.INTERNET" not in manifest:
        manifest = manifest.replace(
            "<application",
            '<uses-permission android:name="android.permission.INTERNET" />\n    <application',
            1
        )
    manifest_path.write_text(manifest, encoding="utf-8")
    logger.log("✅ AndroidManifest.xml patché (label + permission INTERNET)")

    # ── Icônes : écrase les mipmaps par défaut du template avec l'icône
    # fournie (ou une icône unie par défaut) — mêmes densités que Cordova ──
    res_dir = proj / "android" / "app" / "src" / "main" / "res"
    written = 0
    for density_dir, size in FLUTTER_ICON_DENSITIES.items():
        target_dir = res_dir / density_dir
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
        png = make_icon_png(icon_bytes, size)
        (target_dir / "ic_launcher.png").write_bytes(png)
        written += 1
    logger.log(f"✅ Icônes générées ({written} densités)")

    if splash_bytes:
        _flutter_write_splash(proj, splash_bytes, logger)

    write_hybrid_session_meta(sid, "flutter", config)
    enforce_project_entrypoint(sid, kind_hint="flutter", logger=logger)
    return proj


def _find_flutter_project_root(extract_dir):
    """
    Cherche pubspec.yaml à la racine du zip importé, ou un niveau plus
    bas si tout est dans un unique sous-dossier — même logique de
    remontée que _find_cordova_project_root.
    """
    if (extract_dir / "pubspec.yaml").exists():
        return extract_dir
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "pubspec.yaml").exists():
        return children[0]
    return None


def generate_flutter_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger):
    """
    Mode "template" : importe un projet Flutter EXISTANT (zippé) au lieu
    d'en générer un vierge. On ne touche PAS au package (applicationId
    Android / bundle Dart déjà en place dans le projet importé) — on ne
    patch que ce que l'utilisateur a explicitement renseigné (nom
    affiché, version, icône), même prudence que le mode template Cordova.
    """
    if not project_zip_bytes:
        raise RuntimeError("Mode template : un zip du projet Flutter existant est requis.")

    proj = flutter_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.mkdir(parents=True, exist_ok=True)

    logger.log("🗂 Extraction du projet Flutter importé (zip)...")
    zpath = proj.parent / "flutter_template_upload.zip"
    zpath.write_bytes(project_zip_bytes)
    extract_tmp = proj.parent / "flutter_template_extract"
    shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        _safe_extract_zip(zf, extract_tmp, logger)
    zpath.unlink(missing_ok=True)

    root = _find_flutter_project_root(extract_tmp)
    if not root:
        shutil.rmtree(extract_tmp, ignore_errors=True)
        raise RuntimeError(
            "pubspec.yaml introuvable dans le zip importé (ni à la racine, ni dans un unique "
            "sous-dossier) — ce n'est pas un projet Flutter valide."
        )
    for item in root.iterdir():
        shutil.move(str(item), str(proj / item.name))
    shutil.rmtree(extract_tmp, ignore_errors=True)
    logger.log(f"✅ Projet importé ({(proj / 'pubspec.yaml').stat().st_size} octets de pubspec.yaml)")

    if not (proj / "android").exists():
        raise RuntimeError(
            "Le projet Flutter importé n'a pas de dossier android/ — impossible de compiler un "
            "APK à partir de ce zip (projet créé sans la plateforme Android ?)."
        )

    # ── Patch OPTIONNEL : uniquement les champs explicitement renseignés
    # (le reste — dépendances, plugins, id — n'est jamais touché) ────────
    app_name_override = (config.get("appName") or "").strip()
    version_override = (config.get("versionName") or "").strip()
    if app_name_override or version_override:
        pubspec_path = proj / "pubspec.yaml"
        pubspec = pubspec_path.read_text(encoding="utf-8")
        if version_override:
            version_code = str(config.get("versionCode") or "1")
            pubspec = re.sub(r'^version:\s*.*$', f"version: {version_override}+{version_code}",
                              pubspec, count=1, flags=re.MULTILINE)
            logger.log(f"✅ Version surchargée : {version_override}")
        pubspec_path.write_text(pubspec, encoding="utf-8")
        if app_name_override:
            manifest_path = proj / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
            if manifest_path.exists():
                manifest = manifest_path.read_text(encoding="utf-8")
                manifest = re.sub(r'android:label="[^"]*"', f'android:label="{app_name_override}"',
                                   manifest, count=1)
                manifest_path.write_text(manifest, encoding="utf-8")
                logger.log(f"✅ Nom de l'app surchargé : {app_name_override}")

    if icon_bytes:
        res_dir = proj / "android" / "app" / "src" / "main" / "res"
        written = 0
        for density_dir, size in FLUTTER_ICON_DENSITIES.items():
            target_dir = res_dir / density_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            png = make_icon_png(icon_bytes, size)
            (target_dir / "ic_launcher.png").write_bytes(png)
            written += 1
        logger.log(f"✅ Icônes surchargées ({written} densités)")

    if splash_bytes:
        _flutter_write_splash(proj, splash_bytes, logger)

    logger.log("📥 Récupération des dépendances (flutter pub get)...")
    flutter = find_flutter()
    if flutter:
        env = _flutter_env(logger)
        r = subprocess.run([flutter, "pub", "get"], cwd=str(proj), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=300)
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        if r.returncode != 0:
            logger.log("⚠ " + (r.stderr or "")[:500])
            raise RuntimeError("Échec de `flutter pub get` sur le projet importé — voir logs ci-dessus.")

    write_hybrid_session_meta(sid, "flutter", config)
    enforce_project_entrypoint(sid, kind_hint="flutter", logger=logger)
    return proj


def run_flutter_build(proj, logger, build_type="release"):
    """
    `flutter build apk` orchestre son propre Gradle interne (contrairement
    à Cordova) — impossible/inutile de passer par run_gradle_build ici.
    """
    flutter = find_flutter()
    env = _flutter_env(logger)
    variant = "release" if build_type == "release" else "debug"

    logger.log(f"🔨 Compilation Flutter ({variant})... (peut prendre plusieurs minutes la 1ère fois)")
    cmd = [flutter, "build", "apk", f"--{variant}"]
    logger.log(f"$ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=str(proj), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout flutter build (30 min dépassées) — build annulé.")
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    for line in (r.stderr or "").strip().split("\n"):
        if line.strip(): logger.log("⚠ " + line)
    if r.returncode != 0:
        raise RuntimeError("Échec de la compilation Flutter — voir les logs ci-dessus pour la cause exacte.")

    apk_path = proj / "build" / "app" / "outputs" / "flutter-apk" / f"app-{variant}.apk"
    if not apk_path.exists():
        candidates = list((proj / "build" / "app" / "outputs" / "flutter-apk").glob("*.apk")) \
            if (proj / "build" / "app" / "outputs" / "flutter-apk").exists() else []
        if not candidates:
            raise RuntimeError(f"Build Flutter terminé mais aucun APK trouvé pour la variante {variant}.")
        apk_path = candidates[0]
    logger.log(f"✅ APK Flutter compilé : {apk_path.name}")
    return apk_path


# =============================================================
# PIPELINE REACT NATIVE — "site → app" via un vrai projet RN/Android
# ---------------------------------------------------------------
# 6e famille de méthode : embarque le site dans une WebView via le
# module communautaire react-native-webview. C'EST LA MÉTHODE LA PLUS
# FRAGILE DES TROIS (Cordova/Flutter/RN), pour des raisons réelles et
# pas juste par précaution :
#   - `react-native init` télécharge un template complet depuis npm ET
#     lance `npm install` (des centaines de paquets) → sensible à un
#     réseau instable ou lent, contrairement à Cordova/Flutter qui
#     téléchargent beaucoup moins à la création du projet.
#   - react-native-webview est installé après coup via npm (dépendance
#     supplémentaire, donc point de défaillance réseau de plus).
#   - Autolinking Android : fonctionne nativement depuis RN 0.71+ sans
#     configuration manuelle, mais reste plus opaque à déboguer que les
#     deux autres pipelines si une version de react-native-webview n'est
#     pas compatible avec la version de React Native du template.
#   - Comme pour Cordova, on invoque directement notre propre binaire
#     Gradle sur android/ (run_gradle_build) plutôt que le gradlew généré
#     par le template — qui, laissé à lui-même, tenterait de télécharger
#     SA PROPRE version de Gradle depuis services.gradle.org.
#   - On compile toujours en mode "assembleRelease" (jamais "assembleDebug")
#     même si l'utilisateur choisit une signature debug : côté React
#     Native, seule la variante release embarque le bundle JS dans l'APK
#     via le hook Gradle bundleReleaseJsAndAssets. Une "assembleDebug"
#     produirait un APK qui attend un serveur Metro externe et ne
#     fonctionnerait pas de façon autonome sur un téléphone.
# =============================================================
RN_ICON_DENSITIES = {
    "mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192,
}
REACT_NATIVE_WEBVIEW_VERSION = "^13.8.6"

# Densités du logo de splash (drawable-*) — même convention que Flutter :
# fond uni + logo centré, jamais étiré.
RN_SPLASH_DENSITIES = {
    "drawable-mdpi": 192, "drawable-hdpi": 288, "drawable-xhdpi": 384,
    "drawable-xxhdpi": 576, "drawable-xxxhdpi": 768,
}


def _rn_write_splash(proj, splash_bytes, logger, bg_color="#FFFFFF"):
    """Branche un splash natif sur un projet React Native, qui n'en a
    aucun par défaut (contrairement à Flutter, `react-native init` ne
    scaffold rien pour ça) : android:windowBackground de AppTheme reste
    posé le temps que le thread JS mesure et affiche la racine RN — donc
    le fond+logo posé ici agit comme splash screen sans dépendance
    externe (react-native-splash-screen), même principe que de nombreux
    tutoriels 'splash sans lib'."""
    res_dir = proj / "android" / "app" / "src" / "main" / "res"
    for density_dir, size in RN_SPLASH_DENSITIES.items():
        target_dir = res_dir / density_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        png = make_splash_png(splash_bytes, size, size, bg_color=(255, 255, 255, 0))
        (target_dir / "splash_logo.png").write_bytes(png)

    drawable_dir = res_dir / "drawable"
    drawable_dir.mkdir(parents=True, exist_ok=True)
    (drawable_dir / "splash_background.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<layer-list xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <item android:drawable="@color/splashBackground" />\n'
        '    <item>\n'
        '        <bitmap\n'
        '            android:gravity="center"\n'
        '            android:src="@drawable/splash_logo" />\n'
        '    </item>\n'
        '</layer-list>\n',
        encoding="utf-8"
    )

    values_dir = res_dir / "values"
    values_dir.mkdir(parents=True, exist_ok=True)
    colors_path = values_dir / "colors.xml"
    if colors_path.exists():
        colors_xml = colors_path.read_text(encoding="utf-8")
        if "splashBackground" not in colors_xml:
            colors_xml = colors_xml.replace(
                "</resources>", f'    <color name="splashBackground">{bg_color}</color>\n</resources>', 1
            )
    else:
        colors_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            f'    <color name="splashBackground">{bg_color}</color>\n</resources>\n'
        )
    colors_path.write_text(colors_xml, encoding="utf-8")

    styles_path = values_dir / "styles.xml"
    if styles_path.exists():
        styles_xml = styles_path.read_text(encoding="utf-8")
        if "android:windowBackground" in styles_xml:
            styles_xml = re.sub(
                r'<item name="android:windowBackground">[^<]*</item>',
                '<item name="android:windowBackground">@drawable/splash_background</item>',
                styles_xml, count=1
            )
        else:
            styles_xml = re.sub(
                r'(<style name="AppTheme"[^>]*>)',
                r'\1\n        <item name="android:windowBackground">@drawable/splash_background</item>',
                styles_xml, count=1
            )
        styles_path.write_text(styles_xml, encoding="utf-8")
        logger.log(f"✅ Splash généré ({len(RN_SPLASH_DENSITIES)} densités) — styles.xml patché (windowBackground)")
    else:
        logger.log("⚠ styles.xml introuvable — splash_background.xml écrit mais pas activé "
                    "(ajoute manuellement android:windowBackground=@drawable/splash_background à AppTheme).")


def find_react_native_cli():
    """react-native (CLI globale @react-native-community/cli) installée
    en npm-global par setup.js dans tools/nodejs/."""
    bundled = TOOLS_DIR / "nodejs" / ("react-native.cmd" if os.name == "nt" else "react-native")
    if bundled.exists():
        return str(bundled)
    return shutil.which("react-native")


def find_npm():
    bundled = TOOLS_DIR / "nodejs" / ("npm.cmd" if os.name == "nt" else "npm")
    if bundled.exists():
        return str(bundled)
    return shutil.which("npm")


def react_native_project_dir(sid):
    return WORK_DIR / sid / "rn_project"


def _rn_env(logger):
    """JDK + ANDROID_HOME comme les autres pipelines hybrides, + Node.js
    en tête de PATH (nécessaire aux sous-process npm/npx internes à la
    CLI React Native, même quand on invoque react-native.cmd directement)."""
    env = _twa_env(logger)
    nodejs_dir = TOOLS_DIR / "nodejs"
    if nodejs_dir.exists():
        env["PATH"] = str(nodejs_dir) + os.pathsep + env.get("PATH", "")
    env["CI"] = "true"
    return env


def _rn_project_name(app_name):
    """
    `react-native init` exige un nom de projet alphanumérique commençant
    par une lettre (utilisé aussi bien comme nom de dossier que comme
    identifiant Java/Gradle) — donc plus strict que le nom d'app affiché.
    """
    name = re.sub(r'[^A-Za-z0-9]', '', app_name or "")
    if not name or not name[0].isalpha():
        name = "App" + name
    return name[:50] or "MonAppRN"


def generate_react_native_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger):
    """
    `react-native init` (avec --package-name pour éviter tout patch
    manuel fragile du package Java/AndroidManifest après coup), puis
    ajout de react-native-webview et remplacement de App.tsx/App.js par
    une WebView plein écran chargeant l'URL (mode "url") ou un site local
    embarqué dans les assets Android (mode "scratch", 100% hors-ligne) —
    même esprit que generate_cordova_project / generate_flutter_project,
    mais avec des étapes réseau supplémentaires (voir avertissement en
    tête de section). Le mode "template" (import d'un projet RN existant)
    est géré par generate_react_native_project_from_template.
    """
    rn_cli = find_react_native_cli()
    if not rn_cli:
        raise RuntimeError(
            "React Native CLI introuvable. Installe le composant 'reactNativeCli' "
            "depuis l'écran des composants avant de générer une app React Native."
        )
    npm = find_npm()
    if not npm:
        raise RuntimeError("npm introuvable — installe le composant 'nodejs' avant de générer une app React Native.")

    source_mode = (config.get("sourceMode") or "url").strip()

    url = ""
    if source_mode == "scratch":
        pass  # site_zip_bytes optionnel : si absent, fallback sur le dossier-modele reactnative (templates/reactnative/webroot/)
    else:
        url = (config.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            raise RuntimeError("URL du site invalide ou manquante (doit commencer par http:// ou https://).")

    package = normalize_package_name(config.get("packageName"), fallback="com.example.rnapp")
    app_name = (config.get("appName") or "MonApp").strip()
    version_name = str(config.get("versionName") or "1.0")
    version_code = str(config.get("versionCode") or "1")
    proj_name = _rn_project_name(app_name)

    proj = react_native_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.parent.mkdir(parents=True, exist_ok=True)

    env = _rn_env(logger)

    logger.log("📦 Création du projet React Native (react-native init)... (télécharge le template + npm install, peut prendre plusieurs minutes)")
    cmd_init = [rn_cli, "init", proj_name, "--directory", str(proj),
                "--package-name", package, "--skip-install", "false"]
    r = subprocess.run(cmd_init, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=900)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `react-native init` — voir logs ci-dessus (souvent un souci réseau npm).")

    logger.log("📥 Installation de react-native-webview (npm install)...")
    r = subprocess.run([npm, "install", f"react-native-webview@{REACT_NATIVE_WEBVIEW_VERSION}", "--save"],
                        cwd=str(proj), env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=600)
    for line in (r.stdout or "").strip().split("\n"):
        if line.strip(): logger.log(line)
    if r.returncode != 0:
        logger.log("⚠ " + (r.stderr or "")[:500])
        raise RuntimeError("Échec de `npm install react-native-webview` — voir logs ci-dessus.")

    if source_mode == "scratch":
        assets_www = proj / "android" / "app" / "src" / "main" / "assets" / "www"
        if site_zip_bytes:
            # ── Site local zippé : extrait directement dans les assets
            # Android natifs (packagés dans l'APK par Gradle sans config
            # supplémentaire), puis chargé via file:///android_asset/ — pas
            # de mécanisme de bundling déclaratif côté RN comme pubspec.yaml,
            # donc on passe par le dossier assets/ natif comme pour Cordova
            # www/, mêmes conventions d'extraction (remontée d'un niveau si
            # tout est dans un unique sous-dossier) ──────────────────────────
            logger.log("🗂 Extraction du site local (zip)...")
            assets_www.mkdir(parents=True, exist_ok=True)
            zpath = proj / "site_upload.zip"
            zpath.write_bytes(site_zip_bytes)
            with zipfile.ZipFile(zpath) as zf:
                _safe_extract_zip(zf, assets_www, logger)
            children = list(assets_www.iterdir())
            if len(children) == 1 and children[0].is_dir() and not (assets_www / "index.html").exists():
                inner = children[0]
                for item in inner.iterdir():
                    shutil.move(str(item), str(assets_www / item.name))
                inner.rmdir()
            zpath.unlink(missing_ok=True)
            if not (assets_www / "index.html").exists():
                raise RuntimeError("Pas d'index.html trouvé à la racine du zip fourni (ni dans un unique sous-dossier).")
            logger.log("✅ Site local extrait dans android/app/src/main/assets/www/ (app 100% hors-ligne)")
        else:
            # Aucun zip fourni : l'IA compte écrire les fichiers ensuite via
            # write_file — on part du dossier-modèle React Native déjà
            # arrangé (templates/reactnative/webroot/), jamais d'assets/www/ vide.
            if not stage_webroot_from_template("reactnative", assets_www, logger):
                assets_www.mkdir(parents=True, exist_ok=True)
                (assets_www / "index.html").write_text(_SCRATCH_SKELETON_HTML, encoding="utf-8")
                (assets_www / "style.css").write_text(_SCRATCH_SKELETON_CSS, encoding="utf-8")
                (assets_www / "app.js").write_text(_SCRATCH_SKELETON_JS, encoding="utf-8")
                logger.log("✅ Squelette React Native écrit (android/app/src/main/assets/www/index.html + style.css + app.js).")

    # ── App.tsx (ou App.js selon la version du template) : remplace
    # l'écran de démarrage par une WebView plein écran ──────────────────
    entry = proj / "App.tsx"
    if not entry.exists():
        entry = proj / "App.js"
    if source_mode == "scratch":
        webview_source = "{{ uri: 'file:///android_asset/www/index.html' }}"
    else:
        webview_source = f"{{{{ uri: {json.dumps(url)} }}}}"
    app_source = f'''import React from 'react';
import {{ SafeAreaView, StatusBar, StyleSheet }} from 'react-native';
import {{ WebView }} from 'react-native-webview';

function App() {{
  return (
    <SafeAreaView style={{styles.container}}>
      <StatusBar barStyle="dark-content" />
      <WebView source={webview_source} style={{styles.webview}} allowFileAccess={{true}} />
    </SafeAreaView>
  );
}}

const styles = StyleSheet.create({{
  container: {{ flex: 1, backgroundColor: '#fff' }},
  webview: {{ flex: 1 }},
}});

export default App;
'''
    entry.write_text(app_source, encoding="utf-8")
    logger.log(f"✅ {entry.name} généré (WebView plein écran vers " + ("le site local embarqué" if source_mode == "scratch" else "le site") + ")")

    # ── strings.xml : nom affiché de l'app ──────────────────────────────
    strings_path = proj / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
    if strings_path.exists():
        content = strings_path.read_text(encoding="utf-8")
        content = re.sub(r'(<string name="app_name">)[^<]*(</string>)',
                          rf'\1{app_name}\2', content, count=1)
        strings_path.write_text(content, encoding="utf-8")

    # ── android/app/build.gradle : version affichée dans le Play Store /
    # sur l'appareil (versionCode/versionName) ──────────────────────────
    build_gradle_path = proj / "android" / "app" / "build.gradle"
    if build_gradle_path.exists():
        content = build_gradle_path.read_text(encoding="utf-8")
        content = re.sub(r'versionCode\s+\d+', f'versionCode {version_code}', content, count=1)
        content = re.sub(r'versionName\s+"[^"]*"', f'versionName "{version_name}"', content, count=1)
        build_gradle_path.write_text(content, encoding="utf-8")
        logger.log("✅ build.gradle patché (versionCode/versionName)")

    # ── Icônes : le template RN utilise des icônes adaptatives (XML sous
    # mipmap-anydpi-v26 référençant foreground/background séparés) qu'on
    # ne recompose pas ici — on supprime ce dossier pour forcer Android à
    # retomber sur les PNG classiques qu'on écrit ensuite, plus simple et
    # fiable qu'une recomposition XML de l'icône adaptative. ────────────
    anydpi_dir = proj / "android" / "app" / "src" / "main" / "res" / "mipmap-anydpi-v26"
    if anydpi_dir.exists():
        shutil.rmtree(anydpi_dir)
    res_dir = proj / "android" / "app" / "src" / "main" / "res"
    written = 0
    for density_dir, size in RN_ICON_DENSITIES.items():
        target_dir = res_dir / density_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        png = make_icon_png(icon_bytes, size)
        (target_dir / "ic_launcher.png").write_bytes(png)
        (target_dir / "ic_launcher_round.png").write_bytes(png)
        written += 1
    logger.log(f"✅ Icônes générées ({written} densités, icône adaptative désactivée au profit du PNG classique)")

    if splash_bytes:
        _rn_write_splash(proj, splash_bytes, logger)

    write_hybrid_session_meta(sid, "reactnative", config)
    enforce_project_entrypoint(sid, kind_hint="reactnative", logger=logger)
    return proj


def _find_rn_project_root(extract_dir):
    """
    Cherche un package.json accompagné d'un dossier android/ (signature
    d'un projet React Native, un simple package.json ne suffit pas à le
    distinguer d'un projet Node quelconque) à la racine du zip importé,
    ou un niveau plus bas — même logique de remontée que les autres
    modes template.
    """
    def _looks_like_rn(d):
        return (d / "package.json").exists() and (d / "android").is_dir()
    if _looks_like_rn(extract_dir):
        return extract_dir
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(children) == 1 and _looks_like_rn(children[0]):
        return children[0]
    return None


def generate_react_native_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger):
    """
    Mode "template" : importe un projet React Native EXISTANT (zippé) au
    lieu d'en générer un vierge. On ne touche PAS au package Java /
    applicationId déjà en place — on ne patch que ce que l'utilisateur a
    explicitement renseigné (nom affiché, version, icône), même prudence
    que les modes template Cordova/Flutter. Si node_modules/ est absent
    du zip (cas fréquent — dossier volumineux souvent exclu à l'export),
    on relance `npm install` avant de rendre la main.
    """
    if not project_zip_bytes:
        raise RuntimeError("Mode template : un zip du projet React Native existant est requis.")

    proj = react_native_project_dir(sid)
    if proj.exists():
        shutil.rmtree(proj)
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.mkdir(parents=True, exist_ok=True)

    logger.log("🗂 Extraction du projet React Native importé (zip)...")
    zpath = proj.parent / "rn_template_upload.zip"
    zpath.write_bytes(project_zip_bytes)
    extract_tmp = proj.parent / "rn_template_extract"
    shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        _safe_extract_zip(zf, extract_tmp, logger)
    zpath.unlink(missing_ok=True)

    root = _find_rn_project_root(extract_tmp)
    if not root:
        shutil.rmtree(extract_tmp, ignore_errors=True)
        raise RuntimeError(
            "package.json + dossier android/ introuvables dans le zip importé (ni à la racine, "
            "ni dans un unique sous-dossier) — ce n'est pas un projet React Native valide."
        )
    for item in root.iterdir():
        shutil.move(str(item), str(proj / item.name))
    shutil.rmtree(extract_tmp, ignore_errors=True)
    logger.log(f"✅ Projet importé ({(proj / 'package.json').stat().st_size} octets de package.json)")

    # ── Patch OPTIONNEL : uniquement les champs explicitement renseignés ──
    app_name_override = (config.get("appName") or "").strip()
    version_override = (config.get("versionName") or "").strip()
    version_code_override = config.get("versionCode")
    if app_name_override:
        strings_path = proj / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
        if strings_path.exists():
            content = strings_path.read_text(encoding="utf-8")
            content = re.sub(r'(<string name="app_name">)[^<]*(</string>)',
                              rf'\1{app_name_override}\2', content, count=1)
            strings_path.write_text(content, encoding="utf-8")
            logger.log(f"✅ Nom de l'app surchargé : {app_name_override}")
    if version_override or version_code_override:
        build_gradle_path = proj / "android" / "app" / "build.gradle"
        if build_gradle_path.exists():
            content = build_gradle_path.read_text(encoding="utf-8")
            if version_code_override:
                content = re.sub(r'versionCode\s+\d+', f'versionCode {version_code_override}', content, count=1)
            if version_override:
                content = re.sub(r'versionName\s+"[^"]*"', f'versionName "{version_override}"', content, count=1)
            build_gradle_path.write_text(content, encoding="utf-8")
            logger.log("✅ build.gradle surchargé (version)")

    if icon_bytes:
        anydpi_dir = proj / "android" / "app" / "src" / "main" / "res" / "mipmap-anydpi-v26"
        if anydpi_dir.exists():
            shutil.rmtree(anydpi_dir)
        res_dir = proj / "android" / "app" / "src" / "main" / "res"
        written = 0
        for density_dir, size in RN_ICON_DENSITIES.items():
            target_dir = res_dir / density_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            png = make_icon_png(icon_bytes, size)
            (target_dir / "ic_launcher.png").write_bytes(png)
            (target_dir / "ic_launcher_round.png").write_bytes(png)
            written += 1
        logger.log(f"✅ Icônes surchargées ({written} densités)")

    if splash_bytes:
        _rn_write_splash(proj, splash_bytes, logger)

    if not (proj / "node_modules").exists():
        npm = find_npm()
        if not npm:
            raise RuntimeError("npm introuvable — installe le composant 'nodejs' avant de compiler un projet React Native importé.")
        logger.log("📥 node_modules/ absent du zip importé — installation (npm install)... (peut prendre plusieurs minutes)")
        env = _rn_env(logger)
        r = subprocess.run([npm, "install"], cwd=str(proj), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=900)
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        if r.returncode != 0:
            logger.log("⚠ " + (r.stderr or "")[:500])
            raise RuntimeError("Échec de `npm install` sur le projet importé — voir logs ci-dessus.")

    write_hybrid_session_meta(sid, "reactnative", config)
    enforce_project_entrypoint(sid, kind_hint="reactnative", logger=logger)
    return proj


def do_generate_rn_session(config, icon_bytes, splash_bytes=None, site_zip_bytes=None, project_zip_bytes=None):
    """Génère UNIQUEMENT le projet React Native (pas de build Gradle) —
    session éditable dans l'explorer avant compilation séparée via
    /build-rn avec {"session": sid}. Token OPS['rn_gen'].
    Dispatch sur config['sourceMode'] : 'url' (défaut) / 'scratch' / 'template'."""
    global CURRENT_SESSION
    logger = OPS["rn_gen"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    sid = new_session_id()
    logger.session = sid
    try:
        source_mode = (config.get("sourceMode") or "url").strip()
        if source_mode == "template":
            generate_react_native_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger)
        else:
            generate_react_native_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        # BUG-FIX : voir do_generate_cordova_session — même correctif pour
        # que /tree et /recompile pointent vers le projet RN fraîchement créé.
        CURRENT_SESSION = sid
        logger.log(f"✅ Projet React Native prêt — session {sid} (modifiable dans l'explorer)")
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


def do_build_react_native(config, icon_bytes, splash_bytes=None, site_zip_bytes=None, project_zip_bytes=None):
    """
    Orchestration complète React Native : token OPS['rn'] dédié.
    Dispatch sur config['sourceMode'] : 'url' (défaut) / 'scratch' / 'template'.
    Toujours assembleRelease (voir note en tête de section), quelle que
    soit la signature choisie ensuite — le "debug" ne concerne ici que la
    clé de signature, pas la variante Gradle compilée.
    Si config['session'] pointe vers une session déjà générée (via
    /rn-generate, potentiellement modifiée dans l'explorer), on saute la
    (re)génération et on compile directement le projet existant.
    """
    logger = OPS["rn"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    existing_sid = (config.get("session") or "").strip()
    if existing_sid:
        sid = existing_sid
        logger.session = sid
        try:
            proj = react_native_project_dir(sid)
            if not proj.exists():
                raise RuntimeError(f"Session {sid} introuvable ou projet React Native absent — régénère.")
            logger.log(f"♻ Compilation depuis la session existante {sid} (fichiers tels que modifiés)")
            _assert_entrypoint_ready(sid, "reactnative", logger)
            android_dir = proj / "android"
            unsigned_apk = run_gradle_build(android_dir, logger, "assembleRelease")
            app_name = (config.get("appName") or "MonApp").strip()
            version_name = str(config.get("versionName") or "1.0")
            out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_reactnative.apk"
            min_sdk = config.get("minSdk") or NATIVE_MIN_SDK_DEFAULT
            signing = config.get("signing", {"mode": "debug"})
            final_apk = sign_native_apk(unsigned_apk, signing, android_dir, out_name, min_sdk, logger)
            logger.result_file = str(final_apk)
            logger.status = "done"
        except Exception as e:
            import traceback
            logger.log(f"❌ Erreur: {e}")
            logger.log(traceback.format_exc())
            logger.status = "error"
        return
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    global CURRENT_SESSION
    CURRENT_SESSION = sid
    try:
        source_mode = (config.get("sourceMode") or "url").strip()
        if source_mode == "template":
            proj = generate_react_native_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger)
        else:
            proj = generate_react_native_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        _assert_entrypoint_ready(sid, "reactnative", logger)
        android_dir = proj / "android"
        unsigned_apk = run_gradle_build(android_dir, logger, "assembleRelease")

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_reactnative.apk"
        min_sdk = config.get("minSdk") or NATIVE_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})

        final_apk = sign_native_apk(unsigned_apk, signing, android_dir, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


def do_generate_flutter_session(config, icon_bytes, splash_bytes=None, site_zip_bytes=None, project_zip_bytes=None):
    """Génère UNIQUEMENT le projet Flutter (pas de build) — session
    éditable dans l'explorer avant compilation séparée via /build-flutter
    avec {"session": sid}. Token OPS['flutter_gen'].
    Dispatch sur config['sourceMode'] : 'url' (défaut) / 'scratch' / 'template'."""
    global CURRENT_SESSION
    logger = OPS["flutter_gen"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    sid = new_session_id()
    logger.session = sid
    try:
        source_mode = (config.get("sourceMode") or "url").strip()
        if source_mode == "template":
            generate_flutter_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger)
        else:
            generate_flutter_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        # BUG-FIX : voir do_generate_cordova_session — même correctif pour
        # que /tree et /recompile pointent vers le projet Flutter fraîchement créé.
        CURRENT_SESSION = sid
        logger.log(f"✅ Projet Flutter prêt — session {sid} (modifiable dans l'explorer)")
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


def do_build_flutter(config, icon_bytes, splash_bytes=None, site_zip_bytes=None, project_zip_bytes=None):
    """
    Orchestration complète Flutter : token OPS['flutter'] dédié.
    Dispatch sur config['sourceMode'] : 'url' (défaut) / 'scratch' / 'template'.
    sign_native_apk re-signe l'APK avec notre clé (debug ou perso) même
    s'il est déjà signé par défaut par le template Flutter — apksigner
    accepte de re-signer un APK existant sans problème.
    Si config['session'] pointe vers une session déjà générée (via
    /flutter-generate, potentiellement modifiée dans l'explorer), on
    saute la (re)génération et on compile directement le projet existant.
    """
    logger = OPS["flutter"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
    existing_sid = (config.get("session") or "").strip()
    if existing_sid:
        sid = existing_sid
        logger.session = sid
        try:
            proj = flutter_project_dir(sid)
            if not proj.exists():
                raise RuntimeError(f"Session {sid} introuvable ou projet Flutter absent — régénère.")
            logger.log(f"♻ Compilation depuis la session existante {sid} (fichiers tels que modifiés)")
            _assert_entrypoint_ready(sid, "flutter", logger)
            build_type = "debug" if (config.get("signing", {}).get("mode") == "debug") else "release"
            unsigned_apk = run_flutter_build(proj, logger, build_type)
            app_name = (config.get("appName") or "MonApp").strip()
            version_name = str(config.get("versionName") or "1.0")
            out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_flutter.apk"
            min_sdk = config.get("minSdk") or FLUTTER_MIN_SDK_DEFAULT
            signing = config.get("signing", {"mode": "debug"})
            final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
            logger.result_file = str(final_apk)
            logger.status = "done"
        except Exception as e:
            import traceback
            logger.log(f"❌ Erreur: {e}")
            logger.log(traceback.format_exc())
            logger.status = "error"
        return
    sid = new_session_id()
    logger.session = sid
    # BUG-FIX (root cause) : voir commentaire équivalent dans do_build_native.
    global CURRENT_SESSION
    CURRENT_SESSION = sid
    try:
        source_mode = (config.get("sourceMode") or "url").strip()
        if source_mode == "template":
            proj = generate_flutter_project_from_template(sid, config, icon_bytes, splash_bytes, project_zip_bytes, logger)
        else:
            proj = generate_flutter_project(sid, config, icon_bytes, splash_bytes, site_zip_bytes, logger)
        _assert_entrypoint_ready(sid, "flutter", logger)
        build_type = "debug" if (config.get("signing", {}).get("mode") == "debug") else "release"
        unsigned_apk = run_flutter_build(proj, logger, build_type)

        app_name = (config.get("appName") or "MonApp").strip()
        version_name = str(config.get("versionName") or "1.0")
        out_name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app_name)}_{version_name}_flutter.apk"
        min_sdk = config.get("minSdk") or FLUTTER_MIN_SDK_DEFAULT
        signing = config.get("signing", {"mode": "debug"})

        final_apk = sign_native_apk(unsigned_apk, signing, proj, out_name, min_sdk, logger)
        logger.result_file = str(final_apk)
        logger.status = "done"
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


def convert_aab_to_universal_apk(aab_path, work_dir, logger):
    """
    Convertit un App Bundle (.aab) en un APK universel autonome via
    bundletool, pour pouvoir ensuite le décompiler (jadx) ou l'importer
    dans le pipeline apktool (Mode Dev) comme un APK normal.
    Signe avec debug.keystore (identifiants standards Android) — suffisant
    puisque l'objectif est l'analyse/l'édition, pas la publication.
    """
    bundletool = find_bundletool()
    if not bundletool:
        raise RuntimeError(
            "bundletool introuvable. Installe le composant 'bundletool' depuis "
            "l'écran des composants pour traiter les fichiers .aab."
        )
    java = find_tool("java")
    if not java:
        raise RuntimeError("Java non trouvé — installe le composant 'jdk'.")

    debug_ks = TOOLS_DIR / "debug.keystore"
    if not debug_ks.exists():
        raise RuntimeError(
            "debug.keystore introuvable — relance launcher.bat pour le générer "
            "avant de traiter un .aab."
        )

    apks_out = work_dir / "bundle.apks"
    apks_out.unlink(missing_ok=True)
    logger.log("📦 Conversion App Bundle (.aab) → APK universel via bundletool...")
    cmd = [java, "-jar", bundletool, "build-apks",
           "--bundle", str(aab_path), "--output", str(apks_out),
           "--mode=universal",
           "--ks", str(debug_ks), "--ks-pass", "pass:android",
           "--ks-key-alias", "androiddebugkey", "--key-pass", "pass:android",
           "--overwrite"]
    ok = run_cmd(cmd, logger, timeout=300)
    if not ok or not apks_out.exists():
        raise RuntimeError("Échec de la conversion .aab → APK (voir les logs bundletool ci-dessus).")

    universal_apk = work_dir / "universal.apk"
    with zipfile.ZipFile(apks_out) as zf:
        candidates = [n for n in zf.namelist() if n.endswith("universal.apk")]
        if not candidates:
            raise RuntimeError("bundletool n'a produit aucun APK universel exploitable.")
        with zf.open(candidates[0]) as src, open(universal_apk, "wb") as dst:
            shutil.copyfileobj(src, dst)
    logger.log(f"✅ APK universel généré depuis le bundle : {universal_apk.name}")
    return universal_apk


def export_as_xapk(apk_path, logger, out_name=None):
    """
    Empaquette un APK déjà compilé/signé dans le format .xapk (utilisé par
    APKPure/APKMirror et compatible avec les installeurs XAPK type SAI).
    Un .xapk est un simple ZIP contenant :
      - l'APK lui-même (nom = package.apk)
      - manifest.json (métadonnées : package, version, nom, icône)
      - icon.png (si trouvable dans l'APK)
    Ce n'est PAS une reconstruction : l'APK à l'intérieur reste strictement
    l'APK d'origine, déjà signé — le .xapk n'est qu'un conteneur de
    distribution autour, donc aucune re-signature n'est nécessaire ici.
    """
    apk_path = Path(apk_path)
    if not apk_path.exists():
        raise RuntimeError(f"APK introuvable : {apk_path}")

    logger.log("📦 Extraction du package/version depuis l'APK (aapt)…")
    package_name, version_name, app_label = _read_apk_identity(apk_path, logger)

    manifest = {
        "xapk_version": 2,
        "package_name": package_name or "unknown.package",
        "name": app_label or apk_path.stem,
        "version_name": version_name or "1.0",
        "version_code": "1",
        "min_sdk_version": "21",
        "target_sdk_version": "34",
        "split_configs": [],
        "permissions": [],
    }

    out_name = out_name or (apk_path.stem + ".xapk")
    out_path = OUTPUT_DIR / out_name
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(apk_path, arcname=f"{manifest['package_name']}.apk")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        icon_bytes = _extract_apk_icon(apk_path, logger)
        if icon_bytes:
            zf.writestr("icon.png", icon_bytes)

    logger.log(f"✅ XAPK généré : {out_path.name}")
    return out_path


def _read_apk_identity(apk_path, logger):
    """Lit package/versionName/label via aapt (déjà utilisé ailleurs dans
    le pipeline signature/inspection) — renvoie (None, None, None) si aapt
    est indisponible plutôt que d'échouer tout l'export pour un détail
    cosmétique du manifest.json."""
    aapt = find_tool("aapt") or find_tool("aapt2")
    if not aapt:
        logger.log("⚠ aapt introuvable — manifest.json du XAPK restera générique")
        return None, None, None
    try:
        r = subprocess.run([aapt, "dump", "badging", str(apk_path)],
                            capture_output=True, text=True, timeout=30)
        out = r.stdout or ""
        pkg = re.search(r"package: name='([^']+)'", out)
        ver = re.search(r"versionName='([^']+)'", out)
        label = re.search(r"application-label:'([^']+)'", out)
        return (pkg.group(1) if pkg else None,
                ver.group(1) if ver else None,
                label.group(1) if label else None)
    except Exception as e:
        logger.log(f"⚠ Lecture identité APK échouée : {e}")
        return None, None, None


def _extract_apk_icon(apk_path, logger):
    """Récupère la plus grosse icône PNG mipmap/drawable trouvée dans
    l'APK pour l'inclure en icon.png du XAPK — best-effort, jamais bloquant."""
    try:
        with zipfile.ZipFile(apk_path) as zf:
            candidates = [n for n in zf.namelist()
                          if re.search(r"(mipmap|drawable).*ic_launcher.*\.png$", n)]
            if not candidates:
                return None
            candidates.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
            return zf.read(candidates[0])
    except Exception as e:
        logger.log(f"⚠ Extraction icône XAPK échouée : {e}")
        return None


def export_as_split_apks(aab_path, work_dir, logger, signing=None, out_name=None):
    """
    Génère de VRAIS Split APK (base + splits par ABI/densité/langue) à
    partir d'un App Bundle (.aab) via bundletool build-apks --mode=default,
    puis regroupe le tout dans un .zip téléchargeable.
    IMPORTANT — limite honnête : un split APK correct ne peut être dérivé
    QUE d'un .aab source (le bundle contient les métadonnées de découpage
    par configuration). Il n'existe PAS de méthode fiable pour re-découper
    un APK universel déjà fusionné après coup ; si le projet n'a produit
    qu'un .apk (apktool/scratch/cordova...), ce chemin n'est pas proposé —
    voir la vérification faite par l'appelant HTTP (/export-package).
    """
    bundletool = find_bundletool()
    if not bundletool:
        raise RuntimeError(
            "bundletool introuvable. Installe le composant 'bundletool' depuis "
            "l'écran des composants pour générer des split APK."
        )
    java = find_tool("java")
    if not java:
        raise RuntimeError("Java non trouvé — installe le composant 'jdk'.")

    work_dir = Path(work_dir)
    apks_out = work_dir / "splits.apks"
    apks_out.unlink(missing_ok=True)

    signing_mode = (signing or {}).get("mode", "debug")
    if signing_mode == "custom" and signing.get("keystoreB64"):
        ks = work_dir / "split_signing.keystore"
        ks.write_bytes(base64.b64decode(signing["keystoreB64"]))
        alias = signing.get("alias") or "key0"
        ks_pass = signing.get("storePass") or "android"
        key_pass = signing.get("keyPass") or ks_pass
    else:
        ks = TOOLS_DIR / "debug.keystore"
        if not ks.exists():
            raise RuntimeError("debug.keystore introuvable — relance launcher.bat.")
        alias, ks_pass, key_pass = "androiddebugkey", "android", "android"

    logger.log("📦 Génération des split APK (mode=default) via bundletool…")
    cmd = [java, "-jar", bundletool, "build-apks",
           "--bundle", str(aab_path), "--output", str(apks_out),
           "--mode=default",
           "--ks", str(ks), "--ks-pass", f"pass:{ks_pass}",
           "--ks-key-alias", alias, "--key-pass", f"pass:{key_pass}",
           "--overwrite"]
    ok = run_cmd(cmd, logger, timeout=300)
    if not ok or not apks_out.exists():
        raise RuntimeError("Échec de la génération des split APK (voir logs bundletool).")

    out_name = out_name or (Path(aab_path).stem + "-splits.zip")
    out_path = OUTPUT_DIR / out_name
    # Le .apks produit par bundletool EST déjà un zip contenant les splits
    # (toc.pb + splits/*.apk) — on le republie simplement sous un nom
    # explicite dans OUTPUT_DIR plutôt que de tout ré-extraire/re-zipper.
    shutil.copy(apks_out, out_path)
    logger.log(f"✅ Split APK générés : {out_path.name}")
    return out_path


def _is_aab_bytes(data):
    """Détecte un App Bundle même si le fichier a été renommé en .apk :
    structure ZIP avec BundleConfig.pb / dossiers base-*, split_*."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            return any(n == "BundleConfig.pb" or n.startswith("base/") for n in names)
    except zipfile.BadZipFile:
        return False


def _inspect_apk_before_jadx(apk_path, logger):
    """
    Vérifications préalables pour transformer les échecs jadx opaques en
    messages clairs, correspondant aux limites connues de l'outil :
    - fichier corrompu / pas un vrai ZIP
    - App Bundle (.aab) envoyé sous une extension .apk → signalé (converti
      automatiquement par l'appelant via bundletool, pas bloqué ici)
    - split APK (config split sans classes.dex — juste des ressources)
    - présence de bibliothèques natives .so (jadx les ignorera, c'est normal)
    Ne bloque QUE sur les cas réellement irrécupérables (zip invalide,
    split sans code) ; les autres cas (natif, obfuscation, .aab) sont
    juste annoncés/gérés pour que le résultat ne surprenne pas l'utilisateur.
    Retourne True si le fichier est un App Bundle.
    """
    try:
        with zipfile.ZipFile(apk_path) as zf:
            names = zf.namelist()
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"Archive corrompue (entrée illisible : {bad}).")
    except zipfile.BadZipFile:
        raise RuntimeError(
            "Ce fichier n'est pas un APK/AAB valide (ZIP corrompu ou illisible). "
            "Re-télécharge-le et réessaie."
        )

    # App Bundle (.aab) : structure différente d'un APK — pas de classes.dex
    # à la racine, mais un BundleConfig.pb et des dossiers base/*.
    is_aab = any(n == "BundleConfig.pb" or n.startswith("base/") for n in names)
    if is_aab:
        logger.log("ℹ Fichier détecté comme App Bundle (.aab) — conversion automatique en APK universel...")
        return True

    has_dex = any(n.endswith(".dex") for n in names)
    has_manifest = "AndroidManifest.xml" in names
    if has_manifest and not has_dex:
        raise RuntimeError(
            "Cet APK ne contient aucun classes.dex — c'est probablement un "
            "APK split (config split : langue, densité d'écran ou ABI), pas "
            "l'APK de base. Décompile plutôt le split 'base.apk' qui contient "
            "le code."
        )

    so_files = [n for n in names if n.endswith(".so")]
    if so_files:
        logger.log(f"ℹ {len(so_files)} bibliothèque(s) native(s) (.so) détectée(s) — "
                    "jadx ne les décompile pas, seul le code Java/Kotlin sera visible.")
    return False


def do_jadx_decompile(apk_bytes, logger):
    """
    Décompile un APK (ou un App Bundle .aab, converti automatiquement en
    APK universel via bundletool) en sources Java lisibles via jadx, puis
    zippe le résultat dans OUTPUT_DIR pour téléchargement — même schéma
    que les autres opérations (OPS token dédié 'jadx', poll via /status).
    """
    # logger déjà réservé (status="building", lines=[], result_file=None)
    # par try_reserve_op() côté handler HTTP avant le démarrage du thread.
    try:
        jadx = find_jadx()
        if not jadx:
            raise RuntimeError(
                "jadx introuvable. Installe le composant 'jadx' depuis "
                "l'écran des composants avant de décompiler."
            )
        sid = new_session_id()
        logger.session = sid
        work = WORK_DIR / sid / "jadx_in"
        out_dir = WORK_DIR / sid / "jadx_out"
        work.mkdir(parents=True, exist_ok=True)
        apk_path = work / "input.apk"
        apk_path.write_bytes(apk_bytes)

        logger.log("🔍 Vérification de l'APK/AAB avant décompilation...")
        is_aab = _inspect_apk_before_jadx(apk_path, logger)
        if is_aab:
            apk_path = convert_aab_to_universal_apk(apk_path, work, logger)

        logger.log("🔎 Décompilation avec jadx (peut prendre plusieurs minutes sur de gros APK)...")
        cmd = [jadx, "-d", str(out_dir), "--show-bad-code", str(apk_path)]
        logger.log(f"$ {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=900)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Timeout jadx (15 min dépassées) — APK trop volumineux.")
        for line in (r.stdout or "").strip().split("\n"):
            if line.strip(): logger.log(line)
        for line in (r.stderr or "").strip().split("\n"):
            if line.strip(): logger.log("⚠ " + line)
        # jadx retourne parfois un code != 0 même quand une partie du code a
        # été décompilée avec succès (classes obfusquées/corrompues) — on ne
        # bloque donc que si AUCUNE source n'a été produite.
        if not out_dir.exists() or not any(out_dir.rglob("*.java")):
            raise RuntimeError(
                "jadx n'a produit aucune source Java lisible. Cause probable : "
                "protection/obfuscation forte (ProGuard agressif, R8, packer "
                "commercial type DexGuard) qui empêche toute reconstruction du code."
            )

        out_name = f"{apk_path.stem}_jadx_sources.zip"
        final_zip = OUTPUT_DIR / out_name
        if final_zip.exists():
            final_zip.unlink()
        with zipfile.ZipFile(final_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in out_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(out_dir))

        logger.result_file = str(final_zip)
        logger.status = "done"
        logger.log(f"✅ Sources Java prêtes : {final_zip.name}")
    except Exception as e:
        import traceback
        logger.log(f"❌ Erreur: {e}")
        logger.log(traceback.format_exc())
        logger.status = "error"


# =============================================================
# PIPELINE COMPLET — BUILD DEPUIS ZÉRO
# =============================================================
def do_build_scratch(config, icon_bytes, splash_bytes, site_zip_bytes):
    """Build APK sans template (mode scratch)."""
    logger = OPS["legacy"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
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
    logger = OPS["legacy"]  # déjà réservé (status="building") par try_reserve_op() côté handler HTTP
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
                low = filename.lower()
                if low.endswith(".zip"):
                    content_type = "application/zip"
                elif low.endswith(".pdf"):
                    content_type = "application/pdf"
                else:
                    content_type = "application/vnd.android.package-archive"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
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
                "customKeystoreExists": _find_custom_keystore() is not None,
                "customKeystoreName":   _find_custom_keystore(),
                "buildToolsVersion":    bt,
                "sdkPresent":           SDK_DIR.exists(),
                "scratchMode":          True,
                "adb":                  bool(adb_path),
                "adbDevices":           adb_devices,
                "gradle":               bool(find_gradle()),
                "jadx":                 bool(find_jadx()),
                "bundletool":           bool(find_bundletool()),
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

        # ── /entrypoint : LA réponse faisant autorité sur "quel est le bon
        # chemin" pour le point d'entrée web/natif d'une session — appelée
        # par l'agent IA avant d'écrire index.html/style.css/app.js, au lieu
        # qu'il redevine lui-même le dossier depuis l'arborescence. Ne fait
        # AUCUNE écriture destructive au-delà du nettoyage habituel des 4
        # noms de fichiers webview connus (voir enforce_project_entrypoint).
        if path == "/entrypoint":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._err("Aucun projet chargé", 404); return
            try:
                class _NullLogger:
                    def log(self, *_a, **_k): pass
                report = enforce_project_entrypoint(sid, kind_hint=None, logger=_NullLogger())
                self._json(report)
            except Exception as e:
                self._err(str(e), 500)
            return

        if path == "/session-identity":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._json({"identity": None}); return
            try:
                meta_f = session_dir(sid) / "session.json"
                meta = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {}
                identity = meta.get("identity")
                icon_b64 = None
                icon_mime = None
                if identity and identity.get("iconRelPath"):
                    try:
                        raw = read_binary_raw(sid, identity["iconRelPath"])
                        icon_b64 = base64.b64encode(raw).decode()
                        icon_mime = "image/png"
                    except Exception:
                        pass
                self._json({"identity": identity, "iconBase64": icon_b64, "iconMime": icon_mime})
            except Exception as e:
                self._err(e, 500)
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

        # Vrai squelette de dossiers/sous-dossiers pour un type d'APK donné,
        # lu directement sur le disque (templates/<type>/...) — fonctionne
        # SANS session active, SANS avoir jamais ouvert l'IA. Utilisé par
        # l'explorateur de gauche pour montrer où l'IA injectera ses
        # fichiers avant même la création d'un premier projet.
        if path == "/template-tree":
            apk_type = qs.get("type", [""])[0]
            try:
                data = list_template_tree(apk_type)
                data["mountPrefix"] = TEMPLATE_MOUNT_PREFIX.get(apk_type)
                self._json(data)
            except Exception as e:
                self._err(e, 500)
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
                        # Édition hexadécimale : tout fichier binaire (compilé AXML, .dex,
                        # keystore, smali patché en brut, etc.) sous la limite de taille
                        # est renvoyé en base64 pour permettre l'édition octet par octet.
                        size = data.get("size", 0)
                        if size <= HEX_EDIT_MAX_SIZE:
                            try:
                                raw = read_binary_raw(sid, rel)
                                b64 = base64.b64encode(raw).decode()
                                resp = {"type": "binary", "reason": data.get("reason", "binary"),
                                        "size": size, "content": b64, "hexEditable": True}
                                # Si c'est un XML binaire compilé (AXML), on propose en plus
                                # l'édition SÛRE du string pool (pas de risque de corruption :
                                # voir read_axml_strings / rewrite_axml_strings).
                                if is_axml(raw):
                                    try:
                                        axml = read_axml_strings(raw)
                                        resp["axml"] = True
                                        resp["axmlStrings"] = axml["strings"]
                                        resp["axmlUtf8"] = axml["utf8"]
                                    except AxmlError:
                                        resp["axml"] = False
                                self._json(resp)
                            except Exception:
                                self._json({"type": "binary", "reason": data.get("reason", "binary"),
                                            "size": size, "hexEditable": False})
                        else:
                            self._json({"type": "binary", "reason": data.get("reason", "binary"),
                                        "size": size, "hexEditable": False})
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

        # ── /download-file : téléchargement GÉNÉRIQUE d'un fichier d'une
        # session (PDF, image, zip, texte...) depuis le chat IA — contrairement
        # à /download (réservé aux APK/ZIP déjà déposés dans OUTPUT_DIR), cet
        # endpoint sert n'importe quel fichier produit dans une session via
        # write_file / write_file_safe, avec le bon Content-Type déduit de
        # l'extension, et force le téléchargement (Content-Disposition).
        if path == "/download-file":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            rel = qs.get("path", [""])[0]
            if not sid or not rel:
                self._err("Paramètres 'session' et 'path' requis", 400); return
            try:
                data = read_binary_raw(sid, rel)
                filename = os.path.basename(rel) or "fichier.bin"
                ext = os.path.splitext(filename)[1].lower()
                mime_map = {
                    ".pdf":  "application/pdf",
                    ".zip":  "application/zip",
                    ".apk":  "application/vnd.android.package-archive",
                    ".png":  "image/png",
                    ".jpg":  "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                    ".gif":  "image/gif",
                    ".svg":  "image/svg+xml",
                    ".txt":  "text/plain; charset=utf-8",
                    ".json": "application/json; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                    ".csv":  "text/csv; charset=utf-8",
                }
                content_type = mime_map.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_cors(); self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self._err("Fichier introuvable dans la session", 404)
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

        # ── /search-content : recherche texte sur TOUT le projet (control total) ──
        # Contrairement à /tree qui ne filtre que les noms de fichiers, cet endpoint
        # cherche dans le CONTENU de chaque fichier texte (smali, xml, json, etc.)
        # et retourne fichier + numéro de ligne + extrait, pour naviguer comme un IDE.
        if path == "/search-content":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            query = qs.get("q", [""])[0]
            use_regex = qs.get("regex", ["0"])[0] == "1"
            case_sensitive = qs.get("case", ["0"])[0] == "1"
            if not sid: self._err("Aucun projet chargé", 404); return
            try:
                result = search_content(sid, query, use_regex=use_regex, case_sensitive=case_sensitive)
                self._json(result)
            except Exception as e:
                self._err(e, 400)
            return

        # ── /detect-apk-type : espace de travail universel — détection
        # déterministe (empreintes de fichiers, pas d'IA) du framework
        # d'origine d'un APK décompilé + liste des outils requis pour ce
        # type précis. L'IA prend ensuite le relais côté client pour
        # vérifier ce qui manque réellement (check_missing_components) et
        # chercher ce qui n'est pas dans le registre (search_missing_component).
        if path == "/detect-apk-type":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._err("Aucun projet chargé", 404); return
            try:
                self._json(detect_decompiled_apk_type(sid))
            except Exception as e:
                self._err(e, 500)
            return

        # ── /smali-facts : scan guidé (couleurs, textes, durées, URLs) ──────
        # Permet à un utilisateur ne connaissant pas la syntaxe smali de voir
        # et modifier les valeurs courantes via une interface simple.
        if path == "/smali-facts":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid: self._err("Aucun projet chargé", 404); return
            try:
                facts = scan_smali_facts(sid)
                self._json({"facts": facts, "count": len(facts)})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /ui-facts : « Mode Profondeur » — TOUT le texte/boutons affichés
        # dans l'APK (strings.xml + layouts + textes smali bruts), en une
        # seule liste visuelle groupée par type, éditable/supprimable sans
        # connaître XML ni smali.
        if path == "/ui-facts":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid: self._err("Aucun projet chargé", 404); return
            try:
                facts = scan_ui_facts(sid)
                self._json({"facts": facts, "count": len(facts)})
            except Exception as e:
                self._err(e, 500)
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

        # ── /entrypoint : rapport EN DIRECT du dossier racine web réellement
        # actif pour cette session (assets/, www/, assets/www/, .../www/, ou
        # aucun pour flutter-natif/native/twa), + le chemin exact du point
        # d'entrée. Read-only côté résultat affiché : réutilise le même code
        # que celui appelé avant chaque build (enforce_project_entrypoint),
        # donc CE endpoint fait autorité — c'est lui que builder.html appelle
        # pour afficher un chemin garanti exact dans le panneau "espace de
        # travail IA", au lieu d'un chemin statique deviné côté client.
        if path == "/entrypoint":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._err("Aucun projet chargé", 404); return
            try:
                report = enforce_project_entrypoint(sid)
                self._json(report)
            except Exception as e:
                self._err(e, 500)
            return

        # ── /agent-overview : vue d'ensemble condensée pour l'assistant IA ──
        # Utilisé par le mode Agent (agent-engine.js) comme premier appel
        # systématique sur un projet, pour éviter qu'il "explore à l'aveugle"
        # avec des dizaines d'allers-retours /tree + /file.
        if path == "/agent-overview":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._err("Aucun projet chargé", 404); return
            try:
                self._json(build_project_overview(sid))
            except Exception as e:
                self._err(e, 500)
            return

        # ── /export-zip : télécharge TOUT le projet en cours, y compris
        # l'historique complet des fichiers générés/modifiés pendant la
        # session (pas seulement les fichiers de la dernière réponse IA).
        # Structure du ZIP :
        #   project/   → état actuel des fichiers éditables
        #   history/<horodatage>__.../  → chaque écriture successive
        if path == "/export-zip":
            sid = qs.get("session", [""])[0] or CURRENT_SESSION
            if not sid:
                self._err("Aucun projet chargé", 404); return
            try:
                sd = session_dir(sid)
                if not sd.exists():
                    self._err("Session introuvable", 404); return
                root = resolve_session_root(sid)
                hist_dir = sd / ".history"
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    if root.exists():
                        for f in root.rglob("*"):
                            if f.is_file():
                                zf.write(f, arcname=str(Path("project") / f.relative_to(root)))
                    if hist_dir.exists():
                        for f in hist_dir.rglob("*"):
                            if f.is_file():
                                zf.write(f, arcname=str(Path("history") / f.relative_to(hist_dir)))
                    meta_f = sd / "session.json"
                    if meta_f.exists():
                        zf.write(meta_f, arcname="session.json")
                buf.seek(0)
                data = buf.getvalue()
                fname = f"projet-{sid}.zip"
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_cors(); self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._err(str(e), 500)
            return

        self.send_response(404); self.end_headers()

    def do_DELETE(self):
        global CURRENT_SESSION
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/session":
            sid = qs.get("session", [""])[0]
            if not sid:
                self._err("Paramètre 'session' manquant", 400); return
            try:
                delete_session(sid)
            except RuntimeError as e:
                # Session en cours de compilation — refus propre (409),
                # jamais de suppression forcée qui casserait le build actif.
                self._err(str(e), 409); return
            # BUG CORRIGÉ : `OPS.pop(sid, None)` ne faisait jamais rien, car
            # OPS est indexé par NOM DE MODE ("legacy", "native", "twa"...),
            # jamais par id de session — sid ne correspond à aucune de ces
            # clés. Ici on retrouve plutôt le(s) logger(s) qui pointaient sur
            # cette session (op.session == sid) et on les réinitialise à
            # l'état neutre, pour qu'un /status ultérieur sur ce token
            # n'affiche plus une session qui n'existe plus sur disque.
            for op in OPS.values():
                if op.session == sid:
                    op.session = None
                    op.result_file = None
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

        # ── /replace-line : remplace le contenu d'une ligne précise d'un fichier ──
        # Utilisé par la recherche texte globale pour un "rechercher/remplacer"
        # contrôlé : on vérifie l'ancien contenu avant d'écrire pour éviter
        # d'écraser une ligne modifiée entre-temps par autre chose.
        # ── /export-package : convertit un APK/AAB déjà buildé (présent
        # dans OUTPUT_DIR) vers un format d'export supplémentaire (xapk,
        # split). Contrairement aux endpoints /build-*, c'est une simple
        # opération de packaging sur un fichier existant (quelques
        # secondes) — traitée en synchrone, sans passer par le système de
        # polling token/status utilisé pour les vrais builds. ──────────
        if path == "/export-package":
            payload = self._read_json_body()
            src_name = payload.get("file", "")
            fmt = (payload.get("format") or "").lower()
            signing = payload.get("signing") or {"mode": "debug"}
            if not src_name or fmt not in ("xapk", "split"):
                self._err("Paramètres invalides : 'file' requis, 'format' doit être 'xapk' ou 'split'.", 400); return
            src_path = OUTPUT_DIR / src_name
            if not src_path.exists() or OUTPUT_DIR not in src_path.resolve().parents:
                self._err("Fichier source introuvable dans output/.", 404); return
            logger = OpLogger()
            try:
                if fmt == "xapk":
                    if src_path.suffix.lower() != ".apk":
                        self._err("Export XAPK : le fichier source doit être un .apk déjà compilé/signé.", 400); return
                    out_path = export_as_xapk(src_path, logger)
                else:  # split
                    if src_path.suffix.lower() != ".aab":
                        self._err(
                            "Export Split APK : nécessite un vrai .aab en source — un .apk déjà "
                            "fusionné (apktool/scratch/cordova/natif classique) ne peut pas être "
                            "redécoupé après coup. Compile d'abord en sortie .aab si ton mode de "
                            "build le permet.", 400); return
                    work_dir = OUTPUT_DIR / f"_tmp_split_{int(time.time())}"
                    work_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        out_path = export_as_split_apks(src_path, work_dir, logger, signing=signing)
                    finally:
                        shutil.rmtree(work_dir, ignore_errors=True)
                self._json({"started": True, "done": True, "file": out_path.name, "logsTail": "\n".join(logger.lines[-50:])})
            except Exception as e:
                self._err(str(e), 500)
            return

        if path == "/replace-line":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            rel = payload.get("path")
            line_no = payload.get("line")
            old_text = payload.get("oldText")
            new_text = payload.get("newText", "")
            if not sid or not rel or not line_no:
                self._err("Paramètres manquants (session, path, line)", 400); return
            try:
                replace_in_file_line(sid, rel, int(line_no), old_text, new_text)
                self._json({"updated": True, "path": rel, "line": line_no})
            except Exception as e:
                self._err(e, 400)
            return

        # ── /force-delete-with-deps : suppression FORCÉE et déterministe,
        # sans passer par l'IA (contrairement à delete_path côté agent qui
        # peut se tromper ou annoncer une suppression jamais faite). Supprime
        # le fichier pour de vrai, scanne tout le projet pour les dépendances,
        # nettoie automatiquement le motif smali sûr reconnu, et liste le
        # reste pour revue.
        if path == "/force-delete-with-deps":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            rel = payload.get("path")
            if not sid or not rel:
                self._err("Paramètres manquants (session, path)", 400); return
            try:
                result = force_delete_with_refs(sid, rel)
                self._json(result)
            except FileNotFoundError as e:
                self._err(str(e) or "Fichier introuvable", 404)
            except Exception as e:
                self._err(e, 500)
            return

        # ── /smali-apply : applique l'édition guidée d'une valeur smali ─────
        # (couleur, texte, durée, URL) détectée par /smali-facts, sans que
        # l'utilisateur ait besoin d'éditer le smali directement.
        if path == "/smali-apply":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            edits = payload.get("edits")
            if not edits and payload.get("id") is not None:
                edits = [{"id": payload.get("id"), "value": payload.get("value")}]
            if not sid or not edits:
                self._err("Paramètres manquants (session, edits)", 400); return
            results = []
            for edit in edits:
                fid = edit.get("id")
                val = edit.get("value", "")
                try:
                    apply_smali_fact(sid, fid, val)
                    results.append({"id": fid, "ok": True})
                except Exception as e:
                    results.append({"id": fid, "ok": False, "error": str(e)})
            self._json({"results": results})
            return

        # ── /ui-apply : applique l'édition/suppression d'un texte ou bouton
        # détecté par /ui-facts (Mode Profondeur). "Supprimer" = vider le
        # texte (chaîne vide) plutôt que retirer la ligne entière : ça évite
        # de casser une référence @string/xxx utilisée ailleurs (manifest,
        # autre layout...) ou de décaler les lignes suivantes d'un fichier
        # encore ouvert côté client.
        if path == "/ui-apply":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            edits = payload.get("edits")
            if not edits and payload.get("id") is not None:
                edits = [{"id": payload.get("id"), "value": payload.get("value", "")}]
            if not sid or not edits:
                self._err("Paramètres manquants (session, edits)", 400); return
            results = []
            for edit in edits:
                fid = edit.get("id")
                val = edit.get("value", "")
                try:
                    apply_ui_fact(sid, fid, val)
                    results.append({"id": fid, "ok": True})
                except Exception as e:
                    results.append({"id": fid, "ok": False, "error": str(e)})
            self._json({"results": results})
            return

        # ── /ui-disable-block : va plus loin que /ui-apply — au lieu de
        # vider seulement le texte, neutralise TOUTE la méthode smali qui
        # affiche l'élément (dialogue/bandeau entier : fond, texte, boutons).
        if path == "/ui-disable-block":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            fid = payload.get("id")
            if not sid or not fid:
                self._err("Paramètres manquants (session, id)", 400); return
            try:
                result = disable_ui_fact_block(sid, fid)
                self._json({"ok": True, **result})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        # ── /ui-disable-all-popups : bouton dev — scanne TOUT le projet et
        # neutralise d'un coup chaque popup/dialogue codé en dur (même
        # mécanisme que /ui-disable-block, appliqué à toutes les occurrences
        # trouvées). Ne touche jamais une méthode non-void : listée à part.
        if path == "/ui-disable-all-popups":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            force = bool(payload.get("force"))
            if not sid:
                self._err("Paramètre manquant (session)", 400); return
            try:
                results = disable_all_popup_methods(sid, force=force)
                counts = {}
                for r in results:
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
                print(f"[popups] Résumé session {sid} (force={force}) : {counts}")
                self._json({"ok": True, "results": results, "counts": counts, "force": force})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        # ── /build-scratch : génère APK de ZÉRO, sans template ──────────────
        if path == "/build-scratch":
            op = try_reserve_op("legacy")
            if op is None:
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

        # ── /build-native : génère + compile une app NATIVE (Kotlin/Gradle) ──
        # depuis zéro, indépendamment du pipeline WebView/smali ci-dessus.
        if path == "/build-native":
            op = try_reserve_op("native")
            if op is None:
                self._err("Build natif déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_native,
                args=(config, icon_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "native"})
            return

        # ── /build-twa : enveloppe un site existant via bubblewrap (TWA) ────
        if path == "/build-twa":
            op = try_reserve_op("twa")
            if op is None:
                self._err("Build TWA déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_twa,
                args=(config, icon_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "twa"})
            return

        # ── /cordova-generate : génère le projet Cordova SANS compiler,
        # pour permettre l'édition dans l'explorer avant un /build-cordova
        # ultérieur avec {"session": sid} ────────────────────────────────
        # ── NativeScript / MAUI / Titanium — pipelines BETA (voir
        # commentaire en tête de section dans server.py). Même schéma que
        # cordova-generate/build-cordova : générer sans compiler, puis
        # compiler séparément (ou compiler direct sans session existante). ──
        if path == "/nativescript-generate":
            op = try_reserve_op("nativescript_gen")
            if op is None:
                self._err("Génération NativeScript déjà en cours", 409); return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
                site_zip_bytes = _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            def _run():
                sid = new_session_id()
                op.session = sid
                try:
                    generate_nativescript_project(sid, config, icon_bytes, site_zip_bytes, op)
                    op.status = "done"
                except Exception as e:
                    op.log(f"❌ Erreur: {e}")
                    op.status = "error"
            threading.Thread(target=_run, daemon=True).start()
            self._json({"started": True, "session": "nativescript_gen"})
            return

        if path == "/build-nativescript":
            op = try_reserve_op("nativescript")
            if op is None:
                self._err("Build NativeScript déjà en cours", 409); return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
                site_zip_bytes = _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(target=do_build_nativescript, args=(config, icon_bytes, None, site_zip_bytes), daemon=True).start()
            self._json({"started": True, "session": "nativescript"})
            return

        if path == "/maui-generate":
            op = try_reserve_op("maui_gen")
            if op is None:
                self._err("Génération MAUI déjà en cours", 409); return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
                site_zip_bytes = _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            def _run():
                sid = new_session_id()
                op.session = sid
                try:
                    generate_maui_project(sid, config, icon_bytes, site_zip_bytes, op)
                    op.status = "done"
                except Exception as e:
                    op.log(f"❌ Erreur: {e}")
                    op.status = "error"
            threading.Thread(target=_run, daemon=True).start()
            self._json({"started": True, "session": "maui_gen"})
            return

        if path == "/build-maui":
            op = try_reserve_op("maui")
            if op is None:
                self._err("Build MAUI déjà en cours", 409); return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
                site_zip_bytes = _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(target=do_build_maui, args=(config, icon_bytes, None, site_zip_bytes), daemon=True).start()
            self._json({"started": True, "session": "maui"})
            return

        if path == "/titanium-generate":
            op = try_reserve_op("titanium_gen")
            if op is None:
                self._err("Génération Titanium déjà en cours", 409); return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
                site_zip_bytes = _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            def _run():
                sid = new_session_id()
                op.session = sid
                try:
                    generate_titanium_project(sid, config, icon_bytes, site_zip_bytes, op)
                    op.status = "done"
                except Exception as e:
                    op.log(f"❌ Erreur: {e}")
                    op.status = "error"
            threading.Thread(target=_run, daemon=True).start()
            self._json({"started": True, "session": "titanium_gen"})
            return

        if path == "/build-titanium":
            op = try_reserve_op("titanium")
            if op is None:
                self._err("Build Titanium déjà en cours", 409); return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            try:
                icon_bytes = _safe_b64(payload.get("icon"), "icon")
                site_zip_bytes = _safe_b64(payload.get("siteZip"), "siteZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(target=do_build_titanium, args=(config, icon_bytes, None, site_zip_bytes), daemon=True).start()
            self._json({"started": True, "session": "titanium"})
            return

        if path == "/cordova-generate":
            op = try_reserve_op("cordova_gen")
            if op is None:
                self._err("Génération Cordova déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes        = _safe_b64(payload.get("icon"), "icon")
                splash_bytes      = _safe_b64(payload.get("splash"), "splash")
                site_zip_bytes    = _safe_b64(payload.get("siteZip"), "siteZip")
                project_zip_bytes = _safe_b64(payload.get("projectZip"), "projectZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_generate_cordova_session,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes, project_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "cordova_gen"})
            return

        # ── /flutter-generate : génère le projet Flutter SANS compiler ──
        # (url / scratch / template — voir config.sourceMode)
        if path == "/flutter-generate":
            op = try_reserve_op("flutter_gen")
            if op is None:
                self._err("Génération Flutter déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes        = _safe_b64(payload.get("icon"), "icon")
                splash_bytes      = _safe_b64(payload.get("splash"), "splash")
                site_zip_bytes    = _safe_b64(payload.get("siteZip"), "siteZip")
                project_zip_bytes = _safe_b64(payload.get("projectZip"), "projectZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_generate_flutter_session,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes, project_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "flutter_gen"})
            return

        # ── /rn-generate : génère le projet React Native SANS compiler ──
        # (url / scratch / template — voir config.sourceMode)
        if path == "/rn-generate":
            op = try_reserve_op("rn_gen")
            if op is None:
                self._err("Génération React Native déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes        = _safe_b64(payload.get("icon"), "icon")
                splash_bytes      = _safe_b64(payload.get("splash"), "splash")
                site_zip_bytes    = _safe_b64(payload.get("siteZip"), "siteZip")
                project_zip_bytes = _safe_b64(payload.get("projectZip"), "projectZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_generate_rn_session,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes, project_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "rn_gen"})
            return

        # ── /build-cordova : enveloppe un site dans un vrai projet Cordova ──
        # (url / scratch / template — voir config.sourceMode)
        if path == "/build-cordova":
            op = try_reserve_op("cordova")
            if op is None:
                self._err("Build Cordova déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes        = _safe_b64(payload.get("icon"), "icon")
                splash_bytes      = _safe_b64(payload.get("splash"), "splash")
                site_zip_bytes    = _safe_b64(payload.get("siteZip"), "siteZip")
                project_zip_bytes = _safe_b64(payload.get("projectZip"), "projectZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_cordova,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes, project_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "cordova"})
            return

        # ── /build-flutter : enveloppe un site dans un vrai projet Flutter ──
        # (url / scratch / template — voir config.sourceMode)
        if path == "/build-flutter":
            op = try_reserve_op("flutter")
            if op is None:
                self._err("Build Flutter déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes        = _safe_b64(payload.get("icon"), "icon")
                splash_bytes      = _safe_b64(payload.get("splash"), "splash")
                site_zip_bytes    = _safe_b64(payload.get("siteZip"), "siteZip")
                project_zip_bytes = _safe_b64(payload.get("projectZip"), "projectZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_flutter,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes, project_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "flutter"})
            return

        # ── /build-rn : enveloppe un site dans un vrai projet React Native ──
        # (url / scratch / template — voir config.sourceMode)
        if path == "/build-rn":
            op = try_reserve_op("rn")
            if op is None:
                self._err("Build React Native déjà en cours", 409); return
            payload = self._read_json_body()
            config  = payload.get("config", payload)
            try:
                icon_bytes        = _safe_b64(payload.get("icon"), "icon")
                splash_bytes      = _safe_b64(payload.get("splash"), "splash")
                site_zip_bytes    = _safe_b64(payload.get("siteZip"), "siteZip")
                project_zip_bytes = _safe_b64(payload.get("projectZip"), "projectZip")
            except ValueError as e:
                self._err(str(e), 400); return
            threading.Thread(
                target=do_build_react_native,
                args=(config, icon_bytes, splash_bytes, site_zip_bytes, project_zip_bytes),
                daemon=True
            ).start()
            self._json({"started": True, "session": "rn"})
            return

        # ── /jadx-decompile : décompile un APK uploadé en sources Java ──────
        # lisibles (jadx), indépendant des autres pipelines (token OPS 'jadx').
        if path == "/jadx-decompile":
            op = try_reserve_op("jadx")
            if op is None:
                self._err("Décompilation jadx déjà en cours", 409); return
            payload = self._read_json_body()
            apk_b64 = payload.get("apk")
            try:
                apk_bytes = _safe_b64(apk_b64, "apk")
            except ValueError as e:
                self._err(str(e), 400); return
            if not apk_bytes:
                self._err("Aucun APK fourni", 400); return
            threading.Thread(
                target=do_jadx_decompile,
                args=(apk_bytes, op),
                daemon=True
            ).start()
            self._json({"started": True, "session": "jadx"})
            return

        # ── /build : génère APK depuis template OU from scratch ─────────────
        if path == "/build":
            op = try_reserve_op("legacy")
            if op is None:
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
            op = try_reserve_op("legacy")
            if op is None:
                self._err("Décompilation déjà en cours", 409); return
            payload    = self._read_json_body()
            apk_b64    = payload.get("apk") or payload.get("templateApk")
            tmpl_bytes = base64.b64decode(apk_b64) if apk_b64 else None
            op.session = None

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
            op = try_reserve_op("legacy")
            if op is None:
                self._err("Opération déjà en cours", 409); return
            payload = self._read_json_body()
            config        = payload.get("config", payload)
            icon_bytes    = base64.b64decode(payload["icon"])    if payload.get("icon")    else None
            splash_bytes  = base64.b64decode(payload["splash"])  if payload.get("splash")  else None
            site_zip_bytes= base64.b64decode(payload["siteZip"]) if payload.get("siteZip") else None
            op.session = None

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
            op = try_reserve_op("legacy")
            if op is None:
                self._err("Opération déjà en cours", 409); return
            payload = self._read_json_body()
            config        = payload.get("config", payload)
            icon_bytes    = base64.b64decode(payload["icon"])    if payload.get("icon")    else None
            splash_bytes  = base64.b64decode(payload["splash"])  if payload.get("splash")  else None
            site_zip_bytes= base64.b64decode(payload["siteZip"]) if payload.get("siteZip") else None
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
            op = try_reserve_op("legacy")
            if op is None:
                self._err("Opération déjà en cours", 409); return
            payload = self._read_json_body()
            signing  = payload.get("signing",  {"mode": "debug"})
            out_name = payload.get("outName",  "output.apk")
            sid_snap = CURRENT_SESSION

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
                # Filet de sécurité temps réel : si ce fichier vient d'être
                # écrit dans le mauvais dossier racine pour le type réel de
                # CETTE session (ex: assets/app.js dans une session cordova
                # dont la racine active est www/), enforce_project_entrypoint
                # le détecte et nettoie tout de suite, au lieu d'attendre le
                # prochain build. On ne bloque jamais la réponse à cause de
                # ce nettoyage : un souci ici reste non-bloquant et n'empêche
                # pas la confirmation d'écriture.
                try:
                    entry_report = enforce_project_entrypoint(sid, kind_hint=_session_origin(sid) or None)
                except Exception:
                    entry_report = None
                resp = {"saved": rel}
                if entry_report: resp["entrypoint"] = entry_report
                self._json(resp)
            except Exception as e:
                self._err(e, 500)
            return

        # ── /cleanup-mismatched-files : répare une session déjà polluée par
        # le bug "fichiers d'un autre framework écrits dans cette session"
        # (ex: pubspec.yaml/*.dart écrits dans une session scratch avant
        # que le garde-fou write_file_safe ne couvre ce cas). Supprime
        # UNIQUEMENT les fichiers marqueurs d'un AUTRE framework que
        # l'origin réel de la session — jamais les fichiers du bon type.
        if path == "/cleanup-mismatched-files":
            payload = self._read_json_body()
            sid = payload.get("session") or CURRENT_SESSION
            if not sid: self._err("Aucun projet chargé", 400); return
            try:
                removed = cleanup_mismatched_framework_files(sid)
                self._json({"session": sid, "removed": removed, "count": len(removed)})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /save-file-binary : édition hexadécimale (octets bruts, base64) ─────
        if path == "/save-file-binary":
            payload  = self._read_json_body()
            sid      = payload.get("session") or CURRENT_SESSION
            rel      = payload.get("path", "")
            data_b64 = payload.get("data", "")
            if not sid: self._err("Aucun projet chargé", 400); return
            try:
                raw = base64.b64decode(data_b64)
                write_binary_safe(sid, rel, raw)
                self._json({"saved": rel, "size": len(raw)})
            except Exception as e:
                self._err(e, 500)
            return

        # ── /axml-strings : édition SÛRE du string pool d'un XML binaire compilé ─
        # Ne change que le texte des chaînes, jamais leur nombre/ordre → aucun
        # risque de corrompre les références d'index ailleurs dans l'arbre.
        if path == "/axml-strings":
            payload = self._read_json_body()
            sid     = payload.get("session") or CURRENT_SESSION
            rel     = payload.get("path", "")
            strings = payload.get("strings")
            if not sid: self._err("Aucun projet chargé", 400); return
            if not isinstance(strings, list): self._err("Paramètre 'strings' manquant ou invalide", 400); return
            try:
                raw = read_binary_raw(sid, rel)
                new_raw = rewrite_axml_strings(raw, strings)
                write_binary_safe(sid, rel, new_raw)
                self._json({"saved": rel, "size": len(new_raw), "stringCount": len(strings)})
            except AxmlError as e:
                self._err(str(e), 400)
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
            # BUG-mem01 — auparavant : get_op("keystore-"+new_session_id()) créait
            # une PREMIÈRE entrée OPS sous un id jamais utilisé/interrogé, puis un
            # second id différent était généré plus bas pour le token réellement
            # renvoyé au client. La première entrée restait orpheline dans OPS
            # pour toujours (fuite mémoire, un peu plus à chaque création de
            # keystore). On génère maintenant un seul id, utilisé pour les deux.
            token        = f"keystore-{new_session_id()}"
            logger       = get_op(token)
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
            # BUG-mem01 — même correction que /create-keystore : un seul token
            # généré (au lieu de deux), pour ne plus laisser d'entrée OPS
            # orpheline en mémoire à chaque signature.
            sign_token = f"sign-{new_session_id()}"
            logger = get_op(sign_token)
            logger.status = "building"
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
                            import zipfile as _zf
                            sdk_urls = [
                                "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip",
                                "https://dl.google.com/android/repository/commandlinetools-win-10406996_latest.zip",
                            ]
                            sdk_zip = TOOLS_DIR / "cmdline-tools.zip"
                            logger.log("📥 Téléchargement SDK Android command-line tools (~100 Mo)...")
                            _download_with_fallback(sdk_urls, sdk_zip, logger)
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
            payload     = self._read_json_body()
            sid         = payload.get("session") or CURRENT_SESSION
            rel         = (payload.get("path") or "").strip("/\\")
            new_rel     = (payload.get("newPath") or "").strip("/\\")
            update_refs = bool(payload.get("updateRefs"))
            if not sid or not rel or not new_rel: self._err("Paramètres manquants", 400); return
            try:
                if update_refs:
                    result = rename_with_refs(sid, rel, new_rel)
                    self._json({
                        "renamed": new_rel,
                        "refsUpdated": result["refsUpdated"],
                        "nameAmbiguous": result["nameAmbiguous"],
                    })
                else:
                    sdir, src_p = _safe_target(sid, rel)
                    _, dst_p    = _safe_target(sid, new_rel)
                    if not src_p.exists(): self._err("Source introuvable", 404); return
                    if dst_p.exists(): self._err("Destination déjà existante", 409); return
                    dst_p.parent.mkdir(parents=True, exist_ok=True)
                    src_p.rename(dst_p)
                    self._json({"renamed": new_rel})
            except FileNotFoundError as e:
                self._err(e, 404)
            except FileExistsError as e:
                self._err(e, 409)
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
