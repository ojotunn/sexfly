# Grava N segundos de TUDO que a pagina recebe dos dois servidores locais (ela 8435: cerebro + corpo + mercado;
# ele 8436: cerebro). E a base do replay (brain/replay.py), para desligar a placa sem tirar o site do ar.
# Uso: py\Scripts\python.exe brain\gravar.py [segundos]   (padrao 480)
import asyncio
import pickle
import sys
import time
from pathlib import Path

import aiohttp

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 480.0
DEST = Path(__file__).resolve().parent / 'data' / 'gravacao'
DEST.mkdir(parents=True, exist_ok=True)


async def gravar(porta, nome):
    itens = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f'http://localhost:{porta}/ws', max_msg_size=16 * 1024 * 1024) as ws:
            t0 = time.time()
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    itens.append((time.time() - t0, 't', m.data))
                elif m.type == aiohttp.WSMsgType.BINARY:
                    itens.append((time.time() - t0, 'b', m.data))
                if time.time() - t0 >= DUR:
                    break
    with open(DEST / f'{nome}.pkl', 'wb') as f:
        pickle.dump(itens, f)
    print(f'[gravar] {nome}: {len(itens)} mensagens em {DUR:.0f} s -> {DEST / (nome + ".pkl")}', flush=True)


async def main():
    await asyncio.gather(gravar(8435, 'ela'), gravar(8436, 'ele'))


asyncio.run(main())
