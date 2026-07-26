# -*- coding: utf-8 -*-
"""Recuperacao de placares: prova que apos um 'Gerar Jogos' zerar os placares,
os endpoints /api/backups/inspect + /api/backups/restore recuperam o estado anterior.
Simula EXATAMENTE o incidente: placares salvos durante o dia -> alguem regenera a
tabela (zera tudo) -> restaura do backup automatico -> classificacao volta."""
import os, sys, tempfile, shutil, time
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sampa_restore_")
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
r=c.post("/api/torneios", json={"naipe":"feminino","nome":"DETOX E2","categoria":"E2","formato_jogos":"tri_corrido","max_equipes":3})
T=j(r)["id"]
ids={}
for n in ["QVIAGEM","ACE","VALKIRIAS"]:
    ids[n]=j(c.post("/api/equipes", json={"nome":n,"torneio_id":T,"responsavel":"R","telefone":"1"}))["id"]
c.post(f"/api/grupos/{T}/sorteio")
jogos=j(c.post(f"/api/jogos/{T}/gerar"))
check(len(jogos)==3, "tri: 3 jogos gerados")

print("=== dia do torneio: placares salvos (cada save gera backup automatico) ===")
for i,jg in enumerate(jogos):
    r=c.put(f"/api/jogos/{T}/{jg['id']}", json={"sets_a":2,"sets_b":0,"parciais":["25-20","25-18"],"finalizado":True})
    check(r.status_code==200, f"placar jogo {i+1} salvo (2x0)")

rank_bom=j(c.get(f"/api/classificacao/{T}/A"))
pts_bom=sum(x["pontos"] for x in rank_bom)
check(pts_bom>0, f"classificacao TEM pontos apos os placares (soma={pts_bom})")

# inspeciona: o topo dos backups deve ter jogos com placar
insp=j(c.get("/api/backups/inspect"))
check(insp["atual"]["jogos_com_placar"]==3, f"estado ATUAL tem 3 jogos com placar — veio {insp['atual']['jogos_com_placar']}")
com_placar=[b for b in insp["backups"] if b.get("jogos_com_placar",0)>0]
check(len(com_placar)>0, f"existe backup com placar pra restaurar ({len(com_placar)} de {len(insp['backups'])})")
melhor=com_placar[0]["arquivo"]  # mais recente com placar
print(f"  -> melhor backup: {melhor} ({com_placar[0]['jogos_com_placar']} jogos com placar)")

print("=== INCIDENTE: alguem clica 'Gerar Jogos' e zera tudo ===")
c.post(f"/api/jogos/{T}/gerar")
rank_zerado=j(c.get(f"/api/classificacao/{T}/A"))
check(sum(x["pontos"] for x in rank_zerado)==0, "classificacao ZERADA apos regerar (reproduz o bug)")
insp2=j(c.get("/api/backups/inspect"))
check(insp2["atual"]["jogos_com_placar"]==0, "estado atual agora tem 0 jogos com placar")
# os times continuam (nao e' perda total)
check(len(j(c.get(f"/api/equipes?torneio_id={T}")))==3, "os 3 times continuam la (so os placares sumiram)")

print("=== RECUPERACAO: restaura do backup ===")
r=c.post("/api/backups/restore", json={"filename":melhor})
check(r.status_code==200 and j(r)["ok"], f"restore OK de {melhor}")
check(j(r)["resumo"]["jogos_com_placar"]==3, "resumo pos-restore: 3 jogos com placar de volta")

rank_pos=j(c.get(f"/api/classificacao/{T}/A"))
pts_pos=sum(x["pontos"] for x in rank_pos)
check(pts_pos==pts_bom, f"classificacao RESTAURADA identica a original ({pts_pos}=={pts_bom})")
check([x["nome"] for x in rank_pos]==[x["nome"] for x in rank_bom], "mesma ordem da classificacao original")

# snapshot de seguranca do estado zerado foi criado (PRE_RESTORE em DATA_DIR, fora da rotacao)
pre=[f for f in os.listdir(TMP) if f.startswith("PRE_RESTORE_")]
check(len(pre)>=1, f"snapshot PRE_RESTORE do estado atual foi salvo antes de sobrescrever ({pre})")

print("=== seguranca: path-traversal barrado ===")
r=c.post("/api/backups/restore", json={"filename":"../tournament.json"})
check(r.status_code==400, "restore de '../tournament.json' barrado (400)")
r=c.post("/api/backups/restore", json={"filename":"naoexiste.json"})
check(r.status_code==400, "restore de arquivo inexistente barrado (400)")

print(f"\n{'==== RESTORE OK ====' if not erros else '==== FALHAS: '+str(len(erros))+' ===='}")
for e in erros: print("  -",e)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if erros else 0)
