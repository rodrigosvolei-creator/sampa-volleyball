import json
import os
import uuid
import base64
import random
import hashlib
import shutil
import string
try:
    import fcntl  # POSIX (Linux/produção). Ausente no Windows/dev local.
    _HAS_FCNTL = True
except ImportError:
    fcntl = None
    _HAS_FCNTL = False
import secrets
import logging
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, make_response, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image, ImageDraw, ImageFont

# ---------- App setup ----------
app = Flask(__name__, static_folder='static')

# Behind a single reverse proxy (Coolify). Trust X-Forwarded-* exactly one hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Secret key for session cookies. Persisted to disk so sessions survive restarts.
DATA_DIR = os.environ.get('DATA_DIR', '/data')
SECRET_KEY_FILE = os.path.join(DATA_DIR, '.secret_key')

def _load_or_create_secret_key():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'rb') as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(SECRET_KEY_FILE, 'wb') as f:
        f.write(key)
    os.chmod(SECRET_KEY_FILE, 0o600)
    return key

app.secret_key = os.environ.get('FLASK_SECRET_KEY', '').encode() or _load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '1') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,  # 15MB max upload
)

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('svl')

# ---------- Paths ----------
DATA_FILE = os.path.join(DATA_DIR, 'tournament.json')
DATA_LOCK = os.path.join(DATA_DIR, '.tournament.lock')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
UPLOADS_DIR = os.path.join(DATA_DIR, 'uploads')
ORIGINALS_DIR = os.path.join(DATA_DIR, 'originals')

ALLOWED_IMAGE_EXT = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
ALLOWED_COMPROVANTE_EXT = ALLOWED_IMAGE_EXT | {'pdf'}

# ---------- Watermark ----------
def apply_watermark(img_path, output_path):
    """Apply SVL watermark to an image. Saves original separately."""
    logo_path = os.path.join(app.static_folder, 'logo.jpeg')
    try:
        img = Image.open(img_path).convert("RGBA")
        max_w = 1400
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)), Image.LANCZOS)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        if os.path.exists(logo_path):
            logo = Image.open(logo_path).convert("RGBA")
            logo_size = int(min(img.width, img.height) * 0.12)
            logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
            logo_alpha = logo.copy()
            alpha = logo_alpha.split()[3]
            alpha = alpha.point(lambda p: int(p * 0.7))
            logo_alpha.putalpha(alpha)
            padding = 20
            logo_x = img.width - logo_size - padding
            logo_y = img.height - logo_size - padding - 25
            overlay.paste(logo_alpha, (logo_x, logo_y), logo_alpha)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except Exception:
            font = ImageFont.load_default()
        text = "SAMPA VOLLEYBALL LEAGUE"
        padding = 20
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        text_x = img.width - text_w - padding
        text_y = img.height - padding - 5
        draw.text((text_x + 1, text_y + 1), text, fill=(0, 0, 0, 120), font=font)
        draw.text((text_x, text_y), text, fill=(255, 255, 255, 180), font=font)
        try:
            big_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", int(img.height * 0.15))
        except Exception:
            big_font = font
        bt_bbox = draw.textbbox((0, 0), "SVL", font=big_font)
        bt_w = bt_bbox[2] - bt_bbox[0]
        bt_h = bt_bbox[3] - bt_bbox[1]
        draw.text(((img.width - bt_w) // 2, (img.height - bt_h) // 2), "SVL", fill=(255, 255, 255, 25), font=big_font)
        result = Image.alpha_composite(img, overlay).convert("RGB")
        result.save(output_path, "JPEG", quality=85)
    except Exception as e:
        log.warning(f"watermark failed for {img_path}: {e}")
        try:
            shutil.copy2(img_path, output_path)
        except Exception as e2:
            log.error(f"watermark fallback copy also failed: {e2}")

def _safe_ext(filename, allowed=ALLOWED_IMAGE_EXT, default='jpg'):
    """Extract a safe extension from filename. Falls back to default if invalid."""
    if not filename or '.' not in filename:
        return default
    ext = filename.rsplit('.', 1)[-1].lower()
    # Strip any non-alphanumeric chars to defeat smuggling like 'jpg.php'
    ext = ''.join(c for c in ext if c.isalnum())
    if ext in allowed:
        return ext
    return default

def save_photo_with_watermark(file, filename_prefix):
    """Save uploaded photo: original + watermarked version. Returns watermarked filename."""
    ensure_dirs()
    ext = _safe_ext(file.filename, ALLOWED_IMAGE_EXT, 'jpg')
    filename = secure_filename(f"{filename_prefix}.{ext}")
    orig_path = os.path.join(ORIGINALS_DIR, filename)
    wm_path = os.path.join(UPLOADS_DIR, filename)
    file.save(orig_path)
    apply_watermark(orig_path, wm_path)
    return filename

# ---------- Brute force ----------
login_attempts = {}  # ip -> {"count": int, "locked_until": datetime}
MAX_ATTEMPTS = 7
LOCK_MINUTES = 5

# ---------- Defaults ----------
# NOTE: admin_password_hash starts empty; on first run we hash the bootstrap password
# 'sampa2026' from env DEFAULT_ADMIN_PASSWORD or fallback. Existing installations keep working.
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'sampa2026')

DEFAULT_DATA = {
    # Lista de torneios (era "etapas" por naipe). Cada torneio é uma competição completa e
    # independente: carrega seu naipe, categoria, formato, config e regulamento próprios.
    "torneios": [],
    # Equipes vira lista ÚNICA; cada inscrição carrega torneio_id (1 inscrição = 1 equipe + 1 torneio).
    "equipes": [],
    "atletas": {},  # chaveado por equipe_id (inalterado — equipe_id já é único por inscrição)
    # Grupos e jogos agora chaveados por torneio_id (era por naipe).
    "grupos": {},   # {torneio_id: {"A": [eid...], "B": [eid...]}}
    "jogos": {},    # {torneio_id: [ {jogo...}, ... ]}
    # Regulamento: default global + override opcional por torneio (torneio["regulamento"]).
    "regulamento_default": "",
    # Config template para novos torneios; cada torneio carrega sua própria cópia destes campos.
    "config_default": {"max_equipes": 8, "formato_jogos": "grupos", "hora_inicio": "08:30", "intervalo_min": 75},
    "settings": {
        "nome_torneio": "Sampa Volleyball League",
        "subtitulo": "Temporada 2026",
        "instagram": "@sampavolley_league",
        "instagram_url": "https://instagram.com/sampavolley_league",
        "email": "sampavolleyleague@gmail.com",
        "telefone": "",
        "pix_chave": "",
        "pix_tipo": "CPF",
        "pix_titular": "",
        "pix_banco": "",
        "pix_instrucoes": "Envie o comprovante pelo site após o pagamento."
    },
    "patrocinadores": [],  # NEW: list of {id, nome, tipo, url, logo, ordem, created_at}
    "admin_password_hash": ""
}

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    os.makedirs(ORIGINALS_DIR, exist_ok=True)

# ---------- Data file with file lock ----------
class _DataLock:
    """Cross-process file lock for tournament.json read-modify-write cycles."""
    def __init__(self, path):
        self.path = path
        self.fh = None
    def __enter__(self):
        ensure_dirs()
        self.fh = open(self.path, 'a+')
        if _HAS_FCNTL:
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self
    def __exit__(self, *exc):
        try:
            if _HAS_FCNTL:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()

def _formato_label_to_enum(label):
    """Mapeia o texto livre de etapa.formato (ex: 'Hexagonal Feminino') pro enum formato_jogos.
    Retorna None se não reconhecer (aí o migrador cai no formato_jogos da config do naipe)."""
    s = (label or "").strip().lower()
    if not s:
        return None
    if "hexagonal" in s:
        return "hexagonal"
    if "quad" in s and ("decis" in s or "final" in s):
        return "quad_decisao"
    if "quad" in s:
        return "quad_corrido"
    if "tri" in s and "final" in s:
        return "tri_final"
    if "tri" in s:
        return "tri_corrido"
    if "grupo" in s:
        return "grupos"
    return None

def _migrate_to_torneios(data):
    """Migração única e idempotente do schema por-naipe -> por-torneio.
    Só roda se detectar o schema antigo (etapas/equipes como dict por naipe).
      - etapas[naipe][]    -> torneios[]        (cada torneio carrega naipe + config própria)
      - equipes[naipe][]   -> equipes[]         (lista única; cada equipe recebe torneio_id)
      - grupos[naipe]      -> grupos[torneio_id]
      - jogos[naipe]       -> jogos[torneio_id]
      - config[naipe]      -> campos do torneio (formato_jogos/max_equipes/hora/intervalo)
      - regulamento[naipe] -> regulamento_default (global) + torneio.regulamento
    Vínculo de equipe legada: se o naipe tem exatamente 1 torneio, vincula a ele; se tem 0 ou
    >1 (ambíguo), fica torneio_id=None e o admin atribui pela tela (caso QUEEN do SPEC)."""
    old_etapas = data.get("etapas")
    old_equipes = data.get("equipes")
    # Detecta schema antigo: etapas OU equipes como dict por naipe.
    if not (isinstance(old_etapas, dict) or isinstance(old_equipes, dict)):
        return  # já está no schema novo — no-op
    old_config = data.get("config") if isinstance(data.get("config"), dict) else {}
    old_grupos = data.get("grupos") if isinstance(data.get("grupos"), dict) else {}
    old_jogos = data.get("jogos") if isinstance(data.get("jogos"), dict) else {}
    old_reg = data.get("regulamento") if isinstance(data.get("regulamento"), dict) else {}
    cfg_default = DEFAULT_DATA["config_default"]

    torneios = []
    naipe_tids = {}  # naipe -> [torneio_id, ...] na ordem das etapas
    for naipe in ("masculino", "feminino"):
        etapas_naipe = old_etapas.get(naipe, []) if isinstance(old_etapas, dict) else []
        cfg = old_config.get(naipe, {}) if isinstance(old_config, dict) else {}
        tids = []
        for etapa in etapas_naipe:
            tid = etapa.get("id") or str(uuid.uuid4())[:8]
            formato = _formato_label_to_enum(etapa.get("formato")) or cfg.get("formato_jogos", cfg_default["formato_jogos"])
            data_ev = etapa.get("data", "")
            torneios.append({
                "id": tid,
                "naipe": naipe,
                "nome": etapa.get("nome", ""),
                "categoria": etapa.get("categoria", ""),
                "local": etapa.get("local", ""),
                "endereco": etapa.get("endereco", ""),
                "data": data_ev,
                "horario": etapa.get("horario", "") or cfg.get("hora_inicio", cfg_default["hora_inicio"]),
                "formato_jogos": formato,
                "max_equipes": cfg.get("max_equipes", cfg_default["max_equipes"]),
                "hora_inicio": cfg.get("hora_inicio", cfg_default["hora_inicio"]),
                "intervalo_min": cfg.get("intervalo_min", cfg_default["intervalo_min"]),
                "taxa": etapa.get("taxa", 0),
                "adiado": bool(etapa.get("adiado", data_ev == "2026-05-09")),
                "regulamento": etapa.get("regulamento", ""),
                "created_at": etapa.get("created_at", datetime.now().isoformat()),
            })
            tids.append(tid)
        naipe_tids[naipe] = tids

    # equipes -> lista única com torneio_id
    new_equipes = []
    if isinstance(old_equipes, dict):
        for naipe in ("masculino", "feminino"):
            tids = naipe_tids.get(naipe, [])
            auto_tid = tids[0] if len(tids) == 1 else None
            for eq in old_equipes.get(naipe, []):
                eq = dict(eq)
                if eq.get("torneio_id") in (None, ""):
                    eq["torneio_id"] = auto_tid
                eq.setdefault("_naipe_legado", naipe)  # ajuda o admin a saber o naipe original
                new_equipes.append(eq)
    elif isinstance(old_equipes, list):
        new_equipes = old_equipes

    # grupos / jogos -> chaveados por torneio_id
    new_grupos, new_jogos = {}, {}
    for naipe in ("masculino", "feminino"):
        tids = naipe_tids.get(naipe, [])
        if not tids:
            continue
        target = tids[0]  # 1 torneio: é ele. >1: atribui ao mais antigo e loga.
        g = old_grupos.get(naipe) if isinstance(old_grupos, dict) else None
        j = old_jogos.get(naipe) if isinstance(old_jogos, dict) else None
        tem_g = isinstance(g, dict) and (g.get("A") or g.get("B"))
        if len(tids) > 1 and (tem_g or j):
            log.warning(f"Migração: naipe '{naipe}' tem {len(tids)} torneios mas grupos/jogos eram "
                        f"únicos por naipe; vinculados ao torneio mais antigo ({target}). Admin deve conferir.")
        if tem_g:
            new_grupos[target] = {"A": list(g.get("A", [])), "B": list(g.get("B", []))}
        if j:
            new_jogos[target] = j

    # regulamento global default (pega o que houver do schema antigo)
    reg_default = ""
    if isinstance(old_reg, dict):
        reg_default = old_reg.get("feminino") or old_reg.get("masculino") or ""

    data["torneios"] = torneios
    data["equipes"] = new_equipes
    data["grupos"] = new_grupos
    data["jogos"] = new_jogos
    data["regulamento_default"] = data.get("regulamento_default") or reg_default
    data.setdefault("config_default", json.loads(json.dumps(cfg_default)))
    for k in ("etapas", "config", "regulamento"):
        data.pop(k, None)
    log.info(f"Migração multi-torneio concluída: {len(torneios)} torneios, {len(new_equipes)} equipes, "
             f"{len(new_grupos)} c/ grupos, {len(new_jogos)} c/ jogos.")

def _read_data_unlocked():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = json.loads(json.dumps(DEFAULT_DATA))
    # MIGRAÇÃO multi-torneio (idempotente). Roda ANTES dos backfills, pois converte o schema
    # antigo (por-naipe) no novo (por-torneio) que o resto do código passa a assumir.
    _migrate_to_torneios(data)
    # Top-level migrations / defaults
    for key in DEFAULT_DATA:
        if key not in data:
            data[key] = DEFAULT_DATA[key] if not isinstance(DEFAULT_DATA[key], (dict, list)) else json.loads(json.dumps(DEFAULT_DATA[key]))
    # Migration: legacy 'admin_password' (plaintext) -> admin_password_hash
    if data.get('admin_password') and not data.get('admin_password_hash'):
        data['admin_password_hash'] = generate_password_hash(data['admin_password'])
        log.info("Migrated legacy admin_password to admin_password_hash")
    if 'admin_password' in data:
        data.pop('admin_password', None)
    if not data.get('admin_password_hash'):
        data['admin_password_hash'] = generate_password_hash(BOOTSTRAP_ADMIN_PASSWORD)
        log.info(f"Bootstrapped admin password hash from env/default")
    # Migration: equipe.senha (plaintext) -> equipe.senha_hash (equipes agora é lista única)
    for eq in data.get('equipes', []):
        if eq.get('senha') and not eq.get('senha_hash'):
            eq['senha_hash'] = generate_password_hash(eq['senha'])
            # Zera o texto puro após derivar o hash (mesma política de antes).
            eq.pop('senha', None)
    return data

def _write_data_unlocked(data):
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def load_data():
    """Read snapshot under lock. Use update_data() for read-modify-write."""
    ensure_dirs()
    with _DataLock(DATA_LOCK):
        return _read_data_unlocked()

def save_data(data):
    """Write data under lock."""
    ensure_dirs()
    with _DataLock(DATA_LOCK):
        _write_data_unlocked(data)

def update_data(fn):
    """Atomic read-modify-write. fn(data) -> result. Saves data and returns result."""
    ensure_dirs()
    with _DataLock(DATA_LOCK):
        data = _read_data_unlocked()
        result = fn(data)
        _write_data_unlocked(data)
        return result

def do_backup():
    ensure_dirs()
    if os.path.exists(DATA_FILE):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(BACKUP_DIR, f'tournament_{ts}.json')
        shutil.copy2(DATA_FILE, backup_path)
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')])
        while len(backups) > 30:
            os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))

def get_torneio(data, torneio_id):
    """Retorna o dict do torneio pelo id (que já carrega formato_jogos/max_equipes/hora_inicio/
    intervalo_min — funciona como a antiga 'config'), ou None se não existir."""
    for t in data.get("torneios", []):
        if t.get("id") == torneio_id:
            return t
    return None

def torneios_do_naipe(data, naipe):
    """Lista os torneios de um naipe (ordenados por data)."""
    ts = [t for t in data.get("torneios", []) if t.get("naipe") == naipe]
    return sorted(ts, key=lambda t: (t.get("data") or "", t.get("created_at") or ""))

FORMATOS_VALIDOS = ("hexagonal", "grupos", "quad_corrido", "quad_decisao",
                    "tri_corrido", "tri_final", "penta_corrido", "penta_decisao")

def _clamp_int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (ValueError, TypeError):
        return default

def _valid_hora(s):
    """Retorna 'HH:MM' normalizado se válido, senão None."""
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (ValueError, AttributeError):
        pass
    return None

def find_equipe(data, equipe_id):
    """Retorna o dict da equipe (inscrição) pelo id na lista única, ou None."""
    for e in data.get("equipes", []):
        if e.get("id") == equipe_id:
            return e
    return None

def naipe_do_equipe(data, equipe):
    """Naipe de uma equipe = naipe do seu torneio (ou None se órfã)."""
    t = get_torneio(data, equipe.get("torneio_id")) if equipe else None
    return t.get("naipe") if t else None

def generate_team_password():
    chars = string.ascii_uppercase + string.digits
    return f"SVL-{''.join(secrets.choice(chars) for _ in range(6))}"

def generate_unique_team_password(existing_hashes):
    """Generate a password that hasn't been used (we can't compare hashes for collision,
    but we ensure entropy by using secrets and the visible space is huge enough)."""
    return generate_team_password()

def generate_team_login(nome):
    login = nome.lower().strip()
    login = login.replace(' ', '-').replace('/', '-').replace('.', '')
    for a, b in [('á','a'),('à','a'),('ã','a'),('â','a'),('é','e'),('ê','e'),('í','i'),('ó','o'),('ô','o'),('õ','o'),('ú','u'),('ç','c')]:
        login = login.replace(a, b)
    login = ''.join(c for c in login if c.isalnum() or c == '-')
    return login[:20] or 'equipe'

# ---------- Brute force helpers ----------
def check_brute_force(ip):
    if ip in login_attempts:
        info = login_attempts[ip]
        if info.get("locked_until") and datetime.now() < info["locked_until"]:
            return True
        if info.get("locked_until") and datetime.now() >= info["locked_until"]:
            del login_attempts[ip]
    return False

def record_failed_attempt(ip):
    if ip not in login_attempts:
        login_attempts[ip] = {"count": 0}
    login_attempts[ip]["count"] += 1
    if login_attempts[ip]["count"] >= MAX_ATTEMPTS:
        login_attempts[ip]["locked_until"] = datetime.now() + timedelta(minutes=LOCK_MINUTES)

def clear_attempts(ip):
    if ip in login_attempts:
        del login_attempts[ip]

def get_client_ip():
    # ProxyFix already set request.remote_addr from trusted X-Forwarded-For.
    return request.remote_addr or '0.0.0.0'

# ---------- Auth decorators ----------
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({"error": "Acesso restrito ao administrador"}), 403
        return f(*args, **kwargs)
    return wrapper

def team_or_admin_required(f):
    """Allows admin OR the team itself (matched by equipe_id in URL)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('is_admin'):
            return f(*args, **kwargs)
        equipe_id = kwargs.get('equipe_id')
        if equipe_id and session.get('team_id') == equipe_id:
            return f(*args, **kwargs)
        return jsonify({"error": "Acesso restrito"}), 403
    return wrapper

# ---------- Auto-classify ----------
def auto_classify_semis(data, torneio_id, test_mode=False):
    """Auto-classifica semi/final para um torneio.
    test_mode=False: opera em jogos reais (data["jogos"][torneio_id], grupos data["grupos"][torneio_id]).
    test_mode=True: opera só em jogos is_test e grupos data["grupos_test"][torneio_id]."""
    if test_mode:
        # Config padrão pra teste (vai vir do payload do setup)
        fmt = data.get("test_config", {}).get("formato_jogos", "hexagonal")
        jogos = [j for j in data["jogos"].get(torneio_id, []) if j.get("is_test")]
        grupos_src = data.get("grupos_test", {}).get(torneio_id, {"A": [], "B": []})
    else:
        torneio = get_torneio(data, torneio_id) or {}
        fmt = torneio.get("formato_jogos", "grupos")
        # Jogos de teste nunca participam de auto-classificação real
        jogos = [j for j in data["jogos"].get(torneio_id, []) if not j.get("is_test")]
        grupos_src = data["grupos"].get(torneio_id, {"A": [], "B": []})
    if fmt == "hexagonal":
        hex_jogos = [j for j in jogos if j["fase"] == "hexagonal"]
        if not hex_jogos or not all(j["finalizado"] for j in hex_jogos):
            _merge_jogos_back(data, torneio_id, jogos, test_mode)
            return
        eids = grupos_src.get("A", [])
        ranking = compute_ranking(eids, hex_jogos, "hexagonal")
        if len(ranking) >= 4:
            for j in jogos:
                if j["fase"] == "semi" and "1º x 4º" in j.get("label", "") and not j["equipe_a"]:
                    j["equipe_a"] = ranking[0]["id"]
                    j["equipe_b"] = ranking[3]["id"]
                elif j["fase"] == "semi" and "2º x 3º" in j.get("label", "") and not j["equipe_a"]:
                    j["equipe_a"] = ranking[1]["id"]
                    j["equipe_b"] = ranking[2]["id"]
    elif fmt in ("quad_decisao", "penta_decisao"):
        # Corrido com decisão (4 ou 5 equipes): preenche disputa de 3º (3ºx4º) + final (1ºx2º)
        hex_jogos = [j for j in jogos if j["fase"] == "hexagonal"]
        if not hex_jogos or not all(j["finalizado"] for j in hex_jogos):
            _merge_jogos_back(data, torneio_id, jogos, test_mode)
            return
        eids = grupos_src.get("A", [])
        ranking = compute_ranking(eids, hex_jogos, "hexagonal")
        if len(ranking) >= 4:
            for j in jogos:
                if j["fase"] == "final" and not j.get("equipe_a"):
                    j["equipe_a"] = ranking[0]["id"]
                    j["equipe_b"] = ranking[1]["id"]
                elif j["fase"] == "terceiro" and not j.get("equipe_a"):
                    j["equipe_a"] = ranking[2]["id"]
                    j["equipe_b"] = ranking[3]["id"]
        _merge_jogos_back(data, torneio_id, jogos, test_mode)
        return
    elif fmt == "tri_final":
        # Triangular com final: regulares completos -> final = 1º x 2º (sem disputa de 3º)
        hex_jogos = [j for j in jogos if j["fase"] == "hexagonal"]
        if not hex_jogos or not all(j["finalizado"] for j in hex_jogos):
            _merge_jogos_back(data, torneio_id, jogos, test_mode)
            return
        eids = grupos_src.get("A", [])
        ranking = compute_ranking(eids, hex_jogos, "hexagonal")
        if len(ranking) >= 2:
            for j in jogos:
                if j["fase"] == "final" and not j.get("equipe_a"):
                    j["equipe_a"] = ranking[0]["id"]
                    j["equipe_b"] = ranking[1]["id"]
        _merge_jogos_back(data, torneio_id, jogos, test_mode)
        return
    elif fmt in ("quad_corrido", "tri_corrido", "penta_corrido"):
        # Sem fase eliminatória — nada a auto-classificar
        _merge_jogos_back(data, torneio_id, jogos, test_mode)
        return
    else:
        # fmt == "grupos"
        grpA = [j for j in jogos if j["fase"] == "grupos" and j["grupo"] == "A"]
        grpB = [j for j in jogos if j["fase"] == "grupos" and j["grupo"] == "B"]
        if not grpA or not grpB:
            _merge_jogos_back(data, torneio_id, jogos, test_mode)
            return
        if not all(j["finalizado"] for j in grpA) or not all(j["finalizado"] for j in grpB):
            _merge_jogos_back(data, torneio_id, jogos, test_mode)
            return
        eidsA = grupos_src.get("A", [])
        eidsB = grupos_src.get("B", [])
        rankA = compute_ranking(eidsA, grpA, "grupos")
        rankB = compute_ranking(eidsB, grpB, "grupos")
        if len(rankA) >= 2 and len(rankB) >= 2:
            for j in jogos:
                if j["fase"] == "semi" and j.get("label", "").startswith("Semi 1") and not j["equipe_a"]:
                    j["equipe_a"] = rankA[0]["id"]
                    j["equipe_b"] = rankB[1]["id"]
                elif j["fase"] == "semi" and j.get("label", "").startswith("Semi 2") and not j["equipe_a"]:
                    j["equipe_a"] = rankB[0]["id"]
                    j["equipe_b"] = rankA[1]["id"]
    semis = [j for j in jogos if j["fase"] == "semi"]
    if len(semis) == 2 and all(s.get("finalizado") for s in semis):
        semi1, semi2 = semis[0], semis[1]
        winner1 = semi1["equipe_a"] if semi1["sets_a"] > semi1["sets_b"] else semi1["equipe_b"]
        winner2 = semi2["equipe_a"] if semi2["sets_a"] > semi2["sets_b"] else semi2["equipe_b"]
        loser1 = semi1["equipe_b"] if semi1["sets_a"] > semi1["sets_b"] else semi1["equipe_a"]
        loser2 = semi2["equipe_b"] if semi2["sets_a"] > semi2["sets_b"] else semi2["equipe_a"]
        for j in jogos:
            if j["fase"] == "final" and not j.get("equipe_a"):
                j["equipe_a"] = winner1
                j["equipe_b"] = winner2
            elif j["fase"] == "terceiro" and not j.get("equipe_a"):
                j["equipe_a"] = loser1
                j["equipe_b"] = loser2
    _merge_jogos_back(data, torneio_id, jogos, test_mode)

def _merge_jogos_back(data, torneio_id, jogos_modificados, test_mode):
    """Helper: faz merge dos jogos modificados de volta no data, preservando o que não foi tocado."""
    by_id = {j["id"]: j for j in jogos_modificados}
    merged = []
    for j in data["jogos"].get(torneio_id, []):
        if test_mode:
            if j.get("is_test") and j["id"] in by_id:
                merged.append(by_id[j["id"]])
            else:
                merged.append(j)  # real ou teste não modificado
        else:
            if j.get("is_test"):
                merged.append(j)  # teste sempre preservado
            elif j["id"] in by_id:
                merged.append(by_id[j["id"]])
            else:
                merged.append(j)
    data["jogos"][torneio_id] = merged

def compute_ranking(eids, jogos, fase):
    """Ranking with FIVB-style tiebreaker: points -> set ratio -> point ratio -> head-to-head."""
    st = {}
    for eid in eids:
        st[eid] = {"id": eid, "jogos": 0, "vitorias": 0, "derrotas": 0,
                   "sets_pro": 0, "sets_contra": 0,
                   "pontos_pro": 0, "pontos_contra": 0, "pontos": 0}
    h2h = {}  # (a,b) -> "a" if a beat b
    for j in jogos:
        if not j.get("finalizado"):
            continue
        a, b, sa, sb = j["equipe_a"], j["equipe_b"], j["sets_a"], j["sets_b"]
        # Sum partial scores when available for pontos_pro/contra
        pa = pb = 0
        for parcial in (j.get("parciais") or []):
            try:
                if isinstance(parcial, str) and 'x' in parcial.lower():
                    p = parcial.lower().split('x')
                elif isinstance(parcial, str) and '-' in parcial:
                    p = parcial.split('-')
                elif isinstance(parcial, (list, tuple)) and len(parcial) == 2:
                    p = parcial
                else:
                    continue
                pa += int(str(p[0]).strip())
                pb += int(str(p[1]).strip())
            except Exception:
                continue
        for t, sp, sc, pp, pc in [(a, sa, sb, pa, pb), (b, sb, sa, pb, pa)]:
            if t in st:
                st[t]["jogos"] += 1
                st[t]["sets_pro"] += sp
                st[t]["sets_contra"] += sc
                st[t]["pontos_pro"] += pp
                st[t]["pontos_contra"] += pc
                if sp > sc:
                    st[t]["vitorias"] += 1
                    st[t]["pontos"] += 3 if sc == 0 else 2
                else:
                    st[t]["derrotas"] += 1
                    st[t]["pontos"] += 1 if sp > 0 else 0
        if a in st and b in st:
            if sa > sb:
                h2h[(a, b)] = a
            elif sb > sa:
                h2h[(a, b)] = b

    def set_ratio(s):
        return s["sets_pro"] / s["sets_contra"] if s["sets_contra"] > 0 else (999 if s["sets_pro"] > 0 else 0)
    def point_ratio(s):
        return s["pontos_pro"] / s["pontos_contra"] if s["pontos_contra"] > 0 else (999 if s["pontos_pro"] > 0 else 0)

    ordered = sorted(st.values(), key=lambda x: (x["pontos"], set_ratio(x), point_ratio(x)), reverse=True)
    # Resolve pairwise ties via head-to-head when possible
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i+1]
        if a["pontos"] == b["pontos"] and abs(set_ratio(a) - set_ratio(b)) < 1e-9 and abs(point_ratio(a) - point_ratio(b)) < 1e-9:
            winner = h2h.get((a["id"], b["id"])) or h2h.get((b["id"], a["id"]))
            if winner == b["id"]:
                ordered[i], ordered[i+1] = ordered[i+1], ordered[i]
    return ordered


# ===================== ROUTES =====================

@app.route('/')
def landing():
    resp = make_response(send_from_directory('static', 'landing.html'))
    track_visit(resp, 'landing')
    return resp

@app.route('/app')
def apppage():
    resp = make_response(send_from_directory('static', 'index.html'))
    track_visit(resp, 'app')
    return resp

def track_visit(resp, page):
    """Track visit. Cookie set first; counters increment under lock."""
    visitor_id = request.cookies.get('svl_visitor', '')
    is_new_cookie = False
    if not visitor_id:
        visitor_id = uuid.uuid4().hex[:12]
        resp.set_cookie('svl_visitor', visitor_id, max_age=86400*365, httponly=True, samesite='Lax')
        is_new_cookie = True
    today = datetime.now().strftime('%Y-%m-%d')
    def _bump(data):
        if "analytics" not in data:
            data["analytics"] = {"landing": {"total": 0, "por_dia": {}}, "app": {"total": 0, "por_dia": {}}}
        if page not in data["analytics"]:
            data["analytics"][page] = {"total": 0, "por_dia": {}}
        if today not in data["analytics"][page]["por_dia"]:
            data["analytics"][page]["por_dia"][today] = {"acessos": 0, "unicos": 0, "visitors": []}
        day_data = data["analytics"][page]["por_dia"][today]
        data["analytics"][page]["total"] += 1
        day_data["acessos"] += 1
        if visitor_id not in day_data.get("visitors", []):
            day_data["unicos"] += 1
            if len(day_data["visitors"]) < 5000:
                day_data["visitors"].append(visitor_id)
        keys = sorted(data["analytics"][page]["por_dia"].keys())
        for k in keys[:-7]:
            if "visitors" in data["analytics"][page]["por_dia"][k]:
                data["analytics"][page]["por_dia"][k]["visitors"] = []
    try:
        update_data(_bump)
    except Exception as e:
        log.warning(f"track_visit failed: {e}")

@app.route('/api/analytics', methods=['GET'])
@admin_required
def get_analytics():
    data = load_data()
    analytics = data.get("analytics", {})
    result = {}
    for page in ["landing", "app"]:
        pg = analytics.get(page, {"total": 0, "por_dia": {}})
        today = datetime.now().strftime('%Y-%m-%d')
        hoje = pg.get("por_dia", {}).get(today, {"acessos": 0, "unicos": 0})
        ultimos_7 = []
        for i in range(6, -1, -1):
            d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            dd = pg.get("por_dia", {}).get(d, {"acessos": 0, "unicos": 0})
            ultimos_7.append({"data": d, "acessos": dd.get("acessos", 0), "unicos": dd.get("unicos", 0)})
        result[page] = {
            "total": pg.get("total", 0),
            "hoje_acessos": hoje.get("acessos", 0),
            "hoje_unicos": hoje.get("unicos", 0),
            "ultimos_7": ultimos_7
        }
    return jsonify(result)

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

# --- AUTH (Admin) ---
@app.route('/api/auth', methods=['POST'])
def auth():
    ip = get_client_ip()
    if check_brute_force(ip):
        remaining = login_attempts[ip]["locked_until"] - datetime.now()
        mins = max(1, int(remaining.total_seconds() / 60))
        return jsonify({"ok": False, "error": f"Bloqueado por {mins} minutos. Muitas tentativas erradas."}), 429
    data = load_data()
    pwd = (request.json or {}).get('password', '')
    pwd_hash = data.get('admin_password_hash', '')
    if pwd_hash and check_password_hash(pwd_hash, pwd):
        clear_attempts(ip)
        session.clear()
        session['is_admin'] = True
        session.permanent = True
        return jsonify({"ok": True})
    record_failed_attempt(ip)
    attempts_left = MAX_ATTEMPTS - login_attempts.get(ip, {}).get("count", 0)
    if attempts_left <= 0:
        return jsonify({"ok": False, "error": f"Bloqueado por {LOCK_MINUTES} minutos."}), 429
    return jsonify({"ok": False, "error": f"Senha incorreta. {attempts_left} tentativa(s) restante(s)."}), 401

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    return jsonify({
        "is_admin": bool(session.get('is_admin')),
        "team_id": session.get('team_id'),
        "team_naipe": session.get('team_naipe'),
        "team_torneio_id": session.get('team_torneio_id'),
        "team_nome": session.get('team_nome'),
    })

# --- AUTH (Equipe) ---
@app.route('/api/auth/equipe', methods=['POST'])
def auth_equipe():
    ip = get_client_ip()
    if check_brute_force(ip):
        remaining = login_attempts[ip]["locked_until"] - datetime.now()
        mins = max(1, int(remaining.total_seconds() / 60))
        return jsonify({"ok": False, "error": f"Bloqueado por {mins} minutos."}), 429
    data = load_data()
    body = request.json or {}
    login = body.get("login", "").strip().lower()
    senha = body.get("senha", "").strip()
    if not login or not senha:
        return jsonify({"ok": False, "error": "Preencha login e senha"}), 400
    for eq in data.get("equipes", []):
        if eq.get("login") == login and eq.get("senha_hash") and check_password_hash(eq["senha_hash"], senha):
            clear_attempts(ip)
            naipe = naipe_do_equipe(data, eq)
            session.clear()
            session['team_id'] = eq["id"]
            session['team_naipe'] = naipe
            session['team_torneio_id'] = eq.get("torneio_id")
            session['team_nome'] = eq["nome"]
            session.permanent = True
            return jsonify({"ok": True, "equipe_id": eq["id"], "naipe": naipe,
                "torneio_id": eq.get("torneio_id"), "nome": eq["nome"],
                "pagamento_status": eq.get("pagamento_status", "pendente")})
    record_failed_attempt(ip)
    attempts_left = MAX_ATTEMPTS - login_attempts.get(ip, {}).get("count", 0)
    if attempts_left <= 0:
        return jsonify({"ok": False, "error": f"Bloqueado por {LOCK_MINUTES} minutos."}), 429
    return jsonify({"ok": False, "error": f"Login ou senha incorretos. {attempts_left} tentativa(s)."}), 401

# --- TORNEIOS (era ETAPAS + CONFIG por naipe; config agora é embutida no torneio) ---
def _torneio_publico(t, data=None):
    """Torneio sem campos internos (prefixados com _). Se data for passado, inclui contadores
    inscritos / lista_espera_count / lotado (pra card público e lógica de inscrição)."""
    out = {k: v for k, v in t.items() if not k.startswith("_")}
    if data is not None:
        tid = t.get("id")
        eqs = [e for e in data.get("equipes", []) if e.get("torneio_id") == tid and not e.get("is_test")]
        inscritos = sum(1 for e in eqs if not e.get("lista_espera"))
        out["inscritos"] = inscritos
        out["lista_espera_count"] = sum(1 for e in eqs if e.get("lista_espera"))
        out["lotado"] = inscritos >= t.get("max_equipes", 8)
    return out

@app.route('/api/torneios', methods=['GET'])
def get_torneios():
    """Lista torneios com contadores (inscritos/lista_espera/lotado). ?naipe= filtra. Público."""
    data = load_data()
    naipe = request.args.get("naipe")
    ts = data.get("torneios", [])
    if naipe:
        ts = [t for t in ts if t.get("naipe") == naipe]
    ts = sorted(ts, key=lambda t: (t.get("data") or "", t.get("created_at") or ""))
    return jsonify([_torneio_publico(t, data) for t in ts])

@app.route('/api/torneios/<torneio_id>', methods=['GET'])
def get_torneio_route(torneio_id):
    data = load_data()
    t = get_torneio(data, torneio_id)
    if not t:
        return jsonify({"error": "Torneio não encontrado"}), 404
    return jsonify(_torneio_publico(t, data))

@app.route('/api/torneios', methods=['POST'])
@admin_required
def add_torneio():
    body = request.json or {}
    naipe = body.get("naipe")
    if naipe not in ("masculino", "feminino"):
        return jsonify({"error": "naipe inválido (masculino|feminino)"}), 400
    def _do(data):
        cfgd = data.get("config_default", DEFAULT_DATA["config_default"])
        fmt = body.get("formato_jogos")
        if fmt not in FORMATOS_VALIDOS:
            fmt = cfgd["formato_jogos"]
        torneio = {
            "id": str(uuid.uuid4())[:8],
            "naipe": naipe,
            "nome": body.get("nome", ""),
            "categoria": body.get("categoria", ""),
            "local": body.get("local", ""),
            "endereco": body.get("endereco", ""),
            "data": body.get("data", ""),
            "horario": body.get("horario", "") or cfgd["hora_inicio"],
            "formato_jogos": fmt,
            "max_equipes": _clamp_int(body.get("max_equipes"), cfgd["max_equipes"], 2, 32),
            "hora_inicio": _valid_hora(body.get("hora_inicio")) or cfgd["hora_inicio"],
            "intervalo_min": _clamp_int(body.get("intervalo_min"), cfgd["intervalo_min"], 15, 240),
            "taxa": body.get("taxa", 0),
            "adiado": bool(body.get("adiado", False)),
            "regulamento": body.get("regulamento", ""),
            "created_at": datetime.now().isoformat(),
        }
        data.setdefault("torneios", []).append(torneio)
        return torneio
    return jsonify(update_data(_do)), 201

@app.route('/api/torneios/<torneio_id>', methods=['PUT'])
@admin_required
def update_torneio(torneio_id):
    """Atualiza torneio, incluindo a config embutida (formato/hora/intervalo/max)."""
    body = request.json or {}
    if not get_torneio(load_data(), torneio_id):
        return jsonify({"error": "Torneio não encontrado"}), 404
    def _do(data):
        t = get_torneio(data, torneio_id)
        if not t:
            return None
        for k in ("nome", "categoria", "local", "endereco", "data", "horario", "regulamento", "taxa"):
            if k in body:
                t[k] = body[k]
        if "naipe" in body and body["naipe"] in ("masculino", "feminino"):
            t["naipe"] = body["naipe"]
        if "formato_jogos" in body and body["formato_jogos"] in FORMATOS_VALIDOS:
            t["formato_jogos"] = body["formato_jogos"]
        if "max_equipes" in body:
            t["max_equipes"] = _clamp_int(body.get("max_equipes"), t.get("max_equipes", 8), 2, 32)
        if "hora_inicio" in body:
            hv = _valid_hora(body.get("hora_inicio"))
            if hv:
                t["hora_inicio"] = hv
        if "intervalo_min" in body:
            t["intervalo_min"] = _clamp_int(body.get("intervalo_min"), t.get("intervalo_min", 75), 15, 240)
        if "adiado" in body:
            t["adiado"] = bool(body["adiado"])
        return t
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Torneio não encontrado"}), 404
    return jsonify(res)

@app.route('/api/torneios/<torneio_id>', methods=['DELETE'])
@admin_required
def delete_torneio(torneio_id):
    """Remove o torneio e seus grupos/jogos. Equipes inscritas viram órfãs (torneio_id=None)
    pra não perder inscrição/pagamento — o admin reatribui ou apaga manualmente."""
    def _do(data):
        data["torneios"] = [t for t in data.get("torneios", []) if t.get("id") != torneio_id]
        data.get("grupos", {}).pop(torneio_id, None)
        data.get("jogos", {}).pop(torneio_id, None)
        orfas = 0
        for e in data.get("equipes", []):
            if e.get("torneio_id") == torneio_id:
                e["torneio_id"] = None
                orfas += 1
        return {"ok": True, "equipes_orfas": orfas}
    return jsonify(update_data(_do))

# --- EQUIPES ---
@app.route('/api/equipes', methods=['GET'])
def get_equipes():
    """Público. ?torneio_id=<id> filtra por torneio. Sem is_test, sem dados sensíveis."""
    data = load_data()
    tid = request.args.get("torneio_id")
    public = []
    for e in data.get("equipes", []):
        if e.get("is_test"):
            continue  # Equipes de teste nunca aparecem em endpoint público
        if e.get("lista_espera"):
            continue  # Lista de espera não é equipe inscrita (contagem sai no /api/torneios)
        if tid and e.get("torneio_id") != tid:
            continue
        pe = {k: v for k, v in e.items() if k not in ("senha", "senha_hash", "login")}
        public.append(pe)
    return jsonify(public)

@app.route('/api/equipes/admin', methods=['GET'])
@admin_required
def get_equipes_admin():
    """Admin view. Exclui senha_hash. Retorna TODAS as equipes incluindo is_test.
    ?torneio_id=<id> filtra por torneio; ?torneio_id=null retorna as sem torneio (órfãs)."""
    data = load_data()
    tid = request.args.get("torneio_id")
    out = []
    for e in data.get("equipes", []):
        if tid is not None:
            want = None if tid in ("", "null", "none") else tid
            if e.get("torneio_id") != want:
                continue
        pe = {k: v for k, v in e.items() if k != "senha_hash"}
        out.append(pe)
    return jsonify(out)

# Inscrição é pública (POST /api/equipes/<naipe>) durante a janela aberta.
# A janela fica controlada também no front; aqui no back validamos só max_equipes
# e estado básico. Admin pode inscrever a qualquer momento.
INSCRICAO_ABRE_ISO = os.environ.get('INSCRICAO_ABRE_ISO', '2026-04-27T14:00:00-03:00')

def inscricao_aberta_back():
    try:
        from datetime import timezone
        # Parse manually to support -03:00 in older Python
        s = INSCRICAO_ABRE_ISO
        # Convert "2026-04-27T14:00:00-03:00" to a datetime
        dt = datetime.fromisoformat(s)
        return datetime.now(dt.tzinfo) >= dt
    except Exception as e:
        log.warning(f"inscricao_aberta_back parse failed: {e}")
        return True

@app.route('/api/equipes', methods=['POST'])
def add_equipe():
    is_admin = bool(session.get('is_admin'))
    if not is_admin and not inscricao_aberta_back():
        return jsonify({"error": "Inscrições ainda não estão abertas"}), 403
    body = request.json or {}
    torneio_id = body.get("torneio_id") or None
    nome = (body.get("nome", "") or "").strip()
    if not nome:
        return jsonify({"error": "Nome obrigatório"}), 400
    if len(nome) > 80:
        return jsonify({"error": "Nome muito longo"}), 400
    responsavel = (body.get("responsavel", "") or "").strip()[:80]
    telefone = (body.get("telefone", "") or "").strip()[:30]
    plain_password = None  # only kept inside this request to return to user once
    def _do(data):
        nonlocal plain_password
        torneio = get_torneio(data, torneio_id) if torneio_id else None
        # Inscrição pública exige torneio válido (o form pré-seleciona pela URL).
        # Admin pode criar sem torneio (equipe fica órfã pra atribuir depois).
        if torneio_id and not torneio:
            return ("notorneio", None)
        if not is_admin and not torneio:
            return ("notorneio", None)
        equipes = data.setdefault("equipes", [])
        if torneio:
            max_eq = torneio.get("max_equipes", 8)
            # Só equipes reais DO MESMO torneio contam pro máximo (teste e lista de espera nunca contam).
            reais_no_torneio = [e for e in equipes
                                if not e.get("is_test") and not e.get("lista_espera") and e.get("torneio_id") == torneio_id]
            if len(reais_no_torneio) >= max_eq:
                return ("max", max_eq)
        login = generate_team_login(nome)
        existing_logins = {e.get("login", "") for e in equipes}  # login é único globalmente
        base_login = login
        counter = 1
        while login in existing_logins:
            login = f"{base_login}-{counter}"
            counter += 1
        plain_password = generate_team_password()
        equipe = {
            "id": str(uuid.uuid4())[:8], "torneio_id": torneio_id, "nome": nome,
            "responsavel": responsavel, "telefone": telefone,
            "login": login,
            "senha_hash": generate_password_hash(plain_password),
            "pagamento_status": "pendente", "comprovante": None,
            "created_at": datetime.now().isoformat()
        }
        equipes.append(equipe)
        return ("ok", equipe)
    res = update_data(_do)
    if res and res[0] == "max":
        return jsonify({"error": f"Máximo de {res[1]} equipes atingido neste torneio"}), 400
    if res and res[0] == "notorneio":
        return jsonify({"error": "Selecione um torneio válido para a inscrição"}), 400
    do_backup()
    equipe = res[1]
    # Public-safe response: include plaintext password ONLY here, in the create response.
    out = {k: v for k, v in equipe.items() if k != "senha_hash"}
    out["senha"] = plain_password
    return jsonify(out), 201

@app.route('/api/equipes/lista-espera', methods=['POST'])
def add_lista_espera():
    """Entra na LISTA DE ESPERA de um torneio (usado quando está lotado). Cadastro leve:
    nome/responsável/telefone. Não conta no máximo, não gera login/senha (isso vem na promoção)."""
    is_admin = bool(session.get('is_admin'))
    if not is_admin and not inscricao_aberta_back():
        return jsonify({"error": "Inscrições ainda não estão abertas"}), 403
    body = request.json or {}
    torneio_id = body.get("torneio_id") or None
    nome = (body.get("nome", "") or "").strip()
    if not nome:
        return jsonify({"error": "Nome obrigatório"}), 400
    if len(nome) > 80:
        return jsonify({"error": "Nome muito longo"}), 400
    responsavel = (body.get("responsavel", "") or "").strip()[:80]
    telefone = (body.get("telefone", "") or "").strip()[:30]
    def _do(data):
        if not get_torneio(data, torneio_id):
            return ("notorneio", None)
        equipes = data.setdefault("equipes", [])
        na_espera = sum(1 for e in equipes
                        if e.get("lista_espera") and e.get("torneio_id") == torneio_id)
        entry = {
            "id": str(uuid.uuid4())[:8], "torneio_id": torneio_id, "nome": nome,
            "responsavel": responsavel, "telefone": telefone,
            "lista_espera": True, "pagamento_status": "pendente", "comprovante": None,
            "created_at": datetime.now().isoformat(),
        }
        equipes.append(entry)
        return ("ok", entry, na_espera + 1)
    res = update_data(_do)
    if res[0] == "notorneio":
        return jsonify({"error": "Selecione um torneio válido"}), 400
    do_backup()
    return jsonify({"ok": True, "id": res[1]["id"], "posicao": res[2]}), 201

@app.route('/api/equipes/<equipe_id>/promover', methods=['POST'])
@admin_required
def promover_lista_espera(equipe_id):
    """Promove uma equipe da lista de espera para inscrita: gera login+senha e tira a flag.
    Retorna a senha em texto puro UMA vez (igual inscrição normal)."""
    plain = None
    def _do(data):
        nonlocal plain
        eq = find_equipe(data, equipe_id)
        if not eq:
            return None
        if not eq.get("lista_espera"):
            return ("naoespera", None)
        equipes = data.get("equipes", [])
        login = generate_team_login(eq.get("nome", "equipe"))
        existing = {e.get("login", "") for e in equipes}
        base, i = login, 1
        while login in existing:
            login = f"{base}-{i}"; i += 1
        plain = generate_team_password()
        eq["login"] = login
        eq["senha_hash"] = generate_password_hash(plain)
        eq["lista_espera"] = False
        eq.setdefault("pagamento_status", "pendente")
        return ("ok", eq)
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Equipe não encontrada"}), 404
    if res[0] == "naoespera":
        return jsonify({"error": "Equipe não está na lista de espera"}), 400
    do_backup()
    out = {k: v for k, v in res[1].items() if k != "senha_hash"}
    out["senha"] = plain
    return jsonify({"ok": True, "equipe": out})

@app.route('/api/equipes/<equipe_id>', methods=['PUT'])
@admin_required
def update_equipe(equipe_id):
    """Edita dados cadastrais da equipe (nome/responsável/telefone). Não mexe em
    login/senha/pagamento — o login continua o original mesmo se o nome mudar."""
    body = request.json or {}
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if not eq:
            return None
        if "nome" in body:
            nome = (body.get("nome") or "").strip()
            if not nome:
                return ("invalido", "Nome não pode ficar vazio")
            if len(nome) > 80:
                return ("invalido", "Nome muito longo")
            eq["nome"] = nome
        if "responsavel" in body:
            eq["responsavel"] = (body.get("responsavel") or "").strip()[:80]
        if "telefone" in body:
            eq["telefone"] = (body.get("telefone") or "").strip()[:30]
        return ("ok", {k: v for k, v in eq.items() if k != "senha_hash"})
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Equipe não encontrada"}), 404
    if res[0] == "invalido":
        return jsonify({"error": res[1]}), 400
    do_backup()
    return jsonify({"ok": True, "equipe": res[1]})

@app.route('/api/equipes/<equipe_id>/mover', methods=['POST'])
@admin_required
def mover_equipe(equipe_id):
    """Move uma inscrição de um torneio para outro (body: {torneio_id_novo}).
    torneio_id_novo pode ser null/vazio para deixar a equipe órfã (sem torneio)."""
    body = request.json or {}
    novo = body.get("torneio_id_novo") or body.get("torneio_id") or None
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if not eq:
            return None
        if novo and not get_torneio(data, novo):
            return ("notorneio", None)
        eq["torneio_id"] = novo
        return ("ok", eq.get("nome"), novo)
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Equipe não encontrada"}), 404
    if res[0] == "notorneio":
        return jsonify({"error": "Torneio de destino inválido"}), 400
    return jsonify({"ok": True, "equipe": res[1], "torneio_id": res[2]})

@app.route('/api/equipes/<equipe_id>/pagamento', methods=['POST'])
@admin_required
def update_pagamento(equipe_id):
    body = request.json or {}
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if eq and body.get("pagamento_status") in ("aprovado", "pendente"):
            eq["pagamento_status"] = body["pagamento_status"]
        return {"ok": True}
    return jsonify(update_data(_do))

@app.route('/api/equipes/<equipe_id>/comprovante', methods=['POST'])
@team_or_admin_required
def upload_comprovante(equipe_id):
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Arquivo vazio"}), 400
    ext = _safe_ext(file.filename, ALLOWED_COMPROVANTE_EXT, 'jpg')
    filename = secure_filename(f"comprovante_{equipe_id}.{ext}")
    filepath = os.path.join(UPLOADS_DIR, filename)
    ensure_dirs()
    file.save(filepath)
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if eq:
            eq["comprovante"] = filename
        return {"ok": True, "filename": filename}
    return jsonify(update_data(_do))

@app.route('/api/uploads/<filename>')
def serve_upload(filename):
    # secure_filename guards directory traversal
    safe = secure_filename(filename)
    return send_from_directory(UPLOADS_DIR, safe)

# --- FOTOS (Team + MVP) ---
@app.route('/api/equipes/<equipe_id>/logo', methods=['POST'])
@team_or_admin_required
def upload_logo_equipe(equipe_id):
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Arquivo vazio"}), 400
    ensure_dirs()
    ext = _safe_ext(file.filename, ALLOWED_IMAGE_EXT, 'png')
    filename = secure_filename(f"logo_equipe_{equipe_id}.{ext}")
    filepath = os.path.join(UPLOADS_DIR, filename)
    try:
        img = Image.open(file.stream)
        # Detect format from PIL, not extension
        target_format = "PNG" if (img.format == "PNG" or ext == "png") else "JPEG"
        if target_format == "PNG":
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        img.thumbnail((200, 200), Image.LANCZOS)
        img.save(filepath, target_format, quality=85)
    except Exception as e:
        log.warning(f"logo upload resize failed, saving raw: {e}")
        try:
            file.stream.seek(0)
            file.save(filepath)
        except Exception as e2:
            log.error(f"logo raw save failed: {e2}")
            return jsonify({"error": "Falha no upload"}), 500
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if eq:
            eq["logo"] = filename
        return {"ok": True, "filename": filename}
    return jsonify(update_data(_do))

@app.route('/api/equipes/<equipe_id>/foto', methods=['POST'])
@admin_required
def upload_foto_equipe(equipe_id):
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Arquivo vazio"}), 400
    filename = save_photo_with_watermark(file, f"foto_equipe_{equipe_id}")
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if eq:
            eq["foto"] = filename
        return {"ok": True, "filename": filename}
    return jsonify(update_data(_do))

@app.route('/api/destaques', methods=['GET'])
def get_destaques():
    return jsonify(load_data().get("destaques", []))

@app.route('/api/destaques', methods=['POST'])
@admin_required
def add_destaque():
    filename = None
    if 'file' in request.files and request.files['file'].filename:
        filename = save_photo_with_watermark(request.files['file'], f"mvp_{uuid.uuid4().hex[:8]}")
    nome_atleta = (request.form.get("nome_atleta", "") or "").strip()[:80]
    equipe = (request.form.get("equipe", "") or "").strip()[:80]
    partida = (request.form.get("partida", "") or "").strip()[:120]
    naipe = request.form.get("naipe", "")
    naipe = naipe if naipe in ("masculino", "feminino") else ""
    def _do(data):
        if "destaques" not in data:
            data["destaques"] = []
        destaque = {
            "id": str(uuid.uuid4())[:8],
            "nome_atleta": nome_atleta,
            "equipe": equipe,
            "partida": partida,
            "naipe": naipe,
            "foto": filename,
            "created_at": datetime.now().isoformat()
        }
        data["destaques"].append(destaque)
        return destaque
    return jsonify(update_data(_do)), 201

@app.route('/api/destaques/<destaque_id>', methods=['DELETE'])
@admin_required
def delete_destaque(destaque_id):
    def _do(data):
        data["destaques"] = [d for d in data.get("destaques", []) if d["id"] != destaque_id]
        return {"ok": True}
    return jsonify(update_data(_do))

@app.route('/api/galeria', methods=['GET'])
def get_galeria():
    data = load_data()
    galeria = []
    for eq in data.get("equipes", []):
        if eq.get("is_test"):
            continue
        if eq.get("foto"):
            galeria.append({"tipo": "equipe", "nome": eq["nome"],
                            "naipe": naipe_do_equipe(data, eq) or "", "foto": eq["foto"]})
    for d in data.get("destaques", []):
        if d.get("foto"):
            galeria.append({"tipo": "mvp", "nome": d.get("nome_atleta", ""), "equipe": d.get("equipe", ""),
                "partida": d.get("partida", ""), "naipe": d.get("naipe", ""), "foto": d["foto"]})
    for f in data.get("fotos_gerais", []):
        if f.get("foto"):
            galeria.append({"tipo": "geral", "nome": f.get("titulo", ""), "naipe": f.get("naipe", ""),
                "foto": f["foto"], "id": f["id"]})
    return jsonify(galeria)

@app.route('/api/fotos-gerais', methods=['GET'])
def get_fotos_gerais():
    return jsonify(load_data().get("fotos_gerais", []))

@app.route('/api/fotos-gerais', methods=['POST'])
@admin_required
def add_foto_geral():
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({"error": "Nenhuma foto enviada"}), 400
    filename = save_photo_with_watermark(request.files['file'], f"geral_{uuid.uuid4().hex[:8]}")
    titulo = (request.form.get("titulo", "") or "").strip()[:80]
    naipe = request.form.get("naipe", "")
    def _do(data):
        if "fotos_gerais" not in data:
            data["fotos_gerais"] = []
        foto = {
            "id": str(uuid.uuid4())[:8],
            "titulo": titulo,
            "naipe": naipe,
            "foto": filename,
            "created_at": datetime.now().isoformat()
        }
        data["fotos_gerais"].append(foto)
        return foto
    return jsonify(update_data(_do)), 201

@app.route('/api/fotos-gerais/<foto_id>', methods=['DELETE'])
@admin_required
def delete_foto_geral(foto_id):
    def _do(data):
        data["fotos_gerais"] = [f for f in data.get("fotos_gerais", []) if f["id"] != foto_id]
        return {"ok": True}
    return jsonify(update_data(_do))

@app.route('/api/equipes/<equipe_id>/reset-senha', methods=['POST'])
@admin_required
def reset_equipe_senha(equipe_id):
    new_password = generate_team_password()
    out = {"found": False}
    def _do(data):
        eq = find_equipe(data, equipe_id)
        if eq:
            eq["senha_hash"] = generate_password_hash(new_password)
            out["found"] = True
            out["login"] = eq.get("login", "")
        return None
    update_data(_do)
    if not out["found"]:
        return jsonify({"error": "Equipe não encontrada"}), 404
    return jsonify({"ok": True, "nova_senha": new_password, "login": out["login"]})

@app.route('/api/equipes/<equipe_id>', methods=['DELETE'])
@admin_required
def delete_equipe(equipe_id):
    def _do(data):
        eq = find_equipe(data, equipe_id)
        tid = eq.get("torneio_id") if eq else None
        data["equipes"] = [e for e in data.get("equipes", []) if e.get("id") != equipe_id]
        # remove a equipe das listas de grupos do SEU torneio
        g = data.get("grupos", {}).get(tid) if tid else None
        if isinstance(g, dict):
            for grupo in ("A", "B"):
                if grupo in g:
                    g[grupo] = [eid for eid in g[grupo] if eid != equipe_id]
        if equipe_id in data.get("atletas", {}):
            del data["atletas"][equipe_id]
        return {"ok": True}
    res = update_data(_do)
    do_backup()
    return jsonify(res)

# --- ATLETAS ---
@app.route('/api/atletas/<equipe_id>', methods=['GET'])
def get_atletas(equipe_id):
    """Public: returns atletas WITHOUT sensitive doc numbers"""
    atletas = load_data().get("atletas", {}).get(equipe_id, [])
    public = []
    for a in atletas:
        pa = dict(a)
        doc = pa.get("numero_documento", "")
        if len(doc) > 3:
            pa["numero_documento"] = "***" + doc[-3:]
        public.append(pa)
    return jsonify(public)

@app.route('/api/atletas/<equipe_id>/full', methods=['GET'])
@team_or_admin_required
def get_atletas_full(equipe_id):
    return jsonify(load_data().get("atletas", {}).get(equipe_id, []))

@app.route('/api/atletas/<equipe_id>', methods=['POST'])
@team_or_admin_required
def add_atleta(equipe_id):
    body = request.json or {}
    nome = (body.get("nome_completo", "") or "").strip()[:120]
    if not nome:
        return jsonify({"error": "Nome obrigatório"}), 400
    # Block atletas if pagamento not approved (unless admin)
    if not session.get('is_admin'):
        data = load_data()
        eq = find_equipe(data, equipe_id)
        if not eq:
            return jsonify({"error": "Equipe não encontrada"}), 404
        if eq.get('pagamento_status') != 'aprovado':
            return jsonify({"error": "Aguardando aprovação do pagamento"}), 403
    def _do(data):
        if "atletas" not in data: data["atletas"] = {}
        if equipe_id not in data["atletas"]: data["atletas"][equipe_id] = []
        atleta = {
            "id": str(uuid.uuid4())[:8],
            "nome_completo": nome,
            "data_nascimento": (body.get("data_nascimento", "") or "").strip()[:10],
            "tipo_documento": (body.get("tipo_documento", "") or "").strip()[:8],
            "numero_documento": (body.get("numero_documento", "") or "").strip()[:30],
            "created_at": datetime.now().isoformat()
        }
        data["atletas"][equipe_id].append(atleta)
        return atleta
    return jsonify(update_data(_do)), 201

@app.route('/api/atletas/<equipe_id>/<atleta_id>', methods=['PUT'])
@team_or_admin_required
def update_atleta(equipe_id, atleta_id):
    """Edita dados de um atleta (nome/nascimento/documento) — correção de digitação.
    Mesma permissão do cadastro: a própria equipe ou admin."""
    body = request.json or {}
    def _do(data):
        for a in data.get("atletas", {}).get(equipe_id, []):
            if a.get("id") == atleta_id:
                if "nome_completo" in body:
                    nome = (body.get("nome_completo") or "").strip()
                    if not nome:
                        return ("invalido", "Nome não pode ficar vazio")
                    a["nome_completo"] = nome[:120]
                if "data_nascimento" in body:
                    a["data_nascimento"] = (body.get("data_nascimento") or "").strip()[:10]
                if "tipo_documento" in body:
                    a["tipo_documento"] = (body.get("tipo_documento") or "").strip()[:8]
                if "numero_documento" in body:
                    a["numero_documento"] = (body.get("numero_documento") or "").strip()[:30]
                return ("ok", a)
        return None
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Atleta não encontrado"}), 404
    if res[0] == "invalido":
        return jsonify({"error": res[1]}), 400
    do_backup()
    return jsonify({"ok": True, "atleta": res[1]})

@app.route('/api/atletas/<equipe_id>/<atleta_id>', methods=['DELETE'])
@team_or_admin_required
def delete_atleta(equipe_id, atleta_id):
    def _do(data):
        if equipe_id in data.get("atletas", {}):
            data["atletas"][equipe_id] = [a for a in data["atletas"][equipe_id] if a["id"] != atleta_id]
        return {"ok": True}
    return jsonify(update_data(_do))

# --- CARTELÃO ---
@app.route('/api/cartelao/<equipe_id>')
@team_or_admin_required
def gerar_cartelao(equipe_id):
    data = load_data()
    equipe = find_equipe(data, equipe_id)
    if not equipe:
        return jsonify({"error": "Equipe não encontrada"}), 404
    naipe = naipe_do_equipe(data, equipe) or ""
    atletas = data.get("atletas", {}).get(equipe_id, [])
    logo_b64 = ""
    logo_path = os.path.join(app.static_folder, 'logo.jpeg')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            logo_b64 = base64.b64encode(f.read()).decode()
    # Escape HTML in dynamic strings
    import html as _html
    def esc(s): return _html.escape(str(s or ''))
    rows = ""
    for i, a in enumerate(atletas):
        dn = a.get('data_nascimento', '')
        if dn and '-' in dn:
            try:
                p = dn.split('-'); dn = f"{p[2]}/{p[1]}/{p[0]}"
            except Exception:
                pass
        rows += (f'<tr><td class="n">{i+1}</td><td><b>{esc(a.get("nome_completo",""))}</b></td>'
                 f'<td>{esc(dn)}</td><td>{esc(a.get("tipo_documento","").upper())}</td>'
                 f'<td>{esc(a.get("numero_documento",""))}</td></tr>')
    nome_eq = esc(equipe['nome'])
    resp_eq = esc(equipe.get('responsavel',''))
    tel_eq = esc(equipe.get('telefone',''))
    naipe_up = esc(naipe.upper())
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Cartelão - {nome_eq}</title>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:'Barlow',sans-serif;padding:20px}}
.pg{{max-width:700px;margin:0 auto;border:3px solid #1a1a1a;border-radius:12px;overflow:hidden}}
.hd{{background:#1a1a1a;color:#fff;padding:20px;display:flex;align-items:center;gap:16px}}
.hd img{{width:70px;height:70px;border-radius:50%;border:2px solid #E31B23}}
.hd h1{{font-family:'Oswald',sans-serif;font-size:1.4rem;letter-spacing:2px;text-transform:uppercase}}
.hd h1 span{{color:#E31B23}}.hd h2{{font-family:'Oswald',sans-serif;font-size:1rem;color:#E31B23;margin-top:4px}}
.ti{{background:#E31B23;color:#fff;padding:14px 20px;font-family:'Oswald',sans-serif}}
.ti h3{{font-size:1.3rem;letter-spacing:2px;text-transform:uppercase}}.ti p{{font-size:.85rem;opacity:.9;margin-top:2px}}
.at{{padding:16px 20px}}.at h4{{font-family:'Oswald',sans-serif;font-size:.9rem;letter-spacing:1.5px;text-transform:uppercase;color:#71717a;margin-bottom:10px;border-bottom:2px solid #ebebed;padding-bottom:6px}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th{{font-family:'Oswald',sans-serif;font-weight:600;font-size:.7rem;letter-spacing:1px;text-transform:uppercase;color:#a1a1aa;padding:8px 6px;text-align:left;border-bottom:2px solid #ebebed}}
td{{padding:10px 6px;border-bottom:1px solid #f4f4f5}}tr:last-child td{{border-bottom:none}}.n{{font-family:'Oswald',sans-serif;font-weight:700;color:#E31B23}}
.ft{{background:#f7f7f8;padding:12px 20px;text-align:center;font-size:.75rem;color:#a1a1aa;border-top:1px solid #ebebed}}
.st{{display:inline-block;border:2px solid #1a1a1a;border-radius:6px;padding:6px 16px;margin-top:8px;font-family:'Oswald',sans-serif;font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:1px;color:#1a1a1a}}
@media print{{body{{padding:0}}.pg{{border:2px solid #000}}}}
</style></head><body>
<div class="pg">
<div class="hd"><img src="data:image/jpeg;base64,{logo_b64}" alt="Logo"><div><h1>Sampa Volleyball <span>League</span></h1><h2>Cartelão de Equipe</h2></div></div>
<div class="ti"><h3>{nome_eq}</h3><p>Naipe: {naipe_up} | Responsável: {resp_eq} | Tel: {tel_eq}</p></div>
<div class="at"><h4>Atletas Inscritos ({len(atletas)})</h4>
<table><tr><th>#</th><th>Nome Completo</th><th>Data Nasc.</th><th>Documento</th><th>Número</th></tr>{rows}</table></div>
<div class="ft"><p>Sampa Volleyball League — Temporada 2026</p><p>Apresentar antes de cada jogo para conferência.</p><div class="st">Visto da Organização: ________________</div></div>
</div><script>window.onload=function(){{window.print()}}</script></body></html>"""
    return make_response(html)

# --- GRUPOS (chaveados por torneio_id) ---
@app.route('/api/grupos/<torneio_id>', methods=['GET'])
def get_grupos(torneio_id):
    return jsonify(load_data()["grupos"].get(torneio_id, {"A": [], "B": []}))

@app.route('/api/grupos/<torneio_id>', methods=['POST'])
@admin_required
def set_grupos(torneio_id):
    body = request.json or {}
    def _do(data):
        data["grupos"][torneio_id] = {"A": list(body.get("A", [])), "B": list(body.get("B", []))}
        return data["grupos"][torneio_id]
    return jsonify(update_data(_do))

@app.route('/api/grupos/<torneio_id>/sorteio', methods=['POST'])
@admin_required
def sortear_grupos(torneio_id):
    def _do(data):
        torneio = get_torneio(data, torneio_id) or {}
        fmt = torneio.get("formato_jogos", "grupos")
        # Só equipes reais DESTE torneio entram no sorteio (teste e lista de espera nunca).
        ids = [e["id"] for e in data.get("equipes", [])
               if e.get("torneio_id") == torneio_id and not e.get("is_test") and not e.get("lista_espera")]
        random.shuffle(ids)
        # Formatos de grupo único (todos em A): hexagonal, triangular, quadrangular e pentagonal.
        if fmt in ("hexagonal", "quad_corrido", "quad_decisao", "tri_corrido", "tri_final",
                   "penta_corrido", "penta_decisao"):
            data["grupos"][torneio_id] = {"A": ids, "B": []}
        else:
            half = len(ids) // 2
            data["grupos"][torneio_id] = {"A": ids[:half], "B": ids[half:]}
        return data["grupos"][torneio_id]
    return jsonify(update_data(_do))

# --- JOGOS (chaveados por torneio_id) ---
@app.route('/api/jogos/<torneio_id>', methods=['GET'])
def get_jogos(torneio_id):
    jogos = load_data()["jogos"].get(torneio_id, [])
    return jsonify([j for j in jogos if not j.get("is_test")])

@app.route('/api/jogos/<torneio_id>/admin', methods=['GET'])
@admin_required
def get_jogos_admin(torneio_id):
    """Admin vê TODOS os jogos, incluindo is_test, pra poder operar o modo teste."""
    return jsonify(load_data()["jogos"].get(torneio_id, []))

@app.route('/api/jogos/<torneio_id>/gerar', methods=['POST'])
@admin_required
def gerar_jogos(torneio_id):
    def _do(data):
        torneio = get_torneio(data, torneio_id) or {}
        fmt = torneio.get("formato_jogos", "grupos")
        hora_inicio = torneio.get("hora_inicio", "08:30")
        intervalo_min = int(torneio.get("intervalo_min", 75))
        grupos = data["grupos"].get(torneio_id, {"A": [], "B": []})
        jogos = _build_jogos_from_grupos(grupos, fmt, is_test=False,
                                          hora_inicio=hora_inicio, intervalo_min=intervalo_min)
        # Preserva jogos de teste (is_test) que possam existir
        jogos_teste_existentes = [j for j in data["jogos"].get(torneio_id, []) if j.get("is_test")]
        data["jogos"][torneio_id] = jogos + jogos_teste_existentes
        return jogos
    return jsonify(update_data(_do))

@app.route('/api/jogos/<torneio_id>/atualizar-horarios', methods=['POST'])
@admin_required
def atualizar_horarios(torneio_id):
    """Aplica horários sequenciais (a partir da config do torneio) à tabela existente,
    SEM regerar os jogos. Mantém placares e ordem dos jogos."""
    def _do(data):
        torneio = get_torneio(data, torneio_id) or {}
        hora_inicio = torneio.get("hora_inicio", "08:30")
        intervalo_min = int(torneio.get("intervalo_min", 75))
        # Pega só jogos reais (não teste)
        reais = [j for j in data["jogos"].get(torneio_id, []) if not j.get("is_test")]
        testes = [j for j in data["jogos"].get(torneio_id, []) if j.get("is_test")]
        if not reais:
            return {"ok": False, "error": "Nenhuma tabela gerada ainda"}
        # Aplica na ordem atual (não reordena, só seta horário)
        _aplicar_horarios(reais, hora_inicio=hora_inicio, intervalo_min=intervalo_min)
        data["jogos"][torneio_id] = reais + testes
        return {"ok": True, "count": len(reais)}
    res = update_data(_do)
    return jsonify(res)


def _reorder_anti_sequencia(jogos):
    """Reordena uma lista de jogos da fase regular pra evitar ao máximo
    que uma equipe jogue duas vezes seguidas.
    Algoritmo guloso: pra cada slot, escolhe o melhor candidato dos pendentes:
      - Prefere jogo SEM nenhuma das equipes que jogaram no jogo anterior
      - Se todos têm pelo menos 1 conflito, prefere o de menor conflito
      - Desempata pelo descanso médio das equipes (menos recente joga primeiro)
    """
    if len(jogos) <= 2:
        return list(jogos)
    pendentes = list(jogos)
    ordem = []
    # Mapa: equipe_id -> índice do último jogo onde participou
    ultimo_jogo = {}
    while pendentes:
        if not ordem:
            # Primeiro jogo: pega o primeiro da lista (qualquer um serve)
            escolhido = pendentes[0]
        else:
            # Calcula score pra cada candidato. Quanto MAIOR, melhor.
            # +100 se nenhuma equipe estava no jogo anterior
            # +50 se só 1 equipe estava
            # 0 se ambas (péssimo)
            # +N onde N = soma das distâncias do último jogo de cada equipe
            ultimo = ordem[-1]
            ult_eqs = {ultimo["equipe_a"], ultimo["equipe_b"]}
            current_idx = len(ordem)
            def score(j):
                eqs = {j["equipe_a"], j["equipe_b"]}
                conflito = len(eqs & ult_eqs)
                base = (2 - conflito) * 100  # 0, 100 ou 200
                # Bonus: distância do último jogo de cada equipe
                dist = 0
                for eid in eqs:
                    if eid in ultimo_jogo:
                        dist += (current_idx - ultimo_jogo[eid])
                    else:
                        dist += 999  # equipe ainda não jogou — prioridade alta
                return base + dist
            # Escolhe o de maior score (estável: em empates, mantém ordem original)
            escolhido = max(pendentes, key=lambda j: (score(j), -pendentes.index(j)))
        pendentes.remove(escolhido)
        ordem.append(escolhido)
        ultimo_jogo[escolhido["equipe_a"]] = len(ordem) - 1
        ultimo_jogo[escolhido["equipe_b"]] = len(ordem) - 1
    return ordem

def _aplicar_horarios(jogos, hora_inicio="08:30", intervalo_min=75):
    """Aplica horários sequenciais a partir de hora_inicio, com intervalo de N minutos.
    hora_inicio formato 'HH:MM'. Modifica os jogos in-place adicionando campo 'horario'.
    Se passar de 23:59, faz roll-over silencioso (mostra como horário do dia seguinte)."""
    try:
        h, m = hora_inicio.split(":")
        total_min = int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        total_min = 8 * 60 + 30  # fallback 08:30
    for j in jogos:
        # Roll-over por segurança
        h = (total_min // 60) % 24
        m = total_min % 60
        j["horario"] = f"{h:02d}:{m:02d}"
        total_min += intervalo_min
    return jogos

def _build_jogos_from_grupos(grupos, fmt, is_test=False, hora_inicio="08:30", intervalo_min=75):
    """Gera lista de jogos a partir da estrutura de grupos.
    grupos = {"A": [eid1, eid2, ...], "B": [...]}
    fmt:
      - "hexagonal": todos contra todos em A + semis (1ºx4º, 2ºx3º) + 3º + final
      - "grupos": A e B (3+ cada) + semis cruzadas + 3º + final
      - "tri_corrido" / "quad_corrido" / "penta_corrido": todos contra todos em A (3/4/5
        equipes), sem eliminatória — campeão é o 1º colocado
      - "quad_decisao" / "penta_decisao": todos contra todos em A + disputa de 3º (3ºx4º)
        + final (1ºx2º)
      - "tri_final": todos contra todos em A (3 jogos) + final (1ºx2º), sem disputa de 3º
    Aplica algoritmo anti-sequência na fase regular + horários sequenciais.
    Retorna lista de jogos prontos pra inserir, na ordem cronológica.
    """
    base = {"is_test": is_test} if is_test else {}
    # Formatos "todos contra todos em A" (grupo único)
    fmt_grupo_unico = fmt in ("hexagonal", "quad_corrido", "quad_decisao",
                              "tri_corrido", "tri_final", "penta_corrido", "penta_decisao")
    # 1) Gera os jogos da fase regular (combinações)
    fase_regular = []
    if fmt_grupo_unico:
        eids = grupos.get("A", [])
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                fase_regular.append({"id": str(uuid.uuid4())[:8], "fase": "hexagonal", "grupo": "",
                    "equipe_a": eids[i], "equipe_b": eids[j], "sets_a": 0, "sets_b": 0,
                    "parciais": [], "finalizado": False, **base})
    else:
        # fmt == "grupos"
        for gn, eids in grupos.items():
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    fase_regular.append({"id": str(uuid.uuid4())[:8], "fase": "grupos", "grupo": gn,
                        "equipe_a": eids[i], "equipe_b": eids[j], "sets_a": 0, "sets_b": 0,
                        "parciais": [], "finalizado": False, **base})
    # 2) Reordena fase regular pra evitar jogos consecutivos da mesma equipe
    fase_regular = _reorder_anti_sequencia(fase_regular)
    # 3) Monta fase eliminatória conforme o formato
    eliminatoria = []
    if fmt == "hexagonal":
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 1: 1º x 4º",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 2: 2º x 3º",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "terceiro", "label": "Disputa 3º Lugar",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "final", "label": "Final",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
    elif fmt == "grupos":
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 1: 1ºA x 2ºB",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 2: 1ºB x 2ºA",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "terceiro", "label": "Disputa 3º Lugar",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "final", "label": "Final",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
    elif fmt in ("quad_decisao", "penta_decisao"):
        # Corrido com decisão: disputa de 3º (3ºx4º) + final (1ºx2º)
        # Disputa de 3º vem ANTES da final na ordem cronológica (igual hexagonal/grupos)
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "terceiro", "label": "Disputa 3º Lugar",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "final", "label": "Final: 1º x 2º",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
    elif fmt == "tri_final":
        # Triangular com final: só a final 1ºx2º (com 3 equipes não há disputa de 3º)
        eliminatoria.append({"id": str(uuid.uuid4())[:8], "fase": "final", "label": "Final: 1º x 2º",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0,
            "parciais": [], "finalizado": False, **base})
    # corridos (tri/quad/penta): sem eliminatória — campeão é o 1º colocado da fase regular
    # 4) Concatena: fase regular (reordenada) → eliminatória
    todos = fase_regular + eliminatoria
    # 5) Aplica horários sequenciais
    _aplicar_horarios(todos, hora_inicio=hora_inicio, intervalo_min=intervalo_min)
    return todos


@app.route('/api/jogos/<torneio_id>/<jogo_id>', methods=['PUT'])
@admin_required
def update_jogo(torneio_id, jogo_id):
    body = request.json or {}
    def _do(data):
        is_test_jogo = False
        for jogo in data["jogos"].get(torneio_id, []):
            if jogo["id"] == jogo_id:
                is_test_jogo = bool(jogo.get("is_test"))
                for k in ["sets_a","sets_b","parciais","finalizado"]:
                    if k in body: jogo[k] = body[k]
                if body.get("equipe_a") is not None: jogo["equipe_a"] = body["equipe_a"]
                if body.get("equipe_b") is not None: jogo["equipe_b"] = body["equipe_b"]
                # Se estiver finalizando manualmente, encerra o ao vivo
                if body.get("finalizado"):
                    jogo["em_andamento"] = False
                    jogo["set_atual"] = None
                break
        auto_classify_semis(data, torneio_id, test_mode=is_test_jogo)
        return {"ok": True}
    res = update_data(_do)
    do_backup()
    return jsonify(res)

# --- AO VIVO ---
# Filosofia: endpoints idempotentes (recebem estado, não incrementos).
# Classificação NÃO conta jogos não finalizados — auto_classify_semis ignora em_andamento.
# Múltiplos admins simultâneos: last-write-wins (ok pra placar manual a 1 click/seg).

def _find_jogo(data, torneio_id, jogo_id):
    for j in data["jogos"].get(torneio_id, []):
        if j["id"] == jogo_id:
            return j
    return None

@app.route('/api/jogos/<torneio_id>/<jogo_id>/iniciar', methods=['POST'])
@admin_required
def iniciar_jogo_aovivo(torneio_id, jogo_id):
    """Coloca o jogo em modo 'em andamento'. Reseta set_atual para próximo set.
    Body opcional: equipe_a, equipe_b (se ainda não definidos), lado_esq (id da equipe à esquerda)."""
    body = request.json or {}
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        if jogo.get("finalizado"):
            return {"error": "Jogo já finalizado. Reabra primeiro."}
        if body.get("equipe_a") is not None:
            jogo["equipe_a"] = body["equipe_a"]
        if body.get("equipe_b") is not None:
            jogo["equipe_b"] = body["equipe_b"]
        if not jogo.get("equipe_a") or not jogo.get("equipe_b"):
            return {"error": "Defina as equipes antes de iniciar"}
        jogo["em_andamento"] = True
        parciais = jogo.get("parciais") or []
        prox_set = len(parciais) + 1
        # Lado: se admin passou, usa. Senão, default = equipe_a à esquerda no set 1,
        # e segue regra de troca automática nos sets seguintes.
        lado_esq_in = body.get("lado_esq")
        if lado_esq_in and lado_esq_in in (jogo["equipe_a"], jogo["equipe_b"]):
            lado_esq = lado_esq_in
        else:
            # Sem input: define pelas regras
            set_lados = jogo.get("set_lados") or []
            if prox_set == 1:
                lado_esq = jogo["equipe_a"]
            elif prox_set == 2 and len(set_lados) >= 1:
                # Inverte do set 1
                ant = set_lados[0]
                lado_esq = jogo["equipe_b"] if ant == jogo["equipe_a"] else jogo["equipe_a"]
            elif prox_set >= 3 and len(set_lados) >= 2:
                # Default no tie-break: mantém igual ao set 2 (até admin confirmar/trocar via /lado)
                lado_esq = set_lados[1]
            else:
                lado_esq = jogo["equipe_a"]
        jogo["set_atual"] = {
            "numero": prox_set,
            "pontos_a": 0, "pontos_b": 0,
            "lado_esq": lado_esq,
            "troca_8_feita": False
        }
        jogo.setdefault("sets_a", 0)
        jogo.setdefault("sets_b", 0)
        jogo.setdefault("set_lados", [])
        return {"ok": True, "jogo": jogo}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/<jogo_id>/lado', methods=['POST'])
@admin_required
def trocar_lado_aovivo(torneio_id, jogo_id):
    """Troca o lado das equipes no set atual (admin pode invocar manualmente,
    ex: confirmar tie-break com lado diferente)."""
    body = request.json or {}
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        if not jogo.get("em_andamento") or not jogo.get("set_atual"):
            return {"error": "Jogo não está em andamento"}
        sa = jogo["set_atual"]
        novo_lado = body.get("lado_esq")
        if novo_lado not in (jogo["equipe_a"], jogo["equipe_b"]):
            return {"error": "lado_esq inválido"}
        sa["lado_esq"] = novo_lado
        return {"ok": True, "set_atual": sa}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/<jogo_id>/pontos', methods=['POST'])
@admin_required
def atualizar_pontos_aovivo(torneio_id, jogo_id):
    """Atualiza pontos do set atual. Body: {pontos_a, pontos_b}.
    Idempotente: recebe o estado, não incrementos.
    Tie-break (set 3+): troca automática de lado quando QUALQUER equipe atinge 8 pontos."""
    body = request.json or {}
    try:
        pa = int(body.get("pontos_a", 0))
        pb = int(body.get("pontos_b", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Pontos inválidos"}), 400
    pa = max(0, min(99, pa))
    pb = max(0, min(99, pb))
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        if not jogo.get("em_andamento"):
            return {"error": "Jogo não está em andamento"}
        if not jogo.get("set_atual"):
            jogo["set_atual"] = {
                "numero": (len(jogo.get("parciais") or []) + 1),
                "pontos_a": 0, "pontos_b": 0,
                "lado_esq": jogo.get("equipe_a"),
                "troca_8_feita": False
            }
        sa = jogo["set_atual"]
        sa["pontos_a"] = pa
        sa["pontos_b"] = pb
        # Tie-break: troca automática de lado quando alguém atinge 8 (e ainda não trocou)
        troca_acionada = False
        if sa.get("numero", 1) >= 3 and not sa.get("troca_8_feita") and (pa >= 8 or pb >= 8):
            # Inverte lado_esq
            sa["lado_esq"] = jogo["equipe_b"] if sa.get("lado_esq") == jogo["equipe_a"] else jogo["equipe_a"]
            sa["troca_8_feita"] = True
            # O placar é POR LADO da tela — na troca de quadra os pontos acompanham as
            # equipes, então os dois lados também se invertem (senão o placar troca de dono).
            sa["pontos_a"], sa["pontos_b"] = sa["pontos_b"], sa["pontos_a"]
            troca_acionada = True
        return {"ok": True, "set_atual": sa, "troca_acionada": troca_acionada}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/<jogo_id>/encerrar-set', methods=['POST'])
@admin_required
def encerrar_set_aovivo(torneio_id, jogo_id):
    """Fecha o set atual: registra na parcial, soma 1 set pra quem ganhou,
    abre o próximo set zerado (com troca automática de lado se for set 1→2)."""
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        if not jogo.get("em_andamento"):
            return {"error": "Jogo não está em andamento"}
        sa = jogo.get("set_atual") or {"numero": 1, "pontos_a": 0, "pontos_b": 0, "lado_esq": jogo.get("equipe_a")}
        pa = int(sa.get("pontos_a", 0))
        pb = int(sa.get("pontos_b", 0))
        if pa == pb:
            return {"error": "Empate. Ajuste o placar antes de encerrar o set."}
        # Registra a parcial sempre como "pontos_equipe_a-pontos_equipe_b"
        # (independente do lado da quadra, pra manter coerência histórica)
        lado_esq = sa.get("lado_esq") or jogo.get("equipe_a")
        if lado_esq == jogo.get("equipe_a"):
            pts_a, pts_b = pa, pb
        else:
            pts_a, pts_b = pb, pa
        parciais = list(jogo.get("parciais") or [])
        parciais.append(f"{pts_a}-{pts_b}")
        jogo["parciais"] = parciais
        # Registra o lado_esq do set encerrado (paralelo a parciais)
        set_lados = list(jogo.get("set_lados") or [])
        set_lados.append(lado_esq)
        jogo["set_lados"] = set_lados
        # Soma 1 set pra quem ganhou
        if pts_a > pts_b:
            jogo["sets_a"] = int(jogo.get("sets_a", 0)) + 1
        else:
            jogo["sets_b"] = int(jogo.get("sets_b", 0)) + 1
        # Abre próximo set
        prox = len(parciais) + 1
        # Set 1 → 2: troca automática
        if prox == 2:
            novo_lado_esq = jogo["equipe_b"] if lado_esq == jogo["equipe_a"] else jogo["equipe_a"]
        elif prox >= 3:
            # Default no tie-break: mantém igual ao set anterior (admin pode confirmar/trocar)
            novo_lado_esq = lado_esq
        else:
            novo_lado_esq = lado_esq
        jogo["set_atual"] = {
            "numero": prox,
            "pontos_a": 0, "pontos_b": 0,
            "lado_esq": novo_lado_esq,
            "troca_8_feita": False
        }
        return {"ok": True, "jogo": jogo}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/<jogo_id>/encerrar', methods=['POST'])
@admin_required
def encerrar_jogo_aovivo(torneio_id, jogo_id):
    """Finaliza a partida. Atualiza classificação."""
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        # 1) Calcula (sem aplicar ainda) a absorção do set em andamento, se houver placar
        sa = jogo.get("set_atual")
        absorver = None  # (pts_a, pts_b, lado_esq)
        if sa:
            pa = int(sa.get("pontos_a", 0))
            pb = int(sa.get("pontos_b", 0))
            if (pa > 0 or pb > 0) and pa != pb:
                lado_esq = sa.get("lado_esq") or jogo.get("equipe_a")
                if lado_esq == jogo.get("equipe_a"):
                    absorver = (pa, pb, lado_esq)
                else:
                    absorver = (pb, pa, lado_esq)
        # 2) Valida: vôlei não tem empate — bloqueia finalizar sem vencedor
        fs_a = int(jogo.get("sets_a", 0)) + (1 if absorver and absorver[0] > absorver[1] else 0)
        fs_b = int(jogo.get("sets_b", 0)) + (1 if absorver and absorver[1] > absorver[0] else 0)
        if fs_a == fs_b:
            return {"error": f"Não dá pra finalizar empatado em sets ({fs_a}x{fs_b}). "
                             "Encerre o set em andamento (ou ajuste o placar) até ter um vencedor."}
        # 3) Aplica a absorção e finaliza
        if absorver:
            pts_a, pts_b, lado_esq = absorver
            parciais = list(jogo.get("parciais") or [])
            parciais.append(f"{pts_a}-{pts_b}")
            jogo["parciais"] = parciais
            set_lados = list(jogo.get("set_lados") or [])
            set_lados.append(lado_esq)
            jogo["set_lados"] = set_lados
            if pts_a > pts_b:
                jogo["sets_a"] = int(jogo.get("sets_a", 0)) + 1
            else:
                jogo["sets_b"] = int(jogo.get("sets_b", 0)) + 1
        jogo["em_andamento"] = False
        jogo["set_atual"] = None
        jogo["finalizado"] = True
        auto_classify_semis(data, torneio_id, test_mode=bool(jogo.get("is_test")))
        return {"ok": True, "jogo": jogo}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    do_backup()
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/<jogo_id>/pausar', methods=['POST'])
@admin_required
def pausar_jogo_aovivo(torneio_id, jogo_id):
    """Sai do modo ao vivo, mantendo o que foi anotado.
    Pode retomar com /iniciar depois."""
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        jogo["em_andamento"] = False
        # Mantém set_atual pra se retomar pegar de onde parou
        return {"ok": True}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/<jogo_id>/parcial/<int:idx>', methods=['PUT'])
@admin_required
def editar_parcial(torneio_id, jogo_id, idx):
    """Edita uma parcial já fechada. Body: {pontos_a, pontos_b}.
    Recalcula sets_a/sets_b a partir das parciais."""
    body = request.json or {}
    try:
        pa = int(body.get("pontos_a", 0))
        pb = int(body.get("pontos_b", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Pontos inválidos"}), 400
    pa = max(0, min(99, pa))
    pb = max(0, min(99, pb))
    if pa == pb:
        return jsonify({"error": "Empate não permitido em set"}), 400
    def _do(data):
        jogo = _find_jogo(data, torneio_id, jogo_id)
        if not jogo:
            return {"error": "Jogo não encontrado"}
        parciais = list(jogo.get("parciais") or [])
        if idx < 0 or idx >= len(parciais):
            return {"error": "Parcial inexistente"}
        parciais[idx] = f"{pa}-{pb}"
        jogo["parciais"] = parciais
        # Recalcula sets_a / sets_b
        sa = sb = 0
        for p in parciais:
            try:
                a, b = p.split("-")
                a, b = int(a), int(b)
                if a > b: sa += 1
                else: sb += 1
            except Exception:
                continue
        jogo["sets_a"] = sa
        jogo["sets_b"] = sb
        # Se o jogo estava finalizado, mantém. Auto-reclassify caso seja semi/grupos
        if jogo.get("finalizado"):
            auto_classify_semis(data, torneio_id, test_mode=bool(jogo.get("is_test")))
        return {"ok": True, "jogo": jogo}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/jogos/<torneio_id>/aovivo', methods=['GET'])
def get_jogo_aovivo(torneio_id):
    """Endpoint público: retorna o jogo em andamento (se houver) com nomes das equipes.
    Jogos de teste (is_test) NUNCA aparecem aqui — espectador não enxerga."""
    data = load_data()
    emap = {e["id"]: {"nome": e["nome"], "logo": e.get("logo")}
            for e in data.get("equipes", []) if e.get("torneio_id") == torneio_id}
    for j in data["jogos"].get(torneio_id, []):
        if j.get("is_test"):
            continue
        if j.get("em_andamento"):
            out = dict(j)
            out["equipe_a_info"] = emap.get(j.get("equipe_a"), {"nome": "?", "logo": None})
            out["equipe_b_info"] = emap.get(j.get("equipe_b"), {"nome": "?", "logo": None})
            return jsonify(out)
    return jsonify(None)

# --- CLASSIFICAÇÃO ---
@app.route('/api/classificacao/<torneio_id>/<grupo>', methods=['GET'])
def get_classificacao(torneio_id, grupo):
    data = load_data()
    torneio = get_torneio(data, torneio_id) or {}
    fmt = torneio.get("formato_jogos", "grupos")
    eids = data["grupos"].get(torneio_id, {}).get(grupo, [])
    equipes_torneio = [e for e in data.get("equipes", []) if e.get("torneio_id") == torneio_id]
    # Filtra equipes de teste (defesa em profundidade)
    test_team_ids = {e["id"] for e in equipes_torneio if e.get("is_test")}
    eids = [eid for eid in eids if eid not in test_team_ids]
    jogos = data["jogos"].get(torneio_id, [])
    emap = {e["id"]: e["nome"] for e in equipes_torneio if not e.get("is_test")}
    # Tri/quad/penta usam estrutura idêntica ao hexagonal (todos contra todos em A)
    fmt_grupo_unico = fmt in ("hexagonal", "quad_corrido", "quad_decisao", "tri_corrido", "tri_final",
                              "penta_corrido", "penta_decisao")
    fase_filter = "hexagonal" if fmt_grupo_unico else "grupos"
    relevant_jogos = [j for j in jogos
                      if j.get("fase") == fase_filter
                      and (fmt_grupo_unico or j.get("grupo") == grupo)
                      and not j.get("is_test")]
    ranking = compute_ranking(eids, relevant_jogos, fase_filter)
    # Attach name
    for r in ranking:
        r["nome"] = emap.get(r["id"], "???")
    return jsonify(ranking)

# --- REGULAMENTO (default global + override por torneio) ---
@app.route('/api/regulamento', methods=['GET'])
def get_regulamento_default():
    return jsonify({"regulamento": load_data().get("regulamento_default", "")})

@app.route('/api/regulamento/<torneio_id>', methods=['GET'])
def get_regulamento(torneio_id):
    """Regulamento do torneio (se tiver), senão cai no default global."""
    data = load_data()
    t = get_torneio(data, torneio_id)
    texto = (t.get("regulamento") if t else "") or data.get("regulamento_default", "")
    return jsonify({"regulamento": texto})

@app.route('/api/regulamento/<torneio_id>', methods=['POST'])
@admin_required
def set_regulamento(torneio_id):
    body = request.json or {}
    def _do(data):
        t = get_torneio(data, torneio_id)
        if not t:
            return None
        t["regulamento"] = body.get("regulamento", "")
        return {"ok": True}
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Torneio não encontrado"}), 404
    return jsonify(res)

@app.route('/api/regulamento', methods=['POST'])
@admin_required
def set_regulamento_default():
    body = request.json or {}
    def _do(data):
        data["regulamento_default"] = body.get("regulamento", "")
        return {"ok": True}
    return jsonify(update_data(_do))

# --- BLOCKED IPs ---
@app.route('/api/blocked-ips', methods=['GET'])
@admin_required
def get_blocked_ips():
    now = datetime.now()
    blocked = []
    for ip, info in login_attempts.items():
        if info.get("locked_until") and now < info["locked_until"]:
            remaining = int((info["locked_until"] - now).total_seconds() / 60) + 1
            blocked.append({"ip": ip, "minutos_restantes": remaining})
    return jsonify(blocked)

@app.route('/api/blocked-ips/clear', methods=['POST'])
@admin_required
def clear_blocked_ips():
    login_attempts.clear()
    return jsonify({"ok": True})

# --- SETTINGS ---
@app.route('/api/settings', methods=['GET'])
def get_settings():
    data = load_data()
    return jsonify(data.get("settings", DEFAULT_DATA["settings"]))

@app.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    body = request.json or {}
    allowed_keys = set(DEFAULT_DATA["settings"].keys())
    def _do(data):
        if "settings" not in data:
            data["settings"] = dict(DEFAULT_DATA["settings"])
        for k, v in body.items():
            if k in allowed_keys and isinstance(v, (str, int, float)):
                data["settings"][k] = str(v)[:500]
        return data["settings"]
    return jsonify(update_data(_do))

# --- ADMIN PASSWORD CHANGE ---
@app.route('/api/admin-password', methods=['POST'])
@admin_required
def change_admin_password():
    body = request.json or {}
    current = body.get('current', '')
    new = body.get('new', '')
    if not new or len(new) < 6:
        return jsonify({"error": "Nova senha deve ter pelo menos 6 caracteres"}), 400
    data = load_data()
    if not check_password_hash(data.get('admin_password_hash', ''), current):
        return jsonify({"error": "Senha atual incorreta"}), 401
    def _do(d):
        d['admin_password_hash'] = generate_password_hash(new)
        return {"ok": True}
    return jsonify(update_data(_do))

# --- DASHBOARD ---
@app.route('/api/dashboard', methods=['GET'])
@admin_required
def get_dashboard():
    data = load_data()
    tid_naipe = {t["id"]: t.get("naipe") for t in data.get("torneios", [])}
    stats = {}
    for naipe in ["masculino", "feminino"]:
        equipes = [e for e in data.get("equipes", [])
                   if not e.get("is_test") and not e.get("lista_espera") and tid_naipe.get(e.get("torneio_id")) == naipe]
        atletas_count = sum(len(data.get("atletas", {}).get(e["id"], [])) for e in equipes)
        pagos = sum(1 for e in equipes if e.get("pagamento_status") == "aprovado")
        pendentes = sum(1 for e in equipes if e.get("pagamento_status", "pendente") == "pendente")
        # Jogos de TODOS os torneios do naipe
        jogos = []
        for t in data.get("torneios", []):
            if t.get("naipe") == naipe:
                jogos.extend(j for j in data["jogos"].get(t["id"], []) if not j.get("is_test"))
        jogos_feitos = sum(1 for j in jogos if j.get("finalizado"))
        jogos_total = len([j for j in jogos if j.get("fase") in ("grupos", "hexagonal")])
        stats[naipe] = {
            "equipes": len(equipes),
            "atletas": atletas_count,
            "pagos": pagos,
            "pendentes": pendentes,
            "jogos_feitos": jogos_feitos,
            "jogos_total": jogos_total,
            "torneios": sum(1 for t in data.get("torneios", []) if t.get("naipe") == naipe),
        }
    # Equipes órfãs (sem torneio) — precisam de atribuição manual pelo admin (caso QUEEN).
    orfas = [e for e in data.get("equipes", []) if not e.get("is_test") and not e.get("torneio_id")]
    stats["total_equipes"] = stats["masculino"]["equipes"] + stats["feminino"]["equipes"]
    stats["total_atletas"] = stats["masculino"]["atletas"] + stats["feminino"]["atletas"]
    stats["total_pagos"] = stats["masculino"]["pagos"] + stats["feminino"]["pagos"]
    stats["total_pendentes"] = stats["masculino"]["pendentes"] + stats["feminino"]["pendentes"]
    stats["sem_torneio"] = len(orfas)
    stats["total_torneios"] = len(data.get("torneios", []))
    return jsonify(stats)

# --- BACKUP ---
@app.route('/api/backup', methods=['POST'])
@admin_required
def create_backup():
    do_backup()
    return jsonify({"ok": True})

@app.route('/api/backups', methods=['GET'])
@admin_required
def list_backups():
    ensure_dirs()
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')], reverse=True)
    return jsonify(backups)

# --- RESET ---
@app.route('/api/reset', methods=['POST'])
@admin_required
def reset_data():
    do_backup()
    fresh = json.loads(json.dumps(DEFAULT_DATA))
    fresh['admin_password_hash'] = generate_password_hash(BOOTSTRAP_ADMIN_PASSWORD)
    save_data(fresh)
    return jsonify({"ok": True})

# ============ PATROCINADORES / APOIADORES ============
@app.route('/api/patrocinadores', methods=['GET'])
def get_patrocinadores():
    """Public list of sponsors/partners ordered by 'ordem' asc, then created_at."""
    data = load_data()
    items = list(data.get("patrocinadores", []))
    items.sort(key=lambda x: (x.get("ordem", 999), x.get("created_at", "")))
    # Optional filter by tipo
    tipo = request.args.get('tipo')
    if tipo:
        items = [i for i in items if i.get("tipo") == tipo]
    return jsonify(items)

@app.route('/api/patrocinadores', methods=['POST'])
@admin_required
def add_patrocinador():
    """Multipart form: nome, tipo (patrocinador|apoiador), url, ordem, file (logo)"""
    nome = (request.form.get("nome", "") or "").strip()[:80]
    tipo = request.form.get("tipo", "patrocinador")
    if tipo not in ("patrocinador", "apoiador"):
        tipo = "patrocinador"
    url = (request.form.get("url", "") or "").strip()[:300]
    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    try:
        ordem = int(request.form.get("ordem", "100") or "100")
    except (ValueError, TypeError):
        ordem = 100
    if not nome:
        return jsonify({"error": "Nome obrigatório"}), 400
    filename = None
    if 'file' in request.files and request.files['file'].filename:
        file = request.files['file']
        ensure_dirs()
        ext = _safe_ext(file.filename, ALLOWED_IMAGE_EXT, 'png')
        pid = uuid.uuid4().hex[:8]
        filename = secure_filename(f"patrocinador_{pid}.{ext}")
        filepath = os.path.join(UPLOADS_DIR, filename)
        try:
            img = Image.open(file.stream)
            target_format = "PNG" if (img.format == "PNG" or ext == "png") else "JPEG"
            if target_format == "PNG":
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
            img.thumbnail((400, 400), Image.LANCZOS)
            img.save(filepath, target_format, quality=88)
        except Exception as e:
            log.warning(f"sponsor logo resize failed: {e}")
            try:
                file.stream.seek(0); file.save(filepath)
            except Exception as e2:
                log.error(f"sponsor logo save failed: {e2}")
                return jsonify({"error": "Falha no upload"}), 500
    def _do(data):
        if "patrocinadores" not in data:
            data["patrocinadores"] = []
        item = {
            "id": str(uuid.uuid4())[:8],
            "nome": nome,
            "tipo": tipo,
            "url": url,
            "logo": filename,
            "ordem": ordem,
            "created_at": datetime.now().isoformat()
        }
        data["patrocinadores"].append(item)
        return item
    return jsonify(update_data(_do)), 201

@app.route('/api/patrocinadores/<pid>', methods=['PUT'])
@admin_required
def update_patrocinador(pid):
    body = request.json or {}
    def _do(data):
        for p in data.get("patrocinadores", []):
            if p["id"] == pid:
                if "nome" in body: p["nome"] = str(body["nome"])[:80]
                if "tipo" in body and body["tipo"] in ("patrocinador", "apoiador"): p["tipo"] = body["tipo"]
                if "url" in body:
                    u = str(body["url"])[:300].strip()
                    if u and not (u.startswith("http://") or u.startswith("https://")):
                        u = "https://" + u
                    p["url"] = u
                if "ordem" in body:
                    try: p["ordem"] = int(body["ordem"])
                    except (ValueError, TypeError): pass
                return {"ok": True}
        return {"error": "Não encontrado"}
    res = update_data(_do)
    if res.get("error"):
        return jsonify(res), 404
    return jsonify(res)

@app.route('/api/patrocinadores/<pid>', methods=['DELETE'])
@admin_required
def delete_patrocinador(pid):
    def _do(data):
        data["patrocinadores"] = [p for p in data.get("patrocinadores", []) if p["id"] != pid]
        return {"ok": True}
    return jsonify(update_data(_do))

# ============ MODO TESTE (sandbox isolado) ============
# Cria 2 equipes-fantasma + 1 jogo entre elas, todos com is_test=true.
# Tudo marcado is_test é INVISÍVEL em todos os endpoints públicos.
# Admin acessa via /api/test/state e usa fluxo Ao Vivo normal.

TEST_TORNEIO_ID = "__test__"  # Torneio reservado/oculto pro sandbox de teste (invisível ao público)

@app.route('/api/test/state', methods=['GET'])
@admin_required
def test_state():
    """Retorna estado do ambiente de teste:
    - cenario: 'sandbox' | 'grupos' | 'hexagonal' | None
    - equipes: lista das equipes de teste
    - jogos: lista dos jogos de teste com nomes resolvidos
    - grupos_test: estrutura de grupos pra cenario != 'sandbox'
    """
    data = load_data()
    cenario = data.get("test_meta", {}).get("cenario")  # None se não tiver
    test_equipes = [{"id": e["id"], "nome": e["nome"]} for e in data.get("equipes", []) if e.get("is_test")]
    emap = {e["id"]: e["nome"] for e in data.get("equipes", []) if e.get("is_test")}
    test_jogos = []
    for j in data["jogos"].get(TEST_TORNEIO_ID, []):
        if j.get("is_test"):
            test_jogos.append({
                "id": j["id"],
                "fase": j.get("fase"),
                "grupo": j.get("grupo", ""),
                "label": j.get("label", ""),
                "horario": j.get("horario", ""),
                "equipe_a_id": j.get("equipe_a"),
                "equipe_b_id": j.get("equipe_b"),
                "equipe_a_nome": emap.get(j.get("equipe_a"), "?") if j.get("equipe_a") else None,
                "equipe_b_nome": emap.get(j.get("equipe_b"), "?") if j.get("equipe_b") else None,
                "em_andamento": bool(j.get("em_andamento")),
                "finalizado": bool(j.get("finalizado")),
                "sets_a": j.get("sets_a", 0),
                "sets_b": j.get("sets_b", 0),
            })
    grupos_test = data.get("grupos_test", {}).get(TEST_TORNEIO_ID, {})
    return jsonify({
        # Ativo se há equipes de teste (o cenário 'sorteio' começa sem jogos de propósito)
        "active": bool(test_equipes),
        "cenario": cenario,
        "equipes": test_equipes,
        "jogos": test_jogos,
        "grupos": grupos_test,
    })

@app.route('/api/test/jogo', methods=['GET'])
@admin_required
def test_jogo():
    """Retorna o primeiro jogo de teste (sandbox) — usado pelo botão antigo."""
    data = load_data()
    for j in data["jogos"].get(TEST_TORNEIO_ID, []):
        if j.get("is_test"):
            return jsonify(j)
    return jsonify({"error": "Nenhum jogo de teste ativo"}), 404

def _create_test_team(idx, suffix=""):
    """Helper: cria objeto de equipe de teste."""
    return {
        "id": "test_" + uuid.uuid4().hex[:6],
        "nome": f"🧪 Time Teste {suffix or idx}",
        "responsavel": "Modo Teste",
        "telefone": "",
        "login": f"teste_{idx}_" + uuid.uuid4().hex[:4],
        "senha_hash": generate_password_hash("teste123"),
        "pagamento_status": "aprovado",
        "comprovante": None,
        "created_at": datetime.now().isoformat(),
        "is_test": True,
        "torneio_id": TEST_TORNEIO_ID,
    }

def _wipe_test_environment(data):
    """Limpa todo ambiente de teste (equipes is_test da lista única, jogos/grupos/meta do torneio de teste)."""
    test_eq_ids = {e["id"] for e in data.get("equipes", []) if e.get("is_test")}
    data["equipes"] = [e for e in data.get("equipes", []) if not e.get("is_test")]
    data.get("jogos", {}).pop(TEST_TORNEIO_ID, None)
    for eid in test_eq_ids:
        data.get("atletas", {}).pop(eid, None)
    for k in ("grupos_test", "test_meta", "test_config"):
        data.pop(k, None)

@app.route('/api/test/setup', methods=['POST'])
@admin_required
def test_setup():
    """Cenário sandbox: 2 equipes + 1 jogo direto. Pra ensaiar Ao Vivo."""
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        eq_a = _create_test_team("a", "A")
        eq_b = _create_test_team("b", "B")
        data["equipes"].append(eq_a)
        data["equipes"].append(eq_b)
        jogo = {
            "id": "test_" + uuid.uuid4().hex[:6],
            "fase": "teste",
            "grupo": "",
            "label": "🧪 JOGO DE TESTE — não conta na classificação",
            "equipe_a": eq_a["id"],
            "equipe_b": eq_b["id"],
            "sets_a": 0, "sets_b": 0,
            "parciais": [], "set_lados": [],
            "finalizado": False, "em_andamento": False, "set_atual": None,
            "is_test": True,
        }
        data["jogos"].setdefault(tid, []).append(jogo)
        data["test_meta"] = {"cenario": "sandbox", "torneio_id": tid}
        return {"torneio_id": tid, "jogo_id": jogo["id"], "equipe_a_id": eq_a["id"], "equipe_b_id": eq_b["id"]}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": "sandbox", **res})

@app.route('/api/test/setup-grupos', methods=['POST'])
@admin_required
def test_setup_grupos():
    """Cenário 2 grupos de 3: cria 6 equipes-fantasma, sorteia em A/B (3+3),
    gera tabela com fase de grupos + semis + final + 3º. Tudo is_test=true."""
    import random
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        # Cria 6 equipes
        equipes = [_create_test_team(str(i+1), str(i+1)) for i in range(6)]
        for eq in equipes:
            data["equipes"].append(eq)
        # Sorteia: 3 pra A, 3 pra B
        eids = [eq["id"] for eq in equipes]
        random.shuffle(eids)
        grupos = {"A": eids[:3], "B": eids[3:]}
        # Persiste em estrutura SEPARADA (grupos_test) — não toca nos grupos reais!
        data.setdefault("grupos_test", {})[tid] = grupos
        # Config de teste
        data["test_config"] = {"formato_jogos": "grupos"}
        # Gera jogos
        jogos = _build_jogos_from_grupos(grupos, "grupos", is_test=True)
        # Inicializa campos extras pra cada jogo
        for j in jogos:
            j["set_lados"] = []
            j["em_andamento"] = False
            j["set_atual"] = None
        data["jogos"].setdefault(tid, []).extend(jogos)
        data["test_meta"] = {"cenario": "grupos", "torneio_id": tid}
        return {"torneio_id": tid, "equipes_count": len(equipes), "jogos_count": len(jogos), "grupos": grupos}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": "grupos", **res})

@app.route('/api/test/setup-hexagonal', methods=['POST'])
@admin_required
def test_setup_hexagonal():
    """Cenário hexagonal: cria 6 equipes-fantasma todas em grupo único A,
    todos contra todos + semis (1ºx4º, 2ºx3º) + final + 3º. Tudo is_test=true."""
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        equipes = [_create_test_team(str(i+1), str(i+1)) for i in range(6)]
        for eq in equipes:
            data["equipes"].append(eq)
        eids = [eq["id"] for eq in equipes]
        grupos = {"A": eids, "B": []}
        data.setdefault("grupos_test", {})[tid] = grupos
        data["test_config"] = {"formato_jogos": "hexagonal"}
        jogos = _build_jogos_from_grupos(grupos, "hexagonal", is_test=True)
        for j in jogos:
            j["set_lados"] = []
            j["em_andamento"] = False
            j["set_atual"] = None
        data["jogos"].setdefault(tid, []).extend(jogos)
        data["test_meta"] = {"cenario": "hexagonal", "torneio_id": tid}
        return {"torneio_id": tid, "equipes_count": len(equipes), "jogos_count": len(jogos), "grupos": grupos}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": "hexagonal", **res})

@app.route('/api/test/setup-quad-corrido', methods=['POST'])
@admin_required
def test_setup_quad_corrido():
    """Cenário quadrangular pontos corridos: 4 equipes, todos contra todos = 6 jogos.
    Sem fase eliminatória — campeão é o 1º colocado."""
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        equipes = [_create_test_team(str(i+1), str(i+1)) for i in range(4)]
        for eq in equipes:
            data["equipes"].append(eq)
        eids = [eq["id"] for eq in equipes]
        grupos = {"A": eids, "B": []}
        data.setdefault("grupos_test", {})[tid] = grupos
        data["test_config"] = {"formato_jogos": "quad_corrido"}
        jogos = _build_jogos_from_grupos(grupos, "quad_corrido", is_test=True)
        for j in jogos:
            j["set_lados"] = []
            j["em_andamento"] = False
            j["set_atual"] = None
        data["jogos"].setdefault(tid, []).extend(jogos)
        data["test_meta"] = {"cenario": "quad_corrido", "torneio_id": tid}
        return {"torneio_id": tid, "equipes_count": len(equipes), "jogos_count": len(jogos), "grupos": grupos}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": "quad_corrido", **res})

@app.route('/api/test/setup-quad-decisao', methods=['POST'])
@admin_required
def test_setup_quad_decisao():
    """Cenário quadrangular com final: 4 equipes, todos contra todos = 6 jogos + final 1ºx2º = 7 jogos."""
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        equipes = [_create_test_team(str(i+1), str(i+1)) for i in range(4)]
        for eq in equipes:
            data["equipes"].append(eq)
        eids = [eq["id"] for eq in equipes]
        grupos = {"A": eids, "B": []}
        data.setdefault("grupos_test", {})[tid] = grupos
        data["test_config"] = {"formato_jogos": "quad_decisao"}
        jogos = _build_jogos_from_grupos(grupos, "quad_decisao", is_test=True)
        for j in jogos:
            j["set_lados"] = []
            j["em_andamento"] = False
            j["set_atual"] = None
        data["jogos"].setdefault(tid, []).extend(jogos)
        data["test_meta"] = {"cenario": "quad_decisao", "torneio_id": tid}
        return {"torneio_id": tid, "equipes_count": len(equipes), "jogos_count": len(jogos), "grupos": grupos}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": "quad_decisao", **res})

def _test_setup_grupo_unico(cenario, fmt, n_equipes):
    """Helper: monta cenário de teste de grupo único (todos em A) com N equipes no formato dado."""
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        equipes = [_create_test_team(str(i+1), str(i+1)) for i in range(n_equipes)]
        for eq in equipes:
            data["equipes"].append(eq)
        eids = [eq["id"] for eq in equipes]
        grupos = {"A": eids, "B": []}
        data.setdefault("grupos_test", {})[tid] = grupos
        data["test_config"] = {"formato_jogos": fmt}
        jogos = _build_jogos_from_grupos(grupos, fmt, is_test=True)
        for j in jogos:
            j["set_lados"] = []
            j["em_andamento"] = False
            j["set_atual"] = None
        data["jogos"].setdefault(tid, []).extend(jogos)
        data["test_meta"] = {"cenario": cenario, "torneio_id": tid}
        return {"torneio_id": tid, "equipes_count": len(equipes), "jogos_count": len(jogos), "grupos": grupos}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": cenario, **res})

@app.route('/api/test/setup-penta-decisao', methods=['POST'])
@admin_required
def test_setup_penta_decisao():
    """Cenário pentagonal com decisão: 5 equipes, 10 jogos + 3º + final = 12 jogos."""
    return _test_setup_grupo_unico("penta_decisao", "penta_decisao", 5)

@app.route('/api/test/setup-tri-final', methods=['POST'])
@admin_required
def test_setup_tri_final():
    """Cenário triangular com final: 3 equipes, 3 jogos + final 1ºx2º = 4 jogos."""
    return _test_setup_grupo_unico("tri_final", "tri_final", 3)

@app.route('/api/test/setup-sorteio', methods=['POST'])
@admin_required
def test_setup_sorteio():
    """Cenário SIMULAÇÃO DE SORTEIO: cria 6 equipes de teste SEM grupos e SEM jogos.
    O front conduz a revelação ao vivo e grava o resultado em /api/test/sorteio-confirmar."""
    tid = TEST_TORNEIO_ID
    def _do(data):
        _wipe_test_environment(data)
        equipes = [_create_test_team(str(i+1), str(i+1)) for i in range(6)]
        for eq in equipes:
            data["equipes"].append(eq)
        data["test_meta"] = {"cenario": "sorteio", "torneio_id": tid}
        return {"torneio_id": tid, "equipes_count": len(equipes)}
    res = update_data(_do)
    return jsonify({"ok": True, "cenario": "sorteio", **res})

@app.route('/api/test/sorteio-confirmar', methods=['POST'])
@admin_required
def test_sorteio_confirmar():
    """Confirma o resultado do sorteio simulado (body {A:[ids], B:[ids]}, 3+3, só equipes
    de teste) — grava grupos_test e gera a tabela (formato grupos) toda is_test."""
    body = request.json or {}
    A = list(body.get("A") or [])
    B = list(body.get("B") or [])
    tid = TEST_TORNEIO_ID
    def _do(data):
        test_ids = {e["id"] for e in data.get("equipes", []) if e.get("is_test")}
        if len(A) != 3 or len(B) != 3 or len(set(A + B)) != 6 or not set(A + B) <= test_ids:
            return None
        grupos = {"A": A, "B": B}
        data.setdefault("grupos_test", {})[tid] = grupos
        data["test_config"] = {"formato_jogos": "grupos"}
        jogos = _build_jogos_from_grupos(grupos, "grupos", is_test=True)
        for j in jogos:
            j["set_lados"] = []
            j["em_andamento"] = False
            j["set_atual"] = None
        data["jogos"][tid] = jogos  # torneio __test__ só contém jogos de teste
        # A partir daqui o ambiente vira o cenário 'grupos' normal (lista/classificação prontas)
        data["test_meta"] = {"cenario": "grupos", "torneio_id": tid}
        return {"jogos_count": len(jogos), "grupos": grupos}
    res = update_data(_do)
    if res is None:
        return jsonify({"error": "Sorteio inválido: precisa de 3+3 equipes de teste, sem repetição"}), 400
    return jsonify({"ok": True, **res})

@app.route('/api/test/teardown', methods=['POST'])
@admin_required
def test_teardown():
    """Remove TODO o ambiente de teste (equipes + jogos + grupos + meta)."""
    counts = {"equipes": 0, "jogos": 0}
    def _do(data):
        counts["equipes"] += sum(1 for e in data.get("equipes", []) if e.get("is_test"))
        counts["jogos"] += sum(1 for j in data["jogos"].get(TEST_TORNEIO_ID, []) if j.get("is_test"))
        _wipe_test_environment(data)
        return counts
    res = update_data(_do)
    return jsonify({"ok": True, **res})

# Endpoint pra classificação dos jogos de teste (admin only)
@app.route('/api/test/classificacao/<grupo>', methods=['GET'])
@admin_required
def test_classificacao(grupo):
    """Calcula classificação de teste — usa grupos_test[__test__], não os grupos reais."""
    data = load_data()
    tid = TEST_TORNEIO_ID
    fmt = data.get("test_config", {}).get("formato_jogos", "hexagonal")
    eids = data.get("grupos_test", {}).get(tid, {}).get(grupo, [])
    jogos_test = [j for j in data["jogos"].get(tid, []) if j.get("is_test")]
    emap = {e["id"]: e["nome"] for e in data.get("equipes", []) if e.get("is_test")}
    fmt_grupo_unico = fmt in ("hexagonal", "quad_corrido", "quad_decisao", "tri_corrido", "tri_final",
                              "penta_corrido", "penta_decisao")
    fase_filter = "hexagonal" if fmt_grupo_unico else "grupos"
    relevant = [j for j in jogos_test
                if j.get("fase") == fase_filter
                and (fmt_grupo_unico or j.get("grupo") == grupo)]
    ranking = compute_ranking(eids, relevant, fase_filter)
    for r in ranking:
        r["nome"] = emap.get(r["id"], "???")
    return jsonify(ranking)

# --- Health check ---
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "ts": datetime.now().isoformat()})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
