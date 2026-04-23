import json
import os
import uuid
import base64
import random
import hashlib
import shutil
import string
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, make_response

app = Flask(__name__, static_folder='static')

DATA_DIR = os.environ.get('DATA_DIR', '/data')
DATA_FILE = os.path.join(DATA_DIR, 'tournament.json')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
UPLOADS_DIR = os.path.join(DATA_DIR, 'uploads')

# Brute force protection
login_attempts = {}  # ip -> {"count": int, "locked_until": datetime}
MAX_ATTEMPTS = 5
LOCK_MINUTES = 15

DEFAULT_DATA = {
    "etapas": {"masculino": [], "feminino": []},
    "equipes": {"masculino": [], "feminino": []},
    "atletas": {},
    "config": {
        "masculino": {"max_equipes": 8, "formato_jogos": "grupos"},
        "feminino": {"max_equipes": 6, "formato_jogos": "hexagonal"}
    },
    "grupos": {"masculino": {"A": [], "B": []}, "feminino": {"A": [], "B": []}},
    "jogos": {"masculino": [], "feminino": []},
    "regulamento": {"masculino": "", "feminino": ""},
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
    "admin_password": "sampa2026"
}

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)

def load_data():
    ensure_dirs()
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for key in DEFAULT_DATA:
                if key not in data:
                    data[key] = DEFAULT_DATA[key]
            if "config" not in data:
                data["config"] = DEFAULT_DATA["config"]
            for n in ["masculino", "feminino"]:
                if n not in data["config"]:
                    data["config"][n] = DEFAULT_DATA["config"][n]
            return data
    return json.loads(json.dumps(DEFAULT_DATA))

def save_data(data):
    ensure_dirs()
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def do_backup():
    """Create a timestamped backup of the data file"""
    ensure_dirs()
    if os.path.exists(DATA_FILE):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(BACKUP_DIR, f'tournament_{ts}.json')
        shutil.copy2(DATA_FILE, backup_path)
        # Keep only last 30 backups
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')])
        while len(backups) > 30:
            os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))

def get_config(data, naipe):
    return data.get("config", {}).get(naipe, {"max_equipes": 8, "formato_jogos": "grupos"})

def generate_team_password():
    """Generate a readable password like SVL-4K7M2P"""
    chars = string.ascii_uppercase + string.digits
    code = ''.join(random.choices(chars, k=6))
    return f"SVL-{code}"

def generate_team_login(nome):
    """Generate a login from team name"""
    login = nome.lower().strip()
    login = login.replace(' ', '-').replace('/', '-').replace('.', '')
    # Remove accents simply
    for a, b in [('á','a'),('à','a'),('ã','a'),('â','a'),('é','e'),('ê','e'),('í','i'),('ó','o'),('ô','o'),('õ','o'),('ú','u'),('ç','c')]:
        login = login.replace(a, b)
    # Keep only alphanumeric and hyphens
    login = ''.join(c for c in login if c.isalnum() or c == '-')
    return login[:20]

def check_brute_force(ip):
    """Returns True if IP is locked out"""
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
    return request.headers.get('X-Forwarded-For', request.remote_addr or '0.0.0.0').split(',')[0].strip()

# --- Auto-classify semis ---
def auto_classify_semis(data, naipe):
    """When all group/hexagonal games are done, auto-fill semi teams"""
    cfg = get_config(data, naipe)
    fmt = cfg.get("formato_jogos", "grupos")
    jogos = data["jogos"].get(naipe, [])

    if fmt == "hexagonal":
        hex_jogos = [j for j in jogos if j["fase"] == "hexagonal"]
        if not hex_jogos or not all(j["finalizado"] for j in hex_jogos):
            return  # Not all games done
        # Get classification
        eids = data["grupos"].get(naipe, {}).get("A", [])
        ranking = compute_ranking(eids, hex_jogos, "hexagonal")
        if len(ranking) >= 4:
            for j in jogos:
                if j["fase"] == "semi" and "1º x 4º" in j.get("label", "") and not j["equipe_a"]:
                    j["equipe_a"] = ranking[0]["id"]
                    j["equipe_b"] = ranking[3]["id"]
                elif j["fase"] == "semi" and "2º x 3º" in j.get("label", "") and not j["equipe_a"]:
                    j["equipe_a"] = ranking[1]["id"]
                    j["equipe_b"] = ranking[2]["id"]
    else:
        grpA = [j for j in jogos if j["fase"] == "grupos" and j["grupo"] == "A"]
        grpB = [j for j in jogos if j["fase"] == "grupos" and j["grupo"] == "B"]
        if not grpA or not grpB:
            return
        if not all(j["finalizado"] for j in grpA) or not all(j["finalizado"] for j in grpB):
            return
        eidsA = data["grupos"].get(naipe, {}).get("A", [])
        eidsB = data["grupos"].get(naipe, {}).get("B", [])
        rankA = compute_ranking(eidsA, grpA, "grupos")
        rankB = compute_ranking(eidsB, grpB, "grupos")
        if len(rankA) >= 2 and len(rankB) >= 2:
            for j in jogos:
                if j["fase"] == "semi" and "1ºA" in j.get("label", "") and not j["equipe_a"]:
                    j["equipe_a"] = rankA[0]["id"]
                    j["equipe_b"] = rankB[1]["id"]
                elif j["fase"] == "semi" and "1ºB" in j.get("label", "") and not j["equipe_a"]:
                    j["equipe_a"] = rankB[0]["id"]
                    j["equipe_b"] = rankA[1]["id"]

    data["jogos"][naipe] = jogos

def compute_ranking(eids, jogos, fase):
    st = {}
    for eid in eids:
        st[eid] = {"id": eid, "jogos": 0, "vitorias": 0, "sets_pro": 0, "sets_contra": 0, "pontos": 0}
    for j in jogos:
        if not j.get("finalizado"):
            continue
        a, b, sa, sb = j["equipe_a"], j["equipe_b"], j["sets_a"], j["sets_b"]
        for t, sp, sc in [(a, sa, sb), (b, sb, sa)]:
            if t in st:
                st[t]["jogos"] += 1
                st[t]["sets_pro"] += sp
                st[t]["sets_contra"] += sc
                if sp > sc:
                    st[t]["vitorias"] += 1
                    st[t]["pontos"] += 3 if sc == 0 else 2
                else:
                    st[t]["pontos"] += 1 if sp > 0 else 0
    return sorted(st.values(), key=lambda x: (x["pontos"], x["sets_pro"] - x["sets_contra"], x["sets_pro"]), reverse=True)


# === ROUTES ===

@app.route('/')
def landing():
    return send_from_directory('static', 'landing.html')

@app.route('/app')
def apppage():
    return send_from_directory('static', 'index.html')

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
    if request.json.get('password') == data.get('admin_password', 'sampa2026'):
        clear_attempts(ip)
        return jsonify({"ok": True})

    record_failed_attempt(ip)
    attempts_left = MAX_ATTEMPTS - login_attempts.get(ip, {}).get("count", 0)
    if attempts_left <= 0:
        return jsonify({"ok": False, "error": f"Bloqueado por {LOCK_MINUTES} minutos."}), 429
    return jsonify({"ok": False, "error": f"Senha incorreta. {attempts_left} tentativa(s) restante(s)."}), 401

# --- AUTH (Equipe) ---
@app.route('/api/auth/equipe', methods=['POST'])
def auth_equipe():
    ip = get_client_ip()
    if check_brute_force(ip):
        remaining = login_attempts[ip]["locked_until"] - datetime.now()
        mins = max(1, int(remaining.total_seconds() / 60))
        return jsonify({"ok": False, "error": f"Bloqueado por {mins} minutos."}), 429

    data = load_data()
    body = request.json
    login = body.get("login", "").strip().lower()
    senha = body.get("senha", "").strip()

    for naipe in ["masculino", "feminino"]:
        for eq in data["equipes"].get(naipe, []):
            if eq.get("login") == login and eq.get("senha") == senha:
                clear_attempts(ip)
                return jsonify({"ok": True, "equipe_id": eq["id"], "naipe": naipe, "nome": eq["nome"],
                    "pagamento_status": eq.get("pagamento_status", "pendente")})

    record_failed_attempt(ip)
    attempts_left = MAX_ATTEMPTS - login_attempts.get(ip, {}).get("count", 0)
    if attempts_left <= 0:
        return jsonify({"ok": False, "error": f"Bloqueado por {LOCK_MINUTES} minutos."}), 429
    return jsonify({"ok": False, "error": f"Login ou senha incorretos. {attempts_left} tentativa(s)."}), 401

# --- CONFIG ---
@app.route('/api/config/<naipe>', methods=['GET'])
def get_config_route(naipe):
    return jsonify(get_config(load_data(), naipe))

@app.route('/api/config/<naipe>', methods=['POST'])
def set_config_route(naipe):
    data = load_data()
    body = request.json
    if "config" not in data:
        data["config"] = DEFAULT_DATA["config"]
    if naipe not in data["config"]:
        data["config"][naipe] = {}
    if "max_equipes" in body:
        data["config"][naipe]["max_equipes"] = int(body["max_equipes"])
    if "formato_jogos" in body:
        data["config"][naipe]["formato_jogos"] = body["formato_jogos"]
    save_data(data)
    return jsonify(data["config"][naipe])

# --- ETAPAS ---
@app.route('/api/etapas/<naipe>', methods=['GET'])
def get_etapas(naipe):
    return jsonify(load_data()["etapas"].get(naipe, []))

@app.route('/api/etapas/<naipe>', methods=['POST'])
def add_etapa(naipe):
    data = load_data()
    body = request.json
    etapa = {
        "id": str(uuid.uuid4())[:8], "nome": body.get("nome", ""),
        "local": body.get("local", ""), "data": body.get("data", ""),
        "endereco": body.get("endereco", ""), "categoria": body.get("categoria", ""),
        "formato": body.get("formato", ""), "horario": body.get("horario", ""),
        "created_at": datetime.now().isoformat()
    }
    data["etapas"][naipe].append(etapa)
    save_data(data)
    return jsonify(etapa), 201

@app.route('/api/etapas/<naipe>/<etapa_id>', methods=['PUT'])
def update_etapa(naipe, etapa_id):
    data = load_data()
    body = request.json
    for etapa in data["etapas"][naipe]:
        if etapa["id"] == etapa_id:
            for k in ["nome","local","data","endereco","categoria","formato","horario"]:
                if k in body: etapa[k] = body[k]
            break
    save_data(data)
    return jsonify({"ok": True})

@app.route('/api/etapas/<naipe>/<etapa_id>', methods=['DELETE'])
def delete_etapa(naipe, etapa_id):
    data = load_data()
    data["etapas"][naipe] = [e for e in data["etapas"][naipe] if e["id"] != etapa_id]
    save_data(data)
    return jsonify({"ok": True})

# --- EQUIPES ---
@app.route('/api/equipes/<naipe>', methods=['GET'])
def get_equipes(naipe):
    data = load_data()
    equipes = data["equipes"].get(naipe, [])
    # Strip sensitive fields for public view
    public = []
    for e in equipes:
        pe = {k: v for k, v in e.items() if k not in ("senha", "login")}
        public.append(pe)
    return jsonify(public)

@app.route('/api/equipes/<naipe>/admin', methods=['GET'])
def get_equipes_admin(naipe):
    """Admin view with all fields including login/senha/pagamento"""
    return jsonify(load_data()["equipes"].get(naipe, []))

@app.route('/api/equipes/<naipe>', methods=['POST'])
def add_equipe(naipe):
    data = load_data()
    equipes = data["equipes"].get(naipe, [])
    cfg = get_config(data, naipe)
    max_eq = cfg.get("max_equipes", 8)
    if len(equipes) >= max_eq:
        return jsonify({"error": f"Máximo de {max_eq} equipes atingido"}), 400
    body = request.json
    nome = body.get("nome", "").strip()
    login = generate_team_login(nome)
    # Ensure unique login
    existing_logins = [e.get("login", "") for e in equipes]
    base_login = login
    counter = 1
    while login in existing_logins:
        login = f"{base_login}-{counter}"
        counter += 1
    senha = generate_team_password()
    equipe = {
        "id": str(uuid.uuid4())[:8], "nome": nome,
        "responsavel": body.get("responsavel", ""), "telefone": body.get("telefone", ""),
        "login": login, "senha": senha,
        "pagamento_status": "pendente", "comprovante": None,
        "created_at": datetime.now().isoformat()
    }
    equipes.append(equipe)
    data["equipes"][naipe] = equipes
    save_data(data)
    do_backup()
    # Return with credentials (only on creation)
    return jsonify(equipe), 201

@app.route('/api/equipes/<naipe>/<equipe_id>/pagamento', methods=['POST'])
def update_pagamento(naipe, equipe_id):
    data = load_data()
    body = request.json
    for eq in data["equipes"].get(naipe, []):
        if eq["id"] == equipe_id:
            if "pagamento_status" in body:
                eq["pagamento_status"] = body["pagamento_status"]
            break
    save_data(data)
    return jsonify({"ok": True})

@app.route('/api/equipes/<naipe>/<equipe_id>/comprovante', methods=['POST'])
def upload_comprovante(naipe, equipe_id):
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Arquivo vazio"}), 400
    # Save file
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
    filename = f"comprovante_{naipe}_{equipe_id}.{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    file.save(filepath)
    # Update equipe
    data = load_data()
    for eq in data["equipes"].get(naipe, []):
        if eq["id"] == equipe_id:
            eq["comprovante"] = filename
            break
    save_data(data)
    return jsonify({"ok": True, "filename": filename})

@app.route('/api/uploads/<filename>')
def serve_upload(filename):
    return send_from_directory(UPLOADS_DIR, filename)

# --- FOTOS (Team + MVP) ---
@app.route('/api/equipes/<naipe>/<equipe_id>/foto', methods=['POST'])
def upload_foto_equipe(naipe, equipe_id):
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Arquivo vazio"}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
    filename = f"foto_equipe_{naipe}_{equipe_id}.{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    file.save(filepath)
    data = load_data()
    for eq in data["equipes"].get(naipe, []):
        if eq["id"] == equipe_id:
            eq["foto"] = filename
            break
    save_data(data)
    return jsonify({"ok": True, "filename": filename})

@app.route('/api/destaques', methods=['GET'])
def get_destaques():
    data = load_data()
    return jsonify(data.get("destaques", []))

@app.route('/api/destaques', methods=['POST'])
def add_destaque():
    data = load_data()
    if "destaques" not in data:
        data["destaques"] = []
    # Handle file upload
    filename = None
    if 'file' in request.files and request.files['file'].filename:
        file = request.files['file']
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
        filename = f"mvp_{uuid.uuid4().hex[:8]}.{ext}"
        file.save(os.path.join(UPLOADS_DIR, filename))
    destaque = {
        "id": str(uuid.uuid4())[:8],
        "nome_atleta": request.form.get("nome_atleta", ""),
        "equipe": request.form.get("equipe", ""),
        "partida": request.form.get("partida", ""),
        "naipe": request.form.get("naipe", ""),
        "foto": filename,
        "created_at": datetime.now().isoformat()
    }
    data["destaques"].append(destaque)
    save_data(data)
    return jsonify(destaque), 201

@app.route('/api/destaques/<destaque_id>', methods=['DELETE'])
def delete_destaque(destaque_id):
    data = load_data()
    data["destaques"] = [d for d in data.get("destaques", []) if d["id"] != destaque_id]
    save_data(data)
    return jsonify({"ok": True})

@app.route('/api/galeria', methods=['GET'])
def get_galeria():
    """Returns all photos: team photos + MVP highlights for the carousel"""
    data = load_data()
    galeria = []
    # Team photos
    for naipe in ["feminino", "masculino"]:
        for eq in data["equipes"].get(naipe, []):
            if eq.get("foto"):
                galeria.append({
                    "tipo": "equipe",
                    "nome": eq["nome"],
                    "naipe": naipe,
                    "foto": eq["foto"]
                })
    # MVP highlights
    for d in data.get("destaques", []):
        if d.get("foto"):
            galeria.append({
                "tipo": "mvp",
                "nome": d.get("nome_atleta", ""),
                "equipe": d.get("equipe", ""),
                "partida": d.get("partida", ""),
                "naipe": d.get("naipe", ""),
                "foto": d["foto"]
            })
    return jsonify(galeria)

@app.route('/api/equipes/<naipe>/<equipe_id>/reset-senha', methods=['POST'])
def reset_equipe_senha(naipe, equipe_id):
    data = load_data()
    for eq in data["equipes"].get(naipe, []):
        if eq["id"] == equipe_id:
            eq["senha"] = generate_team_password()
            save_data(data)
            return jsonify({"ok": True, "nova_senha": eq["senha"], "login": eq.get("login", "")})
    return jsonify({"error": "Equipe não encontrada"}), 404

@app.route('/api/equipes/<naipe>/<equipe_id>', methods=['DELETE'])
def delete_equipe(naipe, equipe_id):
    data = load_data()
    data["equipes"][naipe] = [e for e in data["equipes"][naipe] if e["id"] != equipe_id]
    for grupo in ["A", "B"]:
        if grupo in data.get("grupos", {}).get(naipe, {}):
            data["grupos"][naipe][grupo] = [eid for eid in data["grupos"][naipe][grupo] if eid != equipe_id]
    if equipe_id in data.get("atletas", {}):
        del data["atletas"][equipe_id]
    save_data(data)
    do_backup()
    return jsonify({"ok": True})

# --- ATLETAS ---
@app.route('/api/atletas/<equipe_id>', methods=['GET'])
def get_atletas(equipe_id):
    """Public: returns atletas WITHOUT sensitive doc numbers"""
    atletas = load_data().get("atletas", {}).get(equipe_id, [])
    public = []
    for a in atletas:
        pa = dict(a)
        # Mask document number: show only last 3 chars
        doc = pa.get("numero_documento", "")
        if len(doc) > 3:
            pa["numero_documento"] = "***" + doc[-3:]
        public.append(pa)
    return jsonify(public)

@app.route('/api/atletas/<equipe_id>/full', methods=['GET'])
def get_atletas_full(equipe_id):
    """Admin/Team owner: returns full atleta data"""
    return jsonify(load_data().get("atletas", {}).get(equipe_id, []))

@app.route('/api/atletas/<equipe_id>', methods=['POST'])
def add_atleta(equipe_id):
    data = load_data()
    # Check if team payment is approved (unless admin)
    # We'll handle this check on frontend for simplicity
    if "atletas" not in data: data["atletas"] = {}
    if equipe_id not in data["atletas"]: data["atletas"][equipe_id] = []
    body = request.json
    atleta = {
        "id": str(uuid.uuid4())[:8],
        "nome_completo": body.get("nome_completo", ""),
        "data_nascimento": body.get("data_nascimento", ""),
        "tipo_documento": body.get("tipo_documento", ""),
        "numero_documento": body.get("numero_documento", ""),
        "created_at": datetime.now().isoformat()
    }
    data["atletas"][equipe_id].append(atleta)
    save_data(data)
    return jsonify(atleta), 201

@app.route('/api/atletas/<equipe_id>/<atleta_id>', methods=['DELETE'])
def delete_atleta(equipe_id, atleta_id):
    data = load_data()
    if equipe_id in data.get("atletas", {}):
        data["atletas"][equipe_id] = [a for a in data["atletas"][equipe_id] if a["id"] != atleta_id]
    save_data(data)
    return jsonify({"ok": True})

# --- CARTELÃO ---
@app.route('/api/cartelao/<naipe>/<equipe_id>')
def gerar_cartelao(naipe, equipe_id):
    data = load_data()
    equipe = next((e for e in data["equipes"].get(naipe, []) if e["id"] == equipe_id), None)
    if not equipe:
        return jsonify({"error": "Equipe não encontrada"}), 404
    atletas = data.get("atletas", {}).get(equipe_id, [])
    logo_b64 = ""
    logo_path = os.path.join(app.static_folder, 'logo.jpeg')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            logo_b64 = base64.b64encode(f.read()).decode()
    rows = ""
    for i, a in enumerate(atletas):
        dn = a.get('data_nascimento', '')
        if dn and '-' in dn:
            try: p = dn.split('-'); dn = f"{p[2]}/{p[1]}/{p[0]}"
            except: pass
        rows += f'<tr><td class="n">{i+1}</td><td><b>{a.get("nome_completo","")}</b></td><td>{dn}</td><td>{a.get("tipo_documento","").upper()}</td><td>{a.get("numero_documento","")}</td></tr>'
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Cartelão - {equipe['nome']}</title>
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
<div class="ti"><h3>{equipe['nome']}</h3><p>Naipe: {naipe.upper()} | Responsável: {equipe.get('responsavel','')} | Tel: {equipe.get('telefone','')}</p></div>
<div class="at"><h4>Atletas Inscritos ({len(atletas)})</h4>
<table><tr><th>#</th><th>Nome Completo</th><th>Data Nasc.</th><th>Documento</th><th>Número</th></tr>{rows}</table></div>
<div class="ft"><p>Sampa Volleyball League — Temporada 2026</p><p>Apresentar antes de cada jogo para conferência.</p><div class="st">Visto da Organização: ________________</div></div>
</div><script>window.onload=function(){{window.print()}}</script></body></html>"""
    return make_response(html)

# --- GRUPOS ---
@app.route('/api/grupos/<naipe>', methods=['GET'])
def get_grupos(naipe):
    return jsonify(load_data()["grupos"].get(naipe, {"A": [], "B": []}))

@app.route('/api/grupos/<naipe>', methods=['POST'])
def set_grupos(naipe):
    data = load_data()
    data["grupos"][naipe] = request.json
    save_data(data)
    return jsonify(data["grupos"][naipe])

@app.route('/api/grupos/<naipe>/sorteio', methods=['POST'])
def sortear_grupos(naipe):
    data = load_data()
    cfg = get_config(data, naipe)
    fmt = cfg.get("formato_jogos", "grupos")
    ids = [e["id"] for e in data["equipes"].get(naipe, [])]
    random.shuffle(ids)
    if fmt == "hexagonal":
        data["grupos"][naipe] = {"A": ids, "B": []}
    else:
        half = len(ids) // 2
        data["grupos"][naipe] = {"A": ids[:half], "B": ids[half:]}
    save_data(data)
    return jsonify(data["grupos"][naipe])

# --- JOGOS ---
@app.route('/api/jogos/<naipe>', methods=['GET'])
def get_jogos(naipe):
    return jsonify(load_data()["jogos"].get(naipe, []))

@app.route('/api/jogos/<naipe>/gerar', methods=['POST'])
def gerar_jogos(naipe):
    data = load_data()
    cfg = get_config(data, naipe)
    fmt = cfg.get("formato_jogos", "grupos")
    grupos = data["grupos"].get(naipe, {"A": [], "B": []})
    jogos = []
    if fmt == "hexagonal":
        eids = grupos.get("A", [])
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                jogos.append({"id": str(uuid.uuid4())[:8], "fase": "hexagonal", "grupo": "",
                    "equipe_a": eids[i], "equipe_b": eids[j], "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
        jogos.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 1: 1º x 4º",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
        jogos.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 2: 2º x 3º",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
    else:
        for gn, eids in grupos.items():
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    jogos.append({"id": str(uuid.uuid4())[:8], "fase": "grupos", "grupo": gn,
                        "equipe_a": eids[i], "equipe_b": eids[j], "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
        jogos.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 1: 1ºA x 2ºB",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
        jogos.append({"id": str(uuid.uuid4())[:8], "fase": "semi", "label": "Semi 2: 1ºB x 2ºA",
            "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
    jogos.append({"id": str(uuid.uuid4())[:8], "fase": "final", "label": "Final",
        "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
    jogos.append({"id": str(uuid.uuid4())[:8], "fase": "terceiro", "label": "Disputa 3º Lugar",
        "equipe_a": None, "equipe_b": None, "sets_a": 0, "sets_b": 0, "parciais": [], "finalizado": False})
    data["jogos"][naipe] = jogos
    save_data(data)
    return jsonify(jogos)

@app.route('/api/jogos/<naipe>/<jogo_id>', methods=['PUT'])
def update_jogo(naipe, jogo_id):
    data = load_data()
    body = request.json
    for jogo in data["jogos"].get(naipe, []):
        if jogo["id"] == jogo_id:
            for k in ["sets_a","sets_b","parciais","finalizado"]:
                if k in body: jogo[k] = body[k]
            if body.get("equipe_a") is not None: jogo["equipe_a"] = body["equipe_a"]
            if body.get("equipe_b") is not None: jogo["equipe_b"] = body["equipe_b"]
            break
    # Auto-classify semis after updating a game
    auto_classify_semis(data, naipe)
    save_data(data)
    do_backup()
    return jsonify({"ok": True})

# --- CLASSIFICAÇÃO ---
@app.route('/api/classificacao/<naipe>/<grupo>', methods=['GET'])
def get_classificacao(naipe, grupo):
    data = load_data()
    cfg = get_config(data, naipe)
    fmt = cfg.get("formato_jogos", "grupos")
    eids = data["grupos"].get(naipe, {}).get(grupo, [])
    jogos = data["jogos"].get(naipe, [])
    emap = {e["id"]: e["nome"] for e in data["equipes"].get(naipe, [])}
    fase_filter = "hexagonal" if fmt == "hexagonal" else "grupos"
    st = {eid: {"id":eid,"nome":emap.get(eid,"???"),"jogos":0,"vitorias":0,"derrotas":0,"sets_pro":0,"sets_contra":0,"pontos":0} for eid in eids}
    for j in jogos:
        if j.get("fase") != fase_filter or not j.get("finalizado"):
            continue
        if fmt == "grupos" and j.get("grupo") != grupo:
            continue
        a, b, sa, sb = j["equipe_a"], j["equipe_b"], j["sets_a"], j["sets_b"]
        for t, sp, sc in [(a, sa, sb), (b, sb, sa)]:
            if t in st:
                st[t]["jogos"] += 1; st[t]["sets_pro"] += sp; st[t]["sets_contra"] += sc
                if sp > sc: st[t]["vitorias"] += 1; st[t]["pontos"] += 3 if sc == 0 else 2
                else: st[t]["derrotas"] += 1; st[t]["pontos"] += 1 if sp > 0 else 0
    return jsonify(sorted(st.values(), key=lambda x: (x["pontos"], x["sets_pro"] - x["sets_contra"], x["sets_pro"]), reverse=True))

# --- REGULAMENTO (per naipe) ---
@app.route('/api/regulamento', methods=['GET'])
def get_regulamento_default():
    """Backward compatible: returns feminino by default"""
    data = load_data()
    reg = data.get("regulamento", "")
    # Migrate old format (single string) to new format (per naipe)
    if isinstance(reg, str):
        return jsonify({"regulamento": reg})
    return jsonify({"regulamento": reg.get("feminino", "")})

@app.route('/api/regulamento/<naipe>', methods=['GET'])
def get_regulamento(naipe):
    data = load_data()
    reg = data.get("regulamento", "")
    if isinstance(reg, str):
        # Old format - return same text for both
        return jsonify({"regulamento": reg})
    return jsonify({"regulamento": reg.get(naipe, "")})

@app.route('/api/regulamento/<naipe>', methods=['POST'])
def set_regulamento(naipe):
    data = load_data()
    reg = data.get("regulamento", "")
    # Migrate old format if needed
    if isinstance(reg, str):
        old_text = reg
        data["regulamento"] = {"masculino": old_text, "feminino": old_text}
    data["regulamento"][naipe] = request.json.get("regulamento", "")
    save_data(data)
    return jsonify({"ok": True})

@app.route('/api/regulamento', methods=['POST'])
def set_regulamento_default():
    """Backward compatible"""
    data = load_data()
    reg = data.get("regulamento", "")
    if isinstance(reg, str):
        data["regulamento"] = {"masculino": "", "feminino": ""}
    data["regulamento"]["feminino"] = request.json.get("regulamento", "")
    save_data(data)
    return jsonify({"ok": True})

# --- SETTINGS ---
@app.route('/api/settings', methods=['GET'])
def get_settings():
    data = load_data()
    return jsonify(data.get("settings", DEFAULT_DATA["settings"]))

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = load_data()
    if "settings" not in data:
        data["settings"] = dict(DEFAULT_DATA["settings"])
    body = request.json
    for k in body:
        data["settings"][k] = body[k]
    save_data(data)
    return jsonify(data["settings"])

# --- DASHBOARD ---
@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    data = load_data()
    stats = {}
    for naipe in ["masculino", "feminino"]:
        equipes = data["equipes"].get(naipe, [])
        atletas_count = sum(len(data.get("atletas", {}).get(e["id"], [])) for e in equipes)
        pagos = sum(1 for e in equipes if e.get("pagamento_status") == "aprovado")
        pendentes = sum(1 for e in equipes if e.get("pagamento_status", "pendente") == "pendente")
        jogos = data["jogos"].get(naipe, [])
        jogos_feitos = sum(1 for j in jogos if j.get("finalizado"))
        jogos_total = len([j for j in jogos if j.get("fase") in ("grupos", "hexagonal")])
        stats[naipe] = {
            "equipes": len(equipes),
            "atletas": atletas_count,
            "pagos": pagos,
            "pendentes": pendentes,
            "jogos_feitos": jogos_feitos,
            "jogos_total": jogos_total
        }
    stats["total_equipes"] = stats["masculino"]["equipes"] + stats["feminino"]["equipes"]
    stats["total_atletas"] = stats["masculino"]["atletas"] + stats["feminino"]["atletas"]
    stats["total_pagos"] = stats["masculino"]["pagos"] + stats["feminino"]["pagos"]
    stats["total_pendentes"] = stats["masculino"]["pendentes"] + stats["feminino"]["pendentes"]
    return jsonify(stats)

# --- BACKUP ---
@app.route('/api/backup', methods=['POST'])
def create_backup():
    do_backup()
    return jsonify({"ok": True})

@app.route('/api/backups', methods=['GET'])
def list_backups():
    ensure_dirs()
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')], reverse=True)
    return jsonify(backups)

# --- RESET ---
@app.route('/api/reset', methods=['POST'])
def reset_data():
    do_backup()
    save_data(json.loads(json.dumps(DEFAULT_DATA)))
    return jsonify({"ok": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
