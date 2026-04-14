import json
import os
import uuid
import base64
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, make_response

app = Flask(__name__, static_folder='static')

DATA_DIR = os.environ.get('DATA_DIR', '/data')
DATA_FILE = os.path.join(DATA_DIR, 'tournament.json')

DEFAULT_DATA = {
    "etapas": {"masculino": [], "feminino": []},
    "equipes": {"masculino": [], "feminino": []},
    "atletas": {},
    "grupos": {"masculino": {"A": [], "B": []}, "feminino": {"A": [], "B": []}},
    "jogos": {"masculino": [], "feminino": []},
    "regulamento": "",
    "admin_password": "sampa2026"
}

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_data():
    ensure_data_dir()
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for key in DEFAULT_DATA:
                if key not in data:
                    data[key] = DEFAULT_DATA[key]
            return data
    return json.loads(json.dumps(DEFAULT_DATA))

def save_data(data):
    ensure_data_dir()
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

# --- AUTH ---
@app.route('/api/auth', methods=['POST'])
def auth():
    data = load_data()
    if request.json.get('password') == data.get('admin_password', 'sampa2026'):
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401

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
        "formato": body.get("formato", ""), "created_at": datetime.now().isoformat()
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
            for k in ["nome","local","data","endereco","categoria","formato"]:
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
    return jsonify(load_data()["equipes"].get(naipe, []))

@app.route('/api/equipes/<naipe>', methods=['POST'])
def add_equipe(naipe):
    data = load_data()
    equipes = data["equipes"].get(naipe, [])
    if len(equipes) >= 8:
        return jsonify({"error": "Máximo de 8 equipes atingido"}), 400
    body = request.json
    equipe = {
        "id": str(uuid.uuid4())[:8], "nome": body.get("nome", ""),
        "responsavel": body.get("responsavel", ""), "telefone": body.get("telefone", ""),
        "created_at": datetime.now().isoformat()
    }
    equipes.append(equipe)
    data["equipes"][naipe] = equipes
    save_data(data)
    return jsonify(equipe), 201

@app.route('/api/equipes/<naipe>/<equipe_id>', methods=['DELETE'])
def delete_equipe(naipe, equipe_id):
    data = load_data()
    data["equipes"][naipe] = [e for e in data["equipes"][naipe] if e["id"] != equipe_id]
    for grupo in ["A", "B"]:
        data["grupos"][naipe][grupo] = [eid for eid in data["grupos"][naipe][grupo] if eid != equipe_id]
    if equipe_id in data.get("atletas", {}):
        del data["atletas"][equipe_id]
    save_data(data)
    return jsonify({"ok": True})

# --- ATLETAS ---
@app.route('/api/atletas/<equipe_id>', methods=['GET'])
def get_atletas(equipe_id):
    return jsonify(load_data().get("atletas", {}).get(equipe_id, []))

@app.route('/api/atletas/<equipe_id>', methods=['POST'])
def add_atleta(equipe_id):
    data = load_data()
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
    body = request.json
    data["grupos"][naipe] = {"A": body.get("A", []), "B": body.get("B", [])}
    save_data(data)
    return jsonify(data["grupos"][naipe])

@app.route('/api/grupos/<naipe>/sorteio', methods=['POST'])
def sortear_grupos(naipe):
    import random
    data = load_data()
    ids = [e["id"] for e in data["equipes"].get(naipe, [])]
    random.shuffle(ids)
    half = len(ids) // 2
    data["grupos"][naipe] = {"A": ids[:min(half,4)], "B": ids[half:half+4]}
    save_data(data)
    return jsonify(data["grupos"][naipe])

# --- JOGOS ---
@app.route('/api/jogos/<naipe>', methods=['GET'])
def get_jogos(naipe):
    return jsonify(load_data()["jogos"].get(naipe, []))

@app.route('/api/jogos/<naipe>/gerar', methods=['POST'])
def gerar_jogos(naipe):
    data = load_data()
    grupos = data["grupos"].get(naipe, {"A": [], "B": []})
    jogos = []
    for gn, eids in grupos.items():
        for i in range(len(eids)):
            for j in range(i+1, len(eids)):
                jogos.append({"id":str(uuid.uuid4())[:8],"fase":"grupos","grupo":gn,"equipe_a":eids[i],"equipe_b":eids[j],"sets_a":0,"sets_b":0,"parciais":[],"finalizado":False})
    for lb in ["Semi 1: 1ºA x 2ºB","Semi 2: 1ºB x 2ºA"]:
        jogos.append({"id":str(uuid.uuid4())[:8],"fase":"semi","label":lb,"equipe_a":None,"equipe_b":None,"sets_a":0,"sets_b":0,"parciais":[],"finalizado":False})
    jogos.append({"id":str(uuid.uuid4())[:8],"fase":"final","label":"Final","equipe_a":None,"equipe_b":None,"sets_a":0,"sets_b":0,"parciais":[],"finalizado":False})
    jogos.append({"id":str(uuid.uuid4())[:8],"fase":"terceiro","label":"Disputa 3º Lugar","equipe_a":None,"equipe_b":None,"sets_a":0,"sets_b":0,"parciais":[],"finalizado":False})
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
    save_data(data)
    return jsonify({"ok": True})

# --- CLASSIFICAÇÃO ---
@app.route('/api/classificacao/<naipe>/<grupo>', methods=['GET'])
def get_classificacao(naipe, grupo):
    data = load_data()
    eids = data["grupos"].get(naipe, {}).get(grupo, [])
    jogos = data["jogos"].get(naipe, [])
    emap = {e["id"]: e["nome"] for e in data["equipes"].get(naipe, [])}
    st = {eid: {"id":eid,"nome":emap.get(eid,"???"),"jogos":0,"vitorias":0,"derrotas":0,"sets_pro":0,"sets_contra":0,"pontos":0} for eid in eids}
    for j in jogos:
        if j.get("fase")!="grupos" or j.get("grupo")!=grupo or not j.get("finalizado"): continue
        a,b,sa,sb = j["equipe_a"],j["equipe_b"],j["sets_a"],j["sets_b"]
        for t,sp,sc in [(a,sa,sb),(b,sb,sa)]:
            if t in st:
                st[t]["jogos"]+=1; st[t]["sets_pro"]+=sp; st[t]["sets_contra"]+=sc
                if sp>sc: st[t]["vitorias"]+=1; st[t]["pontos"]+=3 if sc==0 else 2
                else: st[t]["derrotas"]+=1; st[t]["pontos"]+=1 if sp>0 else 0
    return jsonify(sorted(st.values(), key=lambda x:(x["pontos"],x["sets_pro"]-x["sets_contra"],x["sets_pro"]), reverse=True))

# --- REGULAMENTO ---
@app.route('/api/regulamento', methods=['GET'])
def get_regulamento():
    return jsonify({"regulamento": load_data().get("regulamento", "")})

@app.route('/api/regulamento', methods=['POST'])
def set_regulamento():
    data = load_data()
    data["regulamento"] = request.json.get("regulamento", "")
    save_data(data)
    return jsonify({"ok": True})

# --- RESET ---
@app.route('/api/reset', methods=['POST'])
def reset_data():
    save_data(json.loads(json.dumps(DEFAULT_DATA)))
    return jsonify({"ok": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
