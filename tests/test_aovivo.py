# -*- coding: utf-8 -*-
"""Fluxo Ao Vivo completo: placar por LADO da quadra (troca no set 2, tie-break no 3),
parciais registradas por equipe, e bloqueio de finalizar empatado (vôlei não tem empate)."""
import os, sys, tempfile, shutil
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_aovivo_")
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
r=c.post("/api/torneios", json={"naipe":"feminino","nome":"T","categoria":"E","formato_jogos":"quad_corrido","max_equipes":4})
T=j(r)["id"]
for n in ["AA","BB","CC","DD"]:
    c.post("/api/equipes", json={"nome":n,"torneio_id":T,"responsavel":"R","telefone":"1"})
c.post(f"/api/grupos/{T}/sorteio")
jogos=j(c.post(f"/api/jogos/{T}/gerar"))
jg=jogos[0]; jid=jg["id"]

print("=== jogo 1: 2x0 com TROCA DE LADO no set 2 ===")
r=c.post(f"/api/jogos/{T}/{jid}/iniciar", json={})
check(r.status_code==200 and j(r)["ok"], "inicia (set 1, equipe_a à esquerda)")
c.post(f"/api/jogos/{T}/{jid}/pontos", json={"pontos_a":25,"pontos_b":20})
r=c.post(f"/api/jogos/{T}/{jid}/encerrar-set", json={})
check(r.status_code==200, "encerra set 1")
st=j(c.get(f"/api/jogos/{T}/admin")); g=[x for x in st if x["id"]==jid][0]
check(g["sets_a"]==1 and g["parciais"]==["25-20"], f"set 1 pra equipe A (parcial 25-20) — veio {g['parciais']}")
lado2=g.get("set_atual",{}).get("lado_esq")
check(lado2==g["equipe_b"], "set 2: lados TROCADOS (equipe B à esquerda)")
# equipe A vence o set 2 — como B está à esquerda, o placar por lado é 18(esq) x 25(dir)
c.post(f"/api/jogos/{T}/{jid}/pontos", json={"pontos_a":18,"pontos_b":25})
c.post(f"/api/jogos/{T}/{jid}/encerrar-set", json={})
st=j(c.get(f"/api/jogos/{T}/admin")); g=[x for x in st if x["id"]==jid][0]
check(g["sets_a"]==2 and g["sets_b"]==0, f"2x0 pra equipe A — veio {g['sets_a']}x{g['sets_b']}")
check(g["parciais"]==["25-20","25-18"], f"parciais POR EQUIPE corretas — veio {g['parciais']}")
r=c.post(f"/api/jogos/{T}/{jid}/encerrar", json={})
check(r.status_code==200 and j(r)["ok"], "encerra o jogo 2x0")
rank=j(c.get(f"/api/classificacao/{T}/A"))
venc=[x for x in rank if x["id"]==g["equipe_a"]][0]; perd=[x for x in rank if x["id"]==g["equipe_b"]][0]
check(venc["pontos"]==3 and venc["vitorias"]==1, "vencedor 2x0 leva 3 pts")
check(perd["pontos"]==0 and perd["derrotas"]==1, "perdedor 0 pts")

print("=== jogo 2: EMPATE NÃO FINALIZA (bloqueio novo) ===")
jid2=jogos[1]["id"]
rank_pre={x["id"]:x["pontos"] for x in j(c.get(f"/api/classificacao/{T}/A"))}
r=c.post(f"/api/jogos/{T}/{jid2}/iniciar", json={})
r=c.post(f"/api/jogos/{T}/{jid2}/encerrar", json={})
check(r.status_code==400 and "empatado" in (j(r).get("error") or ""), "0x0 barrado com mensagem clara")
c.post(f"/api/jogos/{T}/{jid2}/pontos", json={"pontos_a":25,"pontos_b":20})
c.post(f"/api/jogos/{T}/{jid2}/encerrar-set", json={})
# set 2 pro lado esquerdo (equipe B, que trocou de lado) => 1x1
c.post(f"/api/jogos/{T}/{jid2}/pontos", json={"pontos_a":25,"pontos_b":20})
c.post(f"/api/jogos/{T}/{jid2}/encerrar-set", json={})
st=j(c.get(f"/api/jogos/{T}/admin")); g2=[x for x in st if x["id"]==jid2][0]
check(g2["sets_a"]==1 and g2["sets_b"]==1, "1x1 após dois sets")
r=c.post(f"/api/jogos/{T}/{jid2}/encerrar", json={})
check(r.status_code==400, "encerrar 1x1 BARRADO")
st=j(c.get(f"/api/jogos/{T}/admin")); g2=[x for x in st if x["id"]==jid2][0]
check(not g2["finalizado"] and g2["em_andamento"], "jogo segue em andamento (nada foi mutado)")
# tie-break (set 3): placar é POR LADO; no 8º ponto o sistema troca os lados E os pontos
# acompanham as equipes. Simula como o painel: equipe A chega a 8 → troca → segue até 15-10.
st=j(c.get(f"/api/jogos/{T}/admin")); g2=[x for x in st if x["id"]==jid2][0]
lado3=g2.get("set_atual",{}).get("lado_esq")
# equipe A com 8, adversária com 5 (mapeado pro lado atual)
pts8={"pontos_a":8,"pontos_b":5} if lado3==g2["equipe_a"] else {"pontos_a":5,"pontos_b":8}
r=c.post(f"/api/jogos/{T}/{jid2}/pontos", json=pts8)
check(j(r).get("troca_acionada")==True, "8º ponto do tie-break dispara a troca de lado")
sa2=j(r)["set_atual"]
lado_novo=sa2["lado_esq"]
check(lado_novo!=lado3, "lado_esq invertido na troca")
pts_A_agora=sa2["pontos_a"] if lado_novo==g2["equipe_a"] else sa2["pontos_b"]
check(pts_A_agora==8, "os 8 pontos SEGUEM a equipe A após a troca (não trocam de dono)")
# segue o jogo até A fechar 15-10 (no lado novo)
ptsFim={"pontos_a":15,"pontos_b":10} if lado_novo==g2["equipe_a"] else {"pontos_a":10,"pontos_b":15}
c.post(f"/api/jogos/{T}/{jid2}/pontos", json=ptsFim)
r=c.post(f"/api/jogos/{T}/{jid2}/encerrar", json={})
check(r.status_code==200 and j(r)["ok"], "com o tie-break marcado, encerrar absorve o set e finaliza")
gf=j(r)["jogo"]
check(gf["sets_a"]==2 and gf["sets_b"]==1, f"final 2x1 — veio {gf['sets_a']}x{gf['sets_b']}")
check(gf["parciais"]==["25-20","20-25","15-10"], f"parciais 2x1 corretas — veio {gf['parciais']}")
rank_pos={x["id"]:x["pontos"] for x in j(c.get(f"/api/classificacao/{T}/A"))}
dv=rank_pos[gf["equipe_a"]]-rank_pre.get(gf["equipe_a"],0)
dp=rank_pos[gf["equipe_b"]]-rank_pre.get(gf["equipe_b"],0)
check(dv==2 and dp==1, f"classificação computa o 2x1 (vencedor +2, perdedor +1) — veio +{dv}/+{dp}")

print("=== PUT manual (grid) também barra finalizar empatado ===")
jid3=jogos[2]["id"]
r=c.put(f"/api/jogos/{T}/{jid3}", json={"sets_a":1,"sets_b":1,"parciais":["25-20","20-25"],"finalizado":True})
check(r.status_code==400, "PUT finalizado 1x1 barrado (400)")
st=j(c.get(f"/api/jogos/{T}/admin")); g3=[x for x in st if x["id"]==jid3][0]
check(not g3["finalizado"] and g3["sets_a"]==0, "jogo intocado após tentativa inválida")
r=c.put(f"/api/jogos/{T}/{jid3}", json={"sets_a":2,"sets_b":1,"parciais":["25-20","20-25","15-10"],"finalizado":True})
check(r.status_code==200, "PUT 2x1 válido finaliza normal")

print(f"\n{'==== AO VIVO OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
