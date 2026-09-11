# Estimulos e saidas extras definidos por TIPO CELULAR (nao por id), a partir de brain/data/neuronios.parquet.
# Assim a troca de conectoma (FlyWire -> Male CNS da Janelia) refaz as listas sozinha, desde que os tipos
# tenham o mesmo nome. Cada entrada: neuronios (root_id), taxa (Hz) e descricao em ingles para a tela.
# As taxas foram escolhidas nos testes de 06/09/2026 (ver README): baixas o bastante para nao prender a rede.
import re
import numpy as np
import pandas as pd

# nome -> (regex do cell_type, taxa Hz, descricao)
ENTRADAS = {
    'lc16':      (r'^LC16$',                100.0, 'LC16 visual cells, object behind (151 neurons): turning away'),
    'eye_touch': (r'^BM_InOm$',              40.0, 'Interommatidial bristles (1,113 neurons): eye grooming'),
    'reward':    (r'^PAM\d\d',               50.0, 'PAM dopamine neurons (307): reward, a state not a movement'),
    'mdn':       (r'^MDN$',                 100.0, 'MDN backward-walking command neurons (4): backing up'),
    'vinegar':   (r'^ORN_(DM1|DM4|VA2)$',    60.0, 'Vinegar smell (BLOCKED: traps the network)'),
    'heat':      (r'^TRN_VP2$',             100.0, 'Hot cells (BLOCKED: traps the network)'),
    'pc1':       (r'^pC1[a-e]$',            100.0, 'pC1 neurons (10): female receptivity, she lets him mount'),
    'reject':    (r'^(DNp13|oviDN.*)$',     100.0, 'DNp13 + oviDN (8): rejection, she kicks him off'),
}
# Medido em 06/09/2026 (400 ms, plasticidade off): olfato (175 ORNs a 30 Hz) e calor (7 neuronios a 50 Hz)
# levam a rede ao estado preso de ~460 mil spikes/s, igual ao odor Or56a. Tudo que entra pelo lobo antenal
# prende. Paladar, visao, cerdas e dopamina sao seguros.
BLOQUEADOS = {'vinegar', 'heat'}

# grupos de saida extras: nome -> regex do cell_type (taxa por neuronio, como os descendentes)
SAIDAS = {
    'halt':   r'^DNp09$',      # parada brusca (2 neuronios)
    'reward': r'^PAM\d\d',     # dopamina: estado de recompensa/excitacao (307)
}


def montar(meta_parquet):
    """Devolve (entradas, saidas): dicts nome -> lista de root_id, a partir do parquet de metadados."""
    m = pd.read_parquet(meta_parquet)
    ct = m['cell_type'].fillna('').to_numpy()
    ids = m['root_id'].to_numpy()
    entradas, saidas = {}, {}
    for nome, (padrao, taxa, desc) in ENTRADAS.items():
        mask = np.array([bool(re.match(padrao, t)) for t in ct])
        entradas[nome] = {'neurons': [int(i) for i in ids[mask]], 'rate': taxa, 'description': desc}
    for nome, padrao in SAIDAS.items():
        mask = np.array([bool(re.match(padrao, t)) for t in ct])
        saidas[nome] = [int(i) for i in ids[mask]]
    return entradas, saidas


if __name__ == '__main__':
    from pathlib import Path
    e, s = montar(Path(__file__).resolve().parent / 'data' / 'neuronios.parquet')
    for k, v in e.items():
        print(f'{k:<10} {len(v["neurons"]):>5} neuronios @ {v["rate"]:.0f} Hz')
    for k, v in s.items():
        print(f'saida {k:<6} {len(v)} neuronios')
