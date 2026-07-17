# -*- coding: utf-8 -*-
"""Testa os formatos novos: penta_corrido/penta_decisao (5 equipes) e tri_corrido/tri_final (3)."""
import os, sys, tempfile, shutil
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_penta_")
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

def monta_torneio(fmt, n):
    """Cria torneio no formato, inscreve n equipes, sorteia e gera jogos. Retorna (tid, jogos)."""
    r=c.post("/api/torneios", json={"naipe":"feminino","nome":f"T-{fmt}","categoria":"X",
                                    "formato_jogos":fmt,"max_equipes":n})
    tid=j(r)["id"]
    for i in range(n):
        c.post("/api/equipes", json={"nome":f"{fmt}-{i+1}","torneio_id":tid,"responsavel":"R","telefone":"1"})
    c.post(f"/api/grupos/{tid}/sorteio")
    r=c.post(f"/api/jogos/{tid}/gerar")
    return tid, j(r)

def joga_regulares(tid):
    """Finaliza todos os regulares (equipe_a sempre ganha 2x0) e retorna jogos atualizados."""
    jogos=j(c.get(f"/api/jogos/{tid}/admin"))
    for jg in [x for x in jogos if x["fase"]=="hexagonal"]:
        c.put(f"/api/jogos/{tid}/{jg['id']}", json={"sets_a":2,"sets_b":0,"finalizado":True,"parciais":["25-10","25-15"]})
    return j(c.get(f"/api/jogos/{tid}/admin"))

print("=== PENTA_DECISAO (5 equipes) ===")
tid,jogos=monta_torneio("penta_decisao",5)
grp=j(c.get(f"/api/grupos/{tid}"))
check(len(grp["A"])==5 and grp["B"]==[], "sorteio: 5 equipes em A")
reg=[x for x in jogos if x["fase"]=="hexagonal"]
check(len(reg)==10, f"10 jogos regulares (todos x todos de 5) — veio {len(reg)}")
check(sum(1 for x in jogos if x["fase"]=="terceiro")==1, "tem disputa de 3º")
check(sum(1 for x in jogos if x["fase"]=="final")==1, "tem final")
check(len(jogos)==12, f"12 jogos no total — veio {len(jogos)}")
jogos=joga_regulares(tid)
rank=j(c.get(f"/api/classificacao/{tid}/A"))
check(len(rank)==5, "classificação com 5 equipes")
fin=next(x for x in jogos if x["fase"]=="final"); ter=next(x for x in jogos if x["fase"]=="terceiro")
check(fin["equipe_a"]==rank[0]["id"] and fin["equipe_b"]==rank[1]["id"], "final auto = 1º x 2º")
check(ter["equipe_a"]==rank[2]["id"] and ter["equipe_b"]==rank[3]["id"], "3º lugar auto = 3º x 4º (5º fica fora)")

print("=== PENTA_CORRIDO (5 equipes) ===")
tid2,jogos2=monta_torneio("penta_corrido",5)
check(len(jogos2)==10, f"10 jogos e nada mais — veio {len(jogos2)}")
check(all(x["fase"]=="hexagonal" for x in jogos2), "sem eliminatória (pontos corridos)")

print("=== TRI_FINAL (3 equipes) ===")
tid3,jogos3=monta_torneio("tri_final",3)
reg3=[x for x in jogos3 if x["fase"]=="hexagonal"]
check(len(reg3)==3, f"3 jogos regulares — veio {len(reg3)}")
check(sum(1 for x in jogos3 if x["fase"]=="final")==1, "tem final")
check(sum(1 for x in jogos3 if x["fase"]=="terceiro")==0, "SEM disputa de 3º (só 3 equipes)")
check(len(jogos3)==4, f"4 jogos no total — veio {len(jogos3)}")
jogos3=joga_regulares(tid3)
rank3=j(c.get(f"/api/classificacao/{tid3}/A"))
fin3=next(x for x in jogos3 if x["fase"]=="final")
check(fin3["equipe_a"]==rank3[0]["id"] and fin3["equipe_b"]==rank3[1]["id"], "final auto = 1º x 2º")

print("=== TRI_CORRIDO (3 equipes) ===")
tid4,jogos4=monta_torneio("tri_corrido",3)
check(len(jogos4)==3 and all(x["fase"]=="hexagonal" for x in jogos4), "3 jogos, sem eliminatória")

print("=== MODO TESTE: cenários novos ===")
r=c.post("/api/test/setup-penta-decisao")
check(r.status_code==200 and j(r)["equipes_count"]==5 and j(r)["jogos_count"]==12, "setup penta: 5 equipes, 12 jogos")
r=c.post("/api/test/setup-tri-final")
check(r.status_code==200 and j(r)["equipes_count"]==3 and j(r)["jogos_count"]==4, "setup tri_final: 3 equipes, 4 jogos")
c.post("/api/test/teardown")

print("=== formato inválido continua barrado ===")
r=c.post("/api/torneios", json={"naipe":"feminino","nome":"X","formato_jogos":"hepta_coisa","max_equipes":7})
check(j(r).get("formato_jogos") in app.FORMATOS_VALIDOS, "formato inválido cai no default (não aceita hepta_coisa)")

print(f"\n{'==== PENTA + TRI OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
