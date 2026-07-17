# -*- coding: utf-8 -*-
"""Testa PUT /api/equipes/<id> (renomear equipe preservando login/senha/pagamento)."""
import os, sys, tempfile, shutil
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_rename_")
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
r=c.post("/api/equipes", json={"nome":"SANTOSPFC","torneio_id":T,"responsavel":"Camila Moretti","telefone":"119"})
eq=j(r); eid=eq["id"]; login_orig=eq["login"]; senha_orig=eq["senha"]

print("=== renomear ===")
r=c.put(f"/api/equipes/{eid}", json={"nome":"SANTOS PFC"})
check(r.status_code==200 and j(r)["ok"], "PUT renomeia")
check(j(r)["equipe"]["nome"]=="SANTOS PFC", "nome novo aplicado")
check(j(r)["equipe"]["login"]==login_orig, "login NÃO muda (equipe não perde acesso)")
check("senha_hash" not in j(r)["equipe"], "resposta não vaza senha_hash")
# login continua funcionando com a senha original
cpub=app.app.test_client()
r=cpub.post("/api/auth/equipe", json={"login":login_orig,"senha":senha_orig})
check(r.status_code==200 and j(r)["ok"], "equipe renomeada ainda loga com credencial original")
check(j(r)["nome"]=="SANTOS PFC", "login retorna o nome novo")
# público reflete
pub=j(cpub.get(f"/api/equipes?torneio_id={T}"))
check(pub[0]["nome"]=="SANTOS PFC", "público mostra nome novo")

print("=== validações ===")
r=c.put(f"/api/equipes/{eid}", json={"nome":"  "})
check(r.status_code==400, "nome vazio barrado")
r=c.put(f"/api/equipes/{eid}", json={"nome":"X"*90})
check(r.status_code==400, "nome >80 barrado")
r=c.put("/api/equipes/naoexiste", json={"nome":"Y"})
check(r.status_code==404, "equipe inexistente = 404")
r=cpub.put(f"/api/equipes/{eid}", json={"nome":"Hack"})
check(r.status_code==403, "sem admin = 403")
# responsavel/telefone também editáveis
r=c.put(f"/api/equipes/{eid}", json={"responsavel":"Nova Resp","telefone":"11888"})
check(j(r)["equipe"]["responsavel"]=="Nova Resp", "responsável editável")

print(f"\n{'==== RENAME OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
