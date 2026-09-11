# Servidor da mosca: sobe o cerebro na placa, serve o site e transmite os quadros por WebSocket.
# Protocolo do quadro (binario): [uint32 tamanho do JSON][JSON][uint32 x N indices que dispararam]
# Mensagens do cliente (texto JSON): {"estimulo": "sugar", "ms": 500}
import asyncio
import json
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np
from aiohttp import web

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from motor import Cerebro, ESTIMULOS_BLOQUEADOS  # noqa: E402
from relay_cliente import Uplink                 # noqa: E402

PORTA = int(os.environ.get('FLY_PORT', '8435'))
QUEM = os.environ.get('FLY_QUEM', 'ela')                       # 'ela' (femea, corpo e mercado) ou 'ele' (macho): dois processos, um por cerebro
RELAY_URL = os.environ.get('FLY_RELAY_URL', '')            # ex.: wss://fly.up.railway.app/fonte (relay/servidor.py)
RELAY_TOKEN = os.environ.get('FLY_RELAY_TOKEN', '')
AMBIENTE_HZ = float(os.environ.get('FLY_AMBIENTE_HZ', '0'))   # 0: ruido difuso prende a rede em crise
PLASTICIDADE = os.environ.get('FLY_PLASTICIDADE', '1') != '0'
MAX_IDX = int(os.environ.get('FLY_MAX_IDX', '4000'))   # teto de indices por quadro enviado
ROT_RAD_S = float(os.environ.get('FLY_ROT_RAD_S', '0.12'))   # relogio de rotacao compartilhado: cerebro na pagina e camera do corpo
SITE = RAIZ / 'site'
DADOS = RAIZ / 'brain' / 'data'


def delta_varint(idx):
    """Indices ordenados, diferenca para o anterior em varint de 7 bits: ~1 byte por neuronio em vez de 4."""
    d = np.diff(np.concatenate([[0], np.sort(np.asarray(idx, dtype=np.int64))]))
    saida = np.empty((len(d), 3), dtype=np.uint8)
    saida[:, 0] = (d & 0x7F) | np.where(d >= 128, 0x80, 0)
    saida[:, 1] = ((d >> 7) & 0x7F) | np.where(d >= 16384, 0x80, 0)
    saida[:, 2] = (d >> 14) & 0x7F
    mascara = np.stack([np.ones(len(d), dtype=bool), d >= 128, d >= 16384], axis=1)
    return saida[mascara].tobytes()


def empacotar(cab, idx):
    cab['enc'] = 'dv'
    j = json.dumps(cab, separators=(',', ':')).encode('utf-8')
    return struct.pack('<I', len(j)) + j + delta_varint(idx)


def mensagem_ola(app):
    cerebro = app['cerebro']
    return {
        'tipo': 'ola', 'quem': QUEM, 'n': cerebro.n, 'synapses': cerebro.n_sinapses, 'device': cerebro.device,
        'stimuli': {k: {'rate': cerebro.stim_rate[k], 'neurons': int(len(v)),
                        'description': cerebro.stim_desc[k]} for k, v in cerebro.stim_idx.items()
                    if k not in ESTIMULOS_BLOQUEADOS},
        'groups': list(cerebro.grupos), 'born': cerebro.nascimento,
        'plasticity': cerebro.plasticidade, 'ambient_hz': cerebro.ambiente_hz,
    }


ARQ_CONFIG = DADOS / 'estado' / 'config.json'   # so 'ela' grava/serve config, mercado e corpo     # CA do token e link do X (publicos), sobrevivem a reinicio


def ler_config():
    try:
        return json.loads(ARQ_CONFIG.read_text(encoding='utf-8'))
    except Exception:
        return {}


def mensagem_config(app):
    c = app['estado'].get('config') or {}
    return json.dumps({'tipo': 'config', 'ca': c.get('ca', ''), 'x': c.get('x', '')}, separators=(',', ':'))


def snapshot(app):
    """Estado atual para quem acaba de chegar (pagina local ou relay): config, resumo, ultimos eventos, ultimo corpo."""
    itens = [mensagem_config(app)]
    if app['estado'].get('mercado_resumo'):
        itens.append(json.dumps(app['estado']['mercado_resumo'], separators=(',', ':')))
    for ev in list(app['estado']['mercado_eventos'])[-12:]:
        itens.append(json.dumps(ev, separators=(',', ':')))
    if app['estado'].get('corpo'):
        cab, dados = app['estado']['corpo']
        itens.append(empacotar_bytes(cab, dados))
    return itens


async def transmitir(app):
    cerebro = app['cerebro']
    clientes = app['clientes']
    ultimo_t = time.time()
    rng = np.random.default_rng()
    while True:
        await asyncio.sleep(0.02)
        q = None
        while True:      # fica com o quadro mais recente; descarta atrasados
            try:
                q = cerebro.fila.get_nowait()
            except Exception:
                break
        if q is None:
            continue
        agora = time.time()
        idx = q['idx']
        idx_total = int(len(idx))
        if idx_total > MAX_IDX:
            idx = rng.choice(idx, MAX_IDX, replace=False)
        pps = q['passos_por_s'] or 0.0
        cab = {
            'tipo': 'quadro',
            'quem': QUEM,
            't': round(q['t_cerebro'], 3),
            'lived': round(q['vivo_s'], 3),
            'brain_ms': round(q['seg_cerebro'] * 1000, 2),
            'wall_ms': round((agora - ultimo_t) * 1000, 1),
            'steps_per_s': round(pps, 1),
            'ratio': round(10000.0 / pps, 1) if pps > 0 else None,   # s de maquina por s de cerebro
            'spikes': int(q['total']),
            'spikes_per_s': round(q['total'] / q['seg_cerebro'], 1) if q['seg_cerebro'] > 0 else 0,
            'regions': q['regioes'],
            'dn': {k: round(v, 1) for k, v in q['dn'].items()},
            'stimuli': q['ativos'],
            'viewers': max(0, len(clientes) - len(app['sem_corpo'])) + (app['uplink'].viewers if app['uplink'] else 0),
            'relay': bool(app['uplink'] and app['uplink'].ligado),
            'synapses_changed': q['sinapses_mudadas'],
            'idx_total': idx_total,
            'state': q['estado'],
            'seizures': q['crises'],
            'body_age': round(agora - app['estado']['corpo'][0]['recebido'], 1) if app['estado'].get('corpo') else None,
            'body_cfg': app['estado']['corpo_cfg'],
            'rot': round(((agora - app['estado']['t0']) * ROT_RAD_S) % 6.283185307, 4),   # angulo comum (rad)
            'rot_speed': ROT_RAD_S,
        }
        ultimo_t = agora
        app['estado']['ultimo'] = cab
        uplink = app['uplink']
        if not clientes and not (uplink and uplink.ligado):
            continue
        dados = empacotar(cab, idx)
        if uplink:
            uplink.push_bin('quadro', dados)
        mortos = []
        for ws in list(clientes):
            try:
                await ws.send_bytes(dados)
            except Exception:
                mortos.append(ws)
        for ws in mortos:
            clientes.discard(ws)


async def ws_handler(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app['clientes'].add(ws)
    if request.query.get('papel') in ('corpo', 'mercado'):   # processos que so leem taxas: sem quadros do corpo
        app['sem_corpo'].add(ws)
    cerebro = app['cerebro']
    await ws.send_str(json.dumps(mensagem_ola(app)))
    if ws not in app['sem_corpo']:
        for item in snapshot(app):
            if isinstance(item, (bytes, bytearray)):
                await ws.send_bytes(item)
            else:
                await ws.send_str(item)
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except Exception:
                continue
            if 'estimulo' in m:
                ok = cerebro.estimular(str(m['estimulo']), m.get('ms', 500), origem='site')
                await ws.send_str(json.dumps({'tipo': 'ack', 'estimulo': m['estimulo'], 'ok': ok}))
            if 'corpo_tempo' in m:          # escala de tempo do corpo (dev): vai no cabecalho de cada quadro
                try:
                    app['estado']['corpo_cfg']['time_scale'] = max(0.1, min(2.0, float(m['corpo_tempo'])))
                except (TypeError, ValueError):
                    pass
    finally:
        app['clientes'].discard(ws)
        app['sem_corpo'].discard(ws)
    return ws


async def api_estado(request):
    return web.json_response(request.app['estado']['ultimo'] or {'tipo': 'aquecendo'})


async def espalhar_corpo(app, cab, dados):
    """Guarda o quadro do corpo e repassa a todos os espectadores."""
    cab['tipo'] = 'corpo'
    cab['recebido'] = time.time()
    app['estado']['corpo'] = (cab, dados)
    app['estado']['corpo_n'] = app['estado'].get('corpo_n', 0) + 1
    pacote = empacotar_bytes(cab, dados)
    if app['uplink']:
        app['uplink'].push_bin('corpo', pacote)
    for ws in list(app['clientes']):
        if ws in app['sem_corpo']:
            continue
        try:
            await ws.send_bytes(pacote)
        except Exception:
            app['clientes'].discard(ws)


async def corpo_quadro(request):
    """Recebe um quadro JPEG do corpo por HTTP (caminho antigo; o corpo usa /corpo/ws)."""
    dados = await request.read()
    try:
        cab = json.loads(request.headers.get('X-Corpo', '{}'))
    except Exception:
        cab = {}
    await espalhar_corpo(request.app, cab, dados)
    return web.json_response({'ok': True, 'viewers': len(request.app['clientes'])})


async def corpo_ws(request):
    """WebSocket persistente do corpo: cada mensagem binaria e [uint32 tamanho][JSON][JPEG]."""
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    app = request.app
    print('[servidor] corpo conectado por WebSocket', flush=True)
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.BINARY:
                continue
            dados = msg.data
            n = struct.unpack_from('<I', dados, 0)[0]
            try:
                cab = json.loads(dados[4:4 + n].decode('utf-8'))
            except Exception:
                cab = {}
            await espalhar_corpo(app, cab, dados[4 + n:])
    finally:
        print('[servidor] corpo desconectado', flush=True)
    return ws


async def corpo_ultimo(request):
    """Ultimo quadro do corpo como JPEG (para clipes, posts e conferencia)."""
    c = request.app['estado'].get('corpo')
    if not c:
        raise web.HTTPNotFound(text='sem quadro do corpo ainda')
    return web.Response(body=c[1], content_type='image/jpeg', headers={'Cache-Control': 'no-store'})


def empacotar_bytes(cab, dados):
    j = json.dumps(cab, separators=(',', ':')).encode('utf-8')
    return struct.pack('<I', len(j)) + j + dados


async def api_estimulo(request):
    m = await request.json()
    ok = request.app['cerebro'].estimular(str(m.get('estimulo', '')), m.get('ms', 500), origem='api')
    return web.json_response({'ok': ok})


async def api_eventos(request):
    return web.json_response(list(request.app['cerebro'].eventos)[-50:])


async def api_mercado(request):
    """Canal do mercado (mercado/mercado.py): POST guarda e espalha um evento (trade lido, estimulo mandado,
    ordem em papel/real, resumo); GET devolve o resumo e os ultimos eventos para a pagina."""
    app = request.app
    if request.method == 'POST':
        ev = await request.json()
        ev['tipo'] = 'mercado'
        ev['t'] = time.time()
        if ev.get('classe') == 'resumo':
            app['estado']['mercado_resumo'] = ev
        else:
            app['estado']['mercado_eventos'].append(ev)
        pacote = json.dumps(ev, separators=(',', ':'))
        if app['uplink']:
            app['uplink'].push_txt(pacote)
        for ws in list(app['clientes']):
            if ws in app['sem_corpo']:
                continue
            try:
                await ws.send_str(pacote)
            except Exception:
                app['clientes'].discard(ws)
        return web.json_response({'ok': True})
    return web.json_response({'resumo': app['estado'].get('mercado_resumo'),
                              'eventos': list(app['estado']['mercado_eventos'])[-40:]})


async def api_config(request):
    """CA do token e link do X: POST {'ca':..., 'x':...} (so local) guarda, espalha ao vivo para as paginas abertas
    e para o relay (que injeta na pagina de quem chegar depois). Sem redeploy no lancamento."""
    app = request.app
    if request.method == 'POST':
        m = await request.json()
        c = dict(app['estado'].get('config') or {})
        for k in ('ca', 'x'):
            if k in m:
                c[k] = str(m[k]).strip()
        app['estado']['config'] = c
        try:
            ARQ_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            ARQ_CONFIG.write_text(json.dumps(c), encoding='utf-8')
        except Exception as e:
            print(f'[servidor] nao gravei config: {e}', flush=True)
        pacote = mensagem_config(app)
        if app['uplink']:
            app['uplink'].push_txt(pacote)
        for ws in list(app['clientes']):
            if ws in app['sem_corpo']:
                continue
            try:
                await ws.send_str(pacote)
            except Exception:
                app['clientes'].discard(ws)
        print(f'[servidor] config: {c}', flush=True)
    return web.json_response(app['estado'].get('config') or {})


async def api_versao(request):
    """Assinatura da pagina: a pagina compara a cada minuto e recarrega sozinha quando eu publico versao nova."""
    import hashlib
    h = hashlib.md5((SITE / 'publico.html').read_bytes() + (SITE / 'fly-cliente.js').read_bytes()).hexdigest()[:12]
    return web.Response(text=h, content_type='text/plain', headers={'Cache-Control': 'no-store'})


async def api_mercado_limpar(request):
    """Zera o historico de cards (so local; o relay zera ao redeployar). Usado no lancamento para o feed recomecar."""
    request.app['estado']['mercado_eventos'].clear()
    return web.json_response({'ok': True})


async def index(request):
    """Pagina publica (espectador). A de desenvolvimento, com os botoes, fica em /dev."""
    arq = SITE / 'publico.html'
    if not arq.exists():
        arq = SITE / 'index.html'
    return web.FileResponse(arq, headers={'Cache-Control': 'no-store'})


async def dev(request):
    return web.FileResponse(SITE / 'index.html', headers={'Cache-Control': 'no-store'})


async def ao_iniciar(app):
    app['tarefa'] = asyncio.create_task(transmitir(app))
    if RELAY_URL:
        app['uplink'] = Uplink(RELAY_URL, RELAY_TOKEN, lambda: mensagem_ola(app), lambda: snapshot(app))
        app['tarefa_relay'] = asyncio.create_task(app['uplink'].rodar())
        print(f'[servidor] relay: {RELAY_URL}', flush=True)


async def ao_encerrar(app):
    app['tarefa'].cancel()
    if app.get('tarefa_relay'):
        app['tarefa_relay'].cancel()
    app['cerebro'].parar()
    print('[servidor] cerebro salvo e parado')


def reservar_cpu():
    """Nucleos proprios para o cerebro, separados dos do corpo (corpo/corpo.py usa 0xFF00). 0 desliga."""
    if os.name != 'nt':
        return
    import ctypes
    mascara = int(os.environ.get('FLY_CEREBRO_AFINIDADE', '0x00FF'), 0)
    if mascara:
        k32 = ctypes.windll.kernel32
        k32.SetProcessAffinityMask(k32.GetCurrentProcess(), mascara)


def main():
    reservar_cpu()
    cerebro = Cerebro(
        dir_dados=DADOS / 'flywire', dir_estado=DADOS / 'estado' / QUEM,
        meta_parquet=DADOS / 'neuronios.parquet',
        plasticidade=PLASTICIDADE, ambiente_hz=AMBIENTE_HZ,
    )
    cerebro.iniciar()
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app['cerebro'] = cerebro
    app['clientes'] = set()
    app['sem_corpo'] = set()           # conexoes que nao recebem os quadros do corpo (o proprio corpo)
    app['uplink'] = None               # ligacao com o relay publico (criada em ao_iniciar se FLY_RELAY_URL existir)
    app['tarefa_relay'] = None
    from collections import deque
    app['estado'] = {'ultimo': None, 'corpo_cfg': {}, 't0': time.time(),   # dict mutavel (o app nao aceita chaves novas depois)
                     'mercado_resumo': None, 'mercado_eventos': deque(maxlen=200)}
    app.router.add_get('/', index)
    app.router.add_get('/dev', dev)
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/api/estado', api_estado)
    app.router.add_get('/api/eventos', api_eventos)
    app.router.add_get('/api/mercado', api_mercado)
    app.router.add_post('/api/mercado', api_mercado)
    app.router.add_post('/api/mercado/limpar', api_mercado_limpar)
    app.router.add_get('/api/config', api_config)
    app.router.add_get('/api/versao', api_versao)
    app.router.add_post('/api/config', api_config)
    app['estado']['config'] = ler_config()
    app.router.add_post('/api/estimulo', api_estimulo)
    app.router.add_post('/corpo/quadro', corpo_quadro)
    app.router.add_get('/corpo/ws', corpo_ws)
    app.router.add_get('/corpo/ultimo.jpg', corpo_ultimo)
    app.router.add_static('/static', SITE, show_index=False)
    app.on_startup.append(ao_iniciar)
    app.on_shutdown.append(ao_encerrar)
    print(f'[servidor] http://localhost:{PORTA}  (ambiente {AMBIENTE_HZ} Hz, plasticidade {PLASTICIDADE})',
          flush=True)
    web.run_app(app, host='0.0.0.0', port=PORTA, print=None)


if __name__ == '__main__':
    main()
