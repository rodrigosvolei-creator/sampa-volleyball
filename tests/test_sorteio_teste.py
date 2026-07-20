# -*- coding: utf-8 -*-
"""Testa a Simulação de Sorteio do Modo Teste: setup sem grupos/jogos + confirmação 3+3."""
import os, sys, tempfile, shutil
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_sorteio_")
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

print("=== SETUP sorteio: 6 equipes, SEM grupos e SEM jogos ===")
r=c.post("/api/test/setup-sorteio")
check(r.status_code==200 and j(r)["ok"] and j(r)["equipes_count"]==6, "setup cria 6 equipes")
st=j(c.get("/api/test/state"))
check(st["active"], "ambiente ATIVO mesmo sem jogos (cenário sorteio)")
check(st["cenario"]=="sorteio", "cenario=sorteio")
check(len(st["equipes"])==6 and len(st["jogos"])==0, "6 equipes, 0 jogos")
ids=[e["id"] for e in st["equipes"]]

print("=== validações do confirmar ===")
r=c.post("/api/test/sorteio-confirmar", json={"A":ids[:4],"B":ids[4:]})
check(r.status_code==400, "4+2 barrado")
r=c.post("/api/test/sorteio-confirmar", json={"A":ids[:3],"B":ids[2:5]})
check(r.status_code==400, "equipe repetida barrada")
r=c.post("/api/test/sorteio-confirmar", json={"A":ids[:3],"B":ids[3:5]+["fake123"]})
check(r.status_code==400, "id que não é equipe de teste barrado")

print("=== confirmar 3+3 gera a tabela ===")
A,B=ids[:3],ids[3:]
r=c.post("/api/test/sorteio-confirmar", json={"A":A,"B":B})
check(r.status_code==200 and j(r)["ok"], "confirmação ok")
check(j(r)["jogos_count"]==10, f"10 jogos (3+3 regulares + semis + 3º + final) — veio {j(r).get('jogos_count')}")
check(j(r)["grupos"]=={"A":A,"B":B}, "grupos gravados NA ORDEM do sorteio revelado")
st=j(c.get("/api/test/state"))
check(st["cenario"]=="grupos", "após confirmar vira cenário grupos (UI normal)")
check(len(st["jogos"])==10, "state mostra os 10 jogos")
check(st["grupos"]=={"A":A,"B":B}, "state expõe os grupos do sorteio")

print("=== isolamento preservado ===")
pub=j(c.get("/api/equipes"))
check(all(not (e.get("nome") or "").startswith("🧪") for e in pub), "equipes de teste invisíveis no público")
tor=j(c.get("/api/torneios"))
check(all(t["id"]!="__test__" for t in tor), "torneio __test__ fora do público")

print("=== teardown limpa tudo ===")
c.post("/api/test/teardown")
st=j(c.get("/api/test/state"))
check(not st["active"] and not st["jogos"], "limpo")

print(f"\n{'==== SORTEIO SIMULADO OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
