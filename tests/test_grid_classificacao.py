# -*- coding: utf-8 -*-
"""Prova que o placar salvo pelo GRID (PUT sets+parciais) reflete na classificação,
INCLUSIVE no desempate por ratio de pontos (que vem das parciais).
Cenário círculo: A>B, B>C, C>A, todos 2x0 — vitórias/pontos/set-ratio empatados,
a ordem final é decidida SÓ pelas parciais digitadas."""
import os, sys, tempfile, shutil
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_gridclass_")
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
r=c.post("/api/torneios", json={"naipe":"feminino","nome":"T","categoria":"E","formato_jogos":"tri_corrido","max_equipes":3})
T=j(r)["id"]
ids={}
for n in ["Alfa","Beta","Gama"]:
    r=c.post("/api/equipes", json={"nome":n,"torneio_id":T,"responsavel":"R","telefone":"1"})
    ids[n]=j(r)["id"]
c.post(f"/api/grupos/{T}/sorteio")
jogos=j(c.post(f"/api/jogos/{T}/gerar"))
check(len(jogos)==3, "tri: 3 jogos gerados")

def salva_grid(par, venc, perd, parciais):
    """Simula o SALVAR do grid: acha o jogo venc x perd e faz o PUT com sets derivados."""
    jg=[x for x in jogos if {x["equipe_a"],x["equipe_b"]}=={ids[venc],ids[perd]}][0]
    # parciais no formato equipe_a-equipe_b do JOGO (inverte se o vencedor é equipe_b)
    if jg["equipe_a"]==ids[venc]:
        ps=[f"{a}-{b}" for a,b in parciais]; sa,sb=2,0
    else:
        ps=[f"{b}-{a}" for a,b in parciais]; sa,sb=0,2
    r=c.put(f"/api/jogos/{T}/{jg['id']}", json={"sets_a":sa,"sets_b":sb,"parciais":ps,"finalizado":True})
    check(r.status_code==200, f"grid salva {venc} 2x0 {perd} {ps}")

print("=== círculo perfeito: só as PARCIAIS desempatam ===")
salva_grid(jogos,"Alfa","Beta",[(25,10),(25,10)])   # Alfa vence LAVADO
salva_grid(jogos,"Beta","Gama",[(25,23),(25,23)])   # Beta vence APERTADO
salva_grid(jogos,"Gama","Alfa",[(25,20),(25,20)])   # Gama vence médio

rank=j(c.get(f"/api/classificacao/{T}/A"))
print("  classificação:")
for i,x in enumerate(rank):
    ratio=round(x["pontos_pro"]/x["pontos_contra"],3) if x["pontos_contra"] else "-"
    print(f"    {i+1}º {x['nome']:5} | V{x['vitorias']} D{x['derrotas']} | pts {x['pontos']} | sets {x['sets_pro']}:{x['sets_contra']} | pontos {x['pontos_pro']}:{x['pontos_contra']} (ratio {ratio})")

check(all(x["vitorias"]==1 and x["pontos"]==3 for x in rank), "empate total em vitórias (1) e pontos (3) — desempate vai pras parciais")
# ratios das parciais digitadas: Alfa 90/70=1.286 > Gama 96/90=1.067 > Beta 70/96=0.729
check([x["nome"] for x in rank]==["Alfa","Gama","Beta"], f"ordem decidida PELAS PARCIAIS: Alfa>Gama>Beta — veio {[x['nome'] for x in rank]}")
check(rank[0]["pontos_pro"]==90 and rank[0]["pontos_contra"]==70, "pontos pró/contra do 1º batem com as parciais digitadas")

print("=== e alimenta a fase seguinte (auto-classificação) ===")
r=c.post("/api/torneios", json={"naipe":"feminino","nome":"T2","categoria":"E","formato_jogos":"quad_decisao","max_equipes":4})
T2=j(r)["id"]
ids2=[]
for n in ["Q1","Q2","Q3","Q4"]:
    ids2.append(j(c.post("/api/equipes", json={"nome":n,"torneio_id":T2,"responsavel":"R","telefone":"1"}))["id"])
c.post(f"/api/grupos/{T2}/sorteio")
jgs2=j(c.post(f"/api/jogos/{T2}/gerar"))
for x in [x for x in jgs2 if x["fase"]=="hexagonal"]:
    c.put(f"/api/jogos/{T2}/{x['id']}", json={"sets_a":2,"sets_b":0,"parciais":["25-15","25-15"],"finalizado":True})
rank2=j(c.get(f"/api/classificacao/{T2}/A"))
fin=[x for x in j(c.get(f"/api/jogos/{T2}/admin")) if x["fase"]=="final"][0]
ter=[x for x in j(c.get(f"/api/jogos/{T2}/admin")) if x["fase"]=="terceiro"][0]
check(fin["equipe_a"]==rank2[0]["id"] and fin["equipe_b"]==rank2[1]["id"], "final auto-preenchida (1ºx2º) a partir dos placares do grid")
check(ter["equipe_a"]==rank2[2]["id"] and ter["equipe_b"]==rank2[3]["id"], "3º lugar auto-preenchido (3ºx4º)")

print(f"\n{'==== GRID -> CLASSIFICACAO OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
