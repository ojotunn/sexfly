# Relay publico do FLY (roda no Railway): o PC do Michel, onde o cerebro e o corpo rodam, conecta em /fonte e
# manda os quadros; os espectadores conectam em /ws e recebem o mesmo protocolo da pagina local. Serve tambem a
# pagina publica e os dados estaticos (neuronios, casca, modelo da mosca). Nao ha /dev nem estimulo aqui:
# o publico so assiste. Cada espectador tem uma fila curta; se a conexao dele atrasa, perde quadros, nao acumula.
import asyncio
import json
import os
import struct
import time
from collections import deque
from pathlib import Path

from aiohttp import web

PORTA = int(os.environ.get('PORT', '8080'))
TOKEN = os.environ.get('FLY_RELAY_TOKEN', '')
import uuid
INSTANCIA = uuid.uuid4().hex[:8]     # cada conteiner tem o seu; a fonte confere pelo /health se esta no conteiner que atende o publico
RAIZ = Path(__file__).resolve().parent.parent
SITE = RAIZ / 'site'
FILA_MAX = 6


def cabecalho(dados):
    try:
        n = struct.unpack_from('<I', dados, 0)[0]
        return json.loads(dados[4:4 + n].decode('utf-8'))
    except Exception:
        return {}


class Espectador:
    def __init__(self, ws):
        self.ws = ws
        self.fila = asyncio.Queue(maxsize=FILA_MAX)
        self.perdidos = 0

    def enviar(self, item):
        if self.fila.full():
            try:
                self.fila.get_nowait()
                self.perdidos += 1
            except asyncio.QueueEmpty:
                pass
        self.fila.put_nowait(item)

    async def escritor(self):
        while True:
            tipo, dados = await self.fila.get()
            if tipo == 'b':
                await self.ws.send_bytes(dados)
            else:
                await self.ws.send_str(dados)


def espalhar(app, tipo, dados):
    for e in list(app['espectadores']):
        e.enviar((tipo, dados))


async def fonte(request):
    """O PC com o cerebro. Um so por vez: o mais novo derruba o anterior."""
    app = request.app
    if not TOKEN or request.query.get('token') != TOKEN:
        raise web.HTTPForbidden(text='token')
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    quem = 'ele' if request.query.get('quem') == 'ele' else 'ela'     # dois cerebros: ela (principal) e ele
    chave = 'fonte' if quem == 'ela' else 'fonte_ele'
    velho = app['estado'].get(chave)
    if velho is not None and not velho.closed:
        await velho.close()
    app['estado'][chave] = ws
    app['estado']['fonte_t'] = time.time()
    print(f'[relay] fonte conectada ({quem})', flush=True)

    async def contar():
        while not ws.closed:
            try:
                await ws.send_str(json.dumps({'viewers': len(app['espectadores']), 'instancia': INSTANCIA}))
            except Exception:
                break
            await asyncio.sleep(2)

    contador = asyncio.create_task(contar())
    try:
        async for msg in ws:
            app['estado']['fonte_t'] = time.time()
            if msg.type == web.WSMsgType.BINARY:
                cab = cabecalho(msg.data)
                tipo = cab.get('tipo')
                if tipo == 'quadro':
                    app['estado']['quadro' if quem == 'ela' else 'quadro_ele'] = (cab, msg.data)
                elif tipo == 'corpo':
                    if quem != 'ela':
                        continue
                    app['estado']['corpo'] = msg.data
                espalhar(app, 'b', msg.data)
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                if m.get('tipo') == 'ola':
                    app['estado']['ola' if quem == 'ela' else 'ola_ele'] = msg.data
                    continue
                if quem != 'ela':                        # o macho so manda quadros; config/mercado/corpo vem dela
                    continue
                if m.get('tipo') == 'config':            # CA e X vindos do PC: guarda (injeta na pagina) e espalha ao vivo
                    app['estado']['config'] = {'ca': str(m.get('ca', '')), 'x': str(m.get('x', ''))}
                    espalhar(app, 't', msg.data)
                    continue
                if m.get('tipo') == 'mercado':
                    if m.get('classe') == 'resumo':
                        app['estado']['resumo'] = msg.data
                    elif m.get('classe') == 'limpar':
                        app['estado']['eventos'].clear()
                        app['estado']['ordens'].clear()
                    else:
                        app['estado']['eventos'].append(msg.data)
                        if m.get('classe') == 'ordem':          # ordens dela guardadas a parte: nao se perdem no feed
                            app['estado']['ordens'].append(msg.data)
                espalhar(app, 't', msg.data)
    finally:
        contador.cancel()
        if app['estado'].get(chave) is ws:
            app['estado'][chave] = None
        print(f'[relay] fonte desconectada ({quem})', flush=True)
    return ws


async def fonte_limpar(request):
    """Zera o historico de cards do relay e manda as paginas abertas limparem (lancamento). Exige o token."""
    app = request.app
    if not TOKEN or request.query.get('token') != TOKEN:
        raise web.HTTPForbidden(text='token')
    app['estado']['eventos'].clear()
    app['estado']['ordens'].clear()
    espalhar(app, 't', json.dumps({'tipo': 'mercado', 'classe': 'limpar', 't': time.time()}, separators=(',', ':')))
    return web.json_response({'ok': True})


async def ws_handler(request):
    """Um espectador: recebe o estado atual e depois tudo o que a fonte manda."""
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    e = Espectador(ws)
    est = app['estado']
    if est.get('ola'):
        e.enviar(('t', est['ola']))
    if est.get('ola_ele'):
        e.enviar(('t', est['ola_ele']))
    if est.get('config'):
        e.enviar(('t', json.dumps(dict(est['config'], tipo='config'), separators=(',', ':'))))
    if est.get('corpo'):
        e.enviar(('b', est['corpo']))
    if est.get('quadro'):
        e.enviar(('b', est['quadro'][1]))
    if est.get('quadro_ele'):
        e.enviar(('b', est['quadro_ele'][1]))
    if est.get('resumo'):
        e.enviar(('t', est['resumo']))
    for ev in list(est['ordens'])[-6:]:
        e.enviar(('t', ev))
    for ev in list(est['eventos'])[-12:]:
        e.enviar(('t', ev))
    app['espectadores'].add(e)
    escritor = asyncio.create_task(e.escritor())
    try:
        async for msg in ws:          # o publico nao manda nada que valha; so mantem a conexao viva
            if msg.type == web.WSMsgType.ERROR:
                break
    finally:
        escritor.cancel()
        app['espectadores'].discard(e)
    return ws


async def api_estado(request):
    q = request.app['estado'].get('quadro')
    return web.json_response(q[0] if q else {'tipo': 'sem fonte'})


async def api_mercado(request):
    est = request.app['estado']
    return web.json_response({'resumo': json.loads(est['resumo']) if est.get('resumo') else None,
                              'ordens': [json.loads(x) for x in list(est['ordens'])[-20:]],
                              'eventos': [json.loads(x) for x in list(est['eventos'])[-40:]]})


async def versao(request):
    """Assinatura da pagina servida: a pagina compara a cada minuto e recarrega sozinha quando muda."""
    import hashlib
    h = hashlib.md5((SITE / 'publico.html').read_bytes() + (SITE / 'fly-cliente.js').read_bytes()).hexdigest()[:12]
    return web.Response(text=h, content_type='text/plain', headers={'Cache-Control': 'no-store'})


async def saude(request):
    est = request.app['estado']
    return web.json_response({'ok': True, 'fonte': est.get('fonte') is not None, 'fonte_ele': est.get('fonte_ele') is not None, 'instancia': INSTANCIA,
                              'fonte_ha_s': round(time.time() - est.get('fonte_t', 0)) if est.get('fonte_t') else None,
                              'viewers': len(request.app['espectadores'])})


async def index(request):
    """Pagina publica com o CA do token e o link do X vindos das variaveis do servico (FLY_CA, FLY_X_URL):
    mudar a variavel no Railway redeploya em um minuto, sem mexer em codigo."""
    host = (request.headers.get('Host') or '').split(':')[0].lower()
    if host == 'sexfly.tech':          # o Railway so aceitou o www: a raiz manda para la
        raise web.HTTPMovedPermanently('https://www.sexfly.tech/')
    html = (SITE / 'publico.html').read_text(encoding='utf-8')
    cfg = request.app['estado'].get('config') or {}
    ca = (cfg.get('ca') or os.environ.get('FLY_CA', '')).strip().replace("'", '')
    x = (cfg.get('x') or os.environ.get('FLY_X_URL', '')).strip().replace("'", '')
    if ca and "const CA='';" in html:
        html = html.replace("const CA='';", f"const CA='{ca}';", 1)
    if x and "const X_URL='';" in html:
        html = html.replace("const X_URL='';", f"const X_URL='{x}';", 1)
    return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-store'})


def main():
    app = web.Application()
    app['espectadores'] = set()
    app['estado'] = {'ola': None, 'quadro': None, 'corpo': None, 'resumo': None, 'eventos': deque(maxlen=200),
                     'ordens': deque(maxlen=50), 'fonte': None, 'fonte_t': 0.0, 'config': None,
                     'fonte_ele': None, 'ola_ele': None, 'quadro_ele': None}
    app.router.add_get('/', index)
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/fonte', fonte)
    app.router.add_post('/fonte/limpar', fonte_limpar)
    app.router.add_get('/api/estado', api_estado)
    app.router.add_get('/api/mercado', api_mercado)
    app.router.add_get('/health', saude)
    app.router.add_get('/api/versao', versao)
    app.router.add_static('/static', SITE, show_index=False)
    print(f'[relay] porta {PORTA}; token {"definido" if TOKEN else "AUSENTE (fonte nao consegue entrar)"}', flush=True)
    web.run_app(app, host='0.0.0.0', port=PORTA, print=None)


if __name__ == '__main__':
    main()
