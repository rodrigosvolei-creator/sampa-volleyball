# -*- coding: utf-8 -*-
"""Roda toda a suíte de testes (cada script em processo próprio, DATA_DIR temporário).
Uso: python tests/run_all.py"""
import os, sys, glob, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
scripts = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
falhas = []
for s in scripts:
    nome = os.path.basename(s)
    r = subprocess.run([sys.executable, s], capture_output=True, text=True)
    ok = r.returncode == 0
    print(f"{'PASSOU' if ok else 'FALHOU'}  {nome}")
    if not ok:
        falhas.append(nome)
        print(r.stdout[-1500:])
        print(r.stderr[-500:])
print(f"\n{len(scripts)-len(falhas)}/{len(scripts)} suites verdes")
sys.exit(1 if falhas else 0)
