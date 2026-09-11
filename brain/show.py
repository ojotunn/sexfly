# Durante a gravacao (brain/gravar.py), no lugar do mercado: mantem o casal cruzando com a libido ondulando e bate
# nos cerebros os MESMOS estimulos que as compras e as estocadas batem (JO nos dois, pC1 nela, dopamina a cada 4,
# acucar de vez em quando). Nao publica trade nenhum: so o evento 'sexo' que move a animacao.
import json
import math
import random
import time
import urllib.request

ELA = 'http://localhost:8435'
ELE = 'http://localhost:8436'


def post(url, d):
    try:
        req = urllib.request.Request(url, data=json.dumps(d).encode(), headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print('[show] falhou', url, str(e)[:60], flush=True)


def estimular(nome, ms, quem='ambos'):
    for alvo in (['ela', 'ele'] if quem == 'ambos' else [quem]):
        post((ELA if alvo == 'ela' else ELE) + '/api/estimulo', {'estimulo': nome, 'ms': ms})


t0 = time.time()
prox_pub = prox_est = prox_doce = 0.0
n = 0
print('[show] libido ondulando 0,10-0,80 a cada 2,5 min; Ctrl+C para parar', flush=True)
while True:
    t = time.time() - t0
    lib = 0.45 + 0.35 * math.sin(2 * math.pi * t / 150.0)
    hz = round(1.0 + 5.0 * lib, 2)
    agora = time.time()
    if agora >= prox_pub:
        prox_pub = agora + 3.0
        post(ELA + '/api/mercado', {'classe': 'sexo', 'libido': round(lib, 3), 'estado': 'mating', 'ritmo_hz': hz})
    if agora >= prox_est:
        periodo = 1.0 / hz
        prox_est = agora + periodo
        ms = int(min(250.0, 600.0 * periodo))
        n += 1
        estimular('jo', ms, 'ambos')
        estimular('pc1', ms, 'ela')
        if n % 4 == 0:
            estimular('reward', int(150 + 350 * lib), 'ambos')
    if agora >= prox_doce:
        prox_doce = agora + random.uniform(8.0, 25.0)
        estimular('sugar', random.randint(150, 500), 'ambos')
    time.sleep(0.05)
