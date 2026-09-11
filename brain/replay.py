# REPLAY: toca a gravacao (brain/data/gravacao/{ela,ele}.pkl) para o relay no lugar dos dois cerebros, com a placa
# desligada. Regras de honestidade: todo quadro e evento vai com replay:true (a pagina mostra REPLAY em vez de LIVE),
# 'lived' continua crescendo entre as voltas, e nenhum trade novo e inventado (trades gravados vao marcados como replay).
# Uso: py\Scripts\python.exe brain\replay.py   (FLY_RELAY_URL / FLY_RELAY_TOKEN ou relay.token na raiz)
import asyncio
import json
import os
import pickle
import struct
import time
from pathlib import Path

import aiohttp

AQUI = Path(__file__).resolve().parent
GRAV = AQUI / 'data' / 'gravacao'
URL = os.environ.get('FLY_RELAY_URL', 'wss://sexfly-production.up.railway.app/fonte')
TOKEN = os.environ.get('FLY_RELAY_TOKEN') or (AQUI.parent / 'relay.token').read_text().strip()
HEALTH = URL.replace('wss://', 'https://').replace('ws://', 'http://').rsplit('/fonte', 1)[0] + '/health'


def desempacotar(b):
    n = struct.unpack_from('<I', b, 0)[0]
    return json.loads(b[4:4 + n]), b[4 + n:]


def empacotar(cab, dados):
    j = json.dumps(cab, separators=(',', ':')).encode()
    return struct.pack('<I', len(j)) + j + dados


class Fita:
    def __init__(self, quem):
        self.quem = quem
        itens = pickle.load(open(GRAV / f'{quem}.pkl', 'rb'))
        self.ola = None
        self.passos = []            # (dt, 't'|'b', dado)
        vividos = []
        for dt, tipo, dado in itens:
            if tipo == 't':
                try:
                    m = json.loads(dado)
                except Exception:
                    continue
                if m.get('tipo') == 'ola':
                    self.ola = m
                elif m.get('tipo') == 'mercado' and m.get('classe') in ('sexo', 'resumo'):   # trades gravados NAO voltam como novos
                    self.passos.append((dt, 't', m))
            else:
                try:
                    cab, dados = desempacotar(dado)
                except Exception:
                    continue
                if cab.get('tipo') in ('quadro', 'corpo'):
                    self.passos.append((dt, 'b', (cab, dados)))
                    if cab.get('tipo') == 'quadro':
                        vividos.append(float(cab.get('lived', 0)))
        self.dur = itens[-1][0] if itens else 1.0
        self.salto = (max(vividos) - min(vividos)) if vividos else 0.0    # quanto de tempo de cerebro uma volta vale
        print(f'[replay {quem}] fita: {len(self.passos)} passos, {self.dur:.0f} s por volta, ola={"sim" if self.ola else "nao"}', flush=True)


async def tocar(quem):
    fita = Fita(quem)
    volta = 0
    viewers = 0
    instancia = ''
    espera = 1
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(URL, params={'token': TOKEN, 'quem': quem}, heartbeat=20,
                                           max_msg_size=16 * 1024 * 1024) as ws:
                    espera = 1
                    print(f'[replay {quem}] ligado a {URL}', flush=True)
                    ola = dict(fita.ola or {'tipo': 'ola', 'quem': quem})
                    ola['replay'] = True
                    await ws.send_str(json.dumps(ola, separators=(',', ':')))

                    async def ler():
                        nonlocal viewers, instancia
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    j = json.loads(m.data)
                                    viewers = int(j.get('viewers', viewers))
                                    instancia = str(j.get('instancia', instancia))
                                except Exception:
                                    pass

                    async def vigiar():
                        # depois de um redeploy do relay o conteiner velho segue vivo com esta conexao: reconecta no novo
                        while not ws.closed:
                            await asyncio.sleep(20)
                            try:
                                async with sess.get(HEALTH, timeout=aiohttp.ClientTimeout(total=10)) as r:
                                    h = await r.json()
                            except Exception:
                                continue
                            publica = str(h.get('instancia', ''))
                            if publica and instancia and publica != instancia:
                                print(f'[replay {quem}] publico no conteiner {publica}, eu no {instancia}: reconectando', flush=True)
                                await ws.close()
                                return

                    leitor = asyncio.create_task(ler())
                    vigia = asyncio.create_task(vigiar())
                    try:
                        while not ws.closed:
                            t0 = time.time()
                            for dt, tipo, dado in fita.passos:
                                atraso = t0 + dt - time.time()
                                if atraso > 0:
                                    await asyncio.sleep(atraso)
                                if ws.closed:
                                    break
                                if tipo == 'b':
                                    cab = dict(dado[0])
                                    cab['replay'] = True
                                    if cab.get('tipo') == 'quadro':
                                        cab['lived'] = round(float(cab.get('lived', 0)) + volta * fita.salto, 3)
                                        cab['viewers'] = viewers
                                        cab['relay'] = True
                                    await ws.send_bytes(empacotar(cab, dado[1]))
                                else:
                                    m = dict(dado)
                                    m['t'] = time.time()
                                    m['replay'] = True
                                    await ws.send_str(json.dumps(m, separators=(',', ':')))
                            volta += 1
                            print(f'[replay {quem}] volta {volta} concluida ({viewers} assistindo)', flush=True)
                    finally:
                        leitor.cancel()
                        vigia.cancel()
        except Exception as e:
            print(f'[replay {quem}] caiu: {str(e)[:100]}; tentando em {espera} s', flush=True)
        await asyncio.sleep(espera)
        espera = min(espera * 2, 30)


async def main():
    await asyncio.gather(tocar('ela'), tocar('ele'))


asyncio.run(main())
