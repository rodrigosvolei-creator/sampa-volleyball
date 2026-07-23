# -*- coding: utf-8 -*-
"""Testa PUT /api/atletas/<equipe_id>/<atleta_id> (corrigir nome/nascimento/documento)."""
import os, sys, tempfile, shutil
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_atleta_")
os.environ["DATA_DIR"] = TMP
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "sampa2026"
os.environ["INSCRICAO_ABRE_ISO"] = "2020-01-01T00:00:00-03:00"
sys.path.insert(0, PROJ)
import app
c = app.app.test_client()
erros=[]
def check(cond,msg): (print(f"  OK  {msg}") if cond else (erros.append(msg) or print(f"  FALHA  {msg}")))
def j(r): return r.get_json()

c.post("/api/auth", json={"password":"sampa2026"})
r=c.post("/api/torneios", json={"naipe":"feminino","nome":"T","categoria":"E1","formato_jogos":"quad_decisao","max_equipes":4})
T=j(r)["id"]
r=c.post("/api/equipes", json={"nome":"Time A","torneio_id":T,"responsavel":"R","telefone":"1"})
eq=j(r); eid=eq["id"]; login=eq["login"]; senha=eq["senha"]
c.post(f"/api/equipes/{eid}/pagamento", json={"pagamento_status":"aprovado"})
r=c.post(f"/api/atletas/{eid}", json={"nome_completo":"Mria Silva","data_nascimento":"1985-01-13",
                                       "tipo_documento":"RG","numero_documento":"123456"})
aid=j(r)["id"]

print("=== admin corrige nome + data de nascimento ===")
r=c.put(f"/api/atletas/{eid}/{aid}", json={"nome_completo":"Maria Silva","data_nascimento":"1985-01-31"})
check(r.status_code==200 and j(r)["ok"], "PUT ok (admin)")
check(j(r)["atleta"]["nome_completo"]=="Maria Silva", "nome corrigido")
check(j(r)["atleta"]["data_nascimento"]=="1985-01-31", "data de nascimento corrigida")
full=j(c.get(f"/api/atletas/{eid}/full"))
check(full[0]["nome_completo"]=="Maria Silva" and full[0]["data_nascimento"]=="1985-01-31", "persistiu")

print("=== a própria equipe também edita ===")
ct=app.app.test_client()
ct.post("/api/auth/equipe", json={"login":login,"senha":senha})
r=ct.put(f"/api/atletas/{eid}/{aid}", json={"numero_documento":"999888","tipo_documento":"CPF"})
check(r.status_code==200 and j(r)["atleta"]["numero_documento"]=="999888", "equipe edita documento")

print("=== permissões e validações ===")
anon=app.app.test_client()
r=anon.put(f"/api/atletas/{eid}/{aid}", json={"nome_completo":"Hack"})
check(r.status_code==403, "anônimo barrado (403)")
r=c.put(f"/api/atletas/{eid}/naoexiste", json={"nome_completo":"X"})
check(r.status_code==404, "atleta inexistente 404")
r=c.put(f"/api/atletas/{eid}/{aid}", json={"nome_completo":"   "})
check(r.status_code==400, "nome vazio barrado")
full=j(c.get(f"/api/atletas/{eid}/full"))
check(full[0]["nome_completo"]=="Maria Silva", "nada mudou após tentativas inválidas")

print(f"\n{'==== EDITAR ATLETA OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
