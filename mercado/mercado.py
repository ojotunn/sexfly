# Mercado -> sentidos -> cerebro -> reflexo -> ordem. Modo PAPEL (saldo virtual), so na Pons.
#
# Leitor: a cada INTERVALO s pega os trades novos do token de maior volume da Pons V2 (GeckoTerminal) e traduz
# cada um em estimulo por uma tabela fixa e publica (compra = acucar, venda = amargo, venda grande = sombra,
# muita transacao = vibracao, preco subindo firme = impulso de andar). Nao decide nada.
# Tradutor de reflexo: le as taxas dos grupos motores do cerebro (as barras da tela) e aplica regras fixas:
# proboscide alta por um tempo compra; fuga ou re vende tudo; o resto segura. Intervalo minimo entre ordens.
# Sem agente, sem IA de linguagem: dado o mesmo mercado e o mesmo cerebro, sai a mesma ordem.
import asyncio
import json
import math
import os
import struct
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sentidos_chain import SentidosChain   # noqa: E402

SERVIDOR = os.environ.get('FLY_SERVIDOR', 'http://localhost:8435')
SERVIDOR_ELE = os.environ.get('FLY_SERVIDOR_ELE', 'http://localhost:8436')   # o cerebro do macho: sente o mercado igual
WS_URL = SERVIDOR.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws?papel=mercado'
GECKO = 'https://api.geckoterminal.com/api/v2/networks/robinhood'
INTERVALO = float(os.environ.get('FLY_MERCADO_INTERVALO', '15'))          # s entre leituras da Pons
POOL = os.environ.get('FLY_MERCADO_POOL', '')                              # vazio: maior volume da Pons V2
MODO = os.environ.get('FLY_MERCADO_MODO', 'papel')                         # papel | real (carteira dela na Pons)
RESERVA_GAS_ETH = float(os.environ.get('FLY_MERCADO_RESERVA_GAS', '0.0015'))  # modo real: nunca gasta a reserva de gas
SLIPPAGE = 0.03                                                            # modo real: minimo aceito = cotacao - 3%
# Valores em DOLAR (como o Michel pensa), convertidos para ETH pelo preco implicito da Pons na hora:
SALDO_USD = float(os.environ.get('FLY_MERCADO_SALDO_USD', '100'))          # saldo virtual inicial (se nao houver o em ETH)
SALDO_ETH = float(os.environ.get('FLY_MERCADO_SALDO_ETH', '0'))            # saldo inicial em ETH; > 0 vence o em dolar (07/09: 0,05)
MAX_ORDEM_USD = float(os.environ.get('FLY_MERCADO_MAX_ORDEM_USD', '5'))     # teto por ordem (compra e venda); 0 = sem teto
ORDEM_FRACAO = float(os.environ.get('FLY_MERCADO_ORDEM', '0.05'))          # fracao do saldo por compra
ORDEM_MIN_USD = 0.50                                                       # abaixo disso nao compra
SALDO0 = 0.0                                                               # em ETH, definido na primeira leitura de preco
MAX_ORDEM_ETH = 0.0
ORDEM_MIN = 0.0
VENDA_AMARGO_FRACAO = 0.25                                                 # regra de ponte: amargo vende 25% do que tem
INTERVALO_ORDEM = float(os.environ.get('FLY_MERCADO_INTERVALO_ORDEM', '20'))   # s entre ordens
REPLAY = os.environ.get('FLY_MERCADO_REPLAY', '1') != '0'                  # mercado quieto: reprisa trades antigos
TAXA = 0.01                                                                # taxa da curva (1%), so para o papel

# regras do reflexo -> ordem (Hz das barras da tela; tempo em segundos de relogio)
REGRAS = {
    'compra': {'grupo': 'feed', 'hz': 30.0, 'segundos': 2.0},
    'venda_fuga': {'grupo': 'escape', 'hz': 40.0, 'segundos': 0.5},
    'venda_re': {'grupo': 'backward', 'hz': 30.0, 'segundos': 0.5},
}
# Regra de ponte (declarada, como o "bitter_escape" do fly-brain): o amargo acende o cerebro mas nao chega
# aos motores lidos; se o amargo ficar ativo por AMARGO_S segundos de relogio e ela tiver tokens, vende
# VENDA_AMARGO_FRACAO do que tem. Na noite de 06/09 ela comprou tudo e nunca vendeu por falta disto.
AMARGO_S = 6.0


def http_json(url, dados=None, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(dados).encode() if dados is not None else None,
                                 headers={'User-Agent': 'fly-mercado/1.0', 'Accept': 'application/json',
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def gecko(caminho):
    try:
        return http_json(GECKO + caminho)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(30)
        return None
    except Exception:
        return None


IGNORAR_ARQ = Path(__file__).resolve().parent / 'ignorar.txt'   # tokens que ela NUNCA opera (o dela): um endereco por linha


def ignorados():
    """Enderecos (token ou curva) fora do alcance dela, relidos a cada escolha: FLY_MERCADO_IGNORAR (virgulas) +
    mercado/ignorar.txt. E assim que o proprio token dela entra na hora do lancamento, sem reiniciar nada."""
    lista = {a.strip().lower() for a in os.environ.get('FLY_MERCADO_IGNORAR', '').split(',') if a.strip()}
    if IGNORAR_ARQ.exists():
        lista |= {l.strip().lower() for l in IGNORAR_ARQ.read_text().splitlines() if l.strip() and not l.startswith('#')}
    return lista


SENTIDOS_ARQ = Path(__file__).resolve().parent / 'sentidos.txt'   # token cujos trades sao os SENTIDOS dela (o dela)
SENTIDOS_INTERVALO = float(os.environ.get('FLY_MERCADO_SENTIDOS_INTERVALO', '4'))   # s entre leituras da chain


def sentidos_config():
    """Endereco do token que ela SENTE (mercado/sentidos.txt ou FLY_MERCADO_SENTIDOS); vazio = sente o que opera."""
    v = os.environ.get('FLY_MERCADO_SENTIDOS', '').strip()
    if SENTIDOS_ARQ.exists():
        linhas = [l.strip() for l in SENTIDOS_ARQ.read_text().splitlines() if l.strip() and not l.startswith('#')]
        v = linhas[0] if linhas else ''
    return v.lower()


ORDENS_ARQ = Path(__file__).resolve().parent / 'ordens.txt'     # 'on' | 'off': liga/desliga as ordens sem reiniciar
ULTIMO_ARQ = Path(__file__).resolve().parent / 'ultimo_token.txt'   # token que ela opera (para retomar apos reinicio)
AJUSTES_ARQ = Path(__file__).resolve().parent / 'ajustes.txt'       # max_ordem_usd=10  lote=0.10  (relido ao vivo)


def ajustes():
    """Teto por ordem (dolares) e lote (fracao do saldo) ajustaveis sem reiniciar: mercado/ajustes.txt."""
    saida = {}
    if AJUSTES_ARQ.exists():
        for linha in AJUSTES_ARQ.read_text().splitlines():
            linha = linha.split('#', 1)[0].strip()
            if '=' in linha:
                k, v = linha.split('=', 1)
                try:
                    saida[k.strip().lower()] = float(v.strip().replace(',', '.'))
                except ValueError:
                    pass
    return saida


def ordens_ativas():
    """Interruptor das ordens (sentidos e tela continuam): FLY_MERCADO_ORDENS (padrao on) ou mercado/ordens.txt.
    Fica em off ate o token dela ser lancado (pedido do Michel, 07/09)."""
    v = os.environ.get('FLY_MERCADO_ORDENS', 'on').strip().lower()
    if ORDENS_ARQ.exists():
        t = ORDENS_ARQ.read_text().strip().lower()
        if t:
            v = t.split()[0]
    return v not in ('off', '0', 'nao', 'no', 'false', 'desligado')


def proibido(c):
    ign = ignorados()
    return (c.get('token') or '').lower() in ign or (c.get('pool') or '').lower() in ign


def _token_de(item):
    """Endereco do token base de um item da GeckoTerminal (id vem como 'robinhood_0x...')."""
    base = (((item.get('relationships') or {}).get('base_token') or {}).get('data') or {}).get('id', '')
    return base.split('_')[-1] if base else ''


def listar_pools():
    """Curvas da Pons V2 por volume de 24 h, as mais movimentadas primeiro, com o endereco do token."""
    d = gecko('/dexes/pons-v2/pools?sort=h24_volume_usd_desc&page=1')
    if not d or not d.get('data'):
        return []
    return [{'pool': p['attributes']['address'], 'nome': p['attributes']['name'].split(' / ')[0],
             'par': p['attributes']['name'], 'token': _token_de(p)} for p in d['data'][:8]]


def ler_pool(pool):
    d = gecko(f'/pools/{pool}')
    if not d or not d.get('data'):
        return None
    a = d['data']['attributes']
    return {'preco_eth': float(a.get('base_token_price_native_currency') or 0),
            'preco_usd': float(a.get('base_token_price_usd') or 0),
            'vol_h1': float((a.get('volume_usd') or {}).get('h1') or 0),
            'vol_h24': float((a.get('volume_usd') or {}).get('h24') or 0),
            'tx_h1': (a.get('transactions') or {}).get('h1') or {},
            'token': _token_de(d['data'])}


def ler_trades(pool):
    d = gecko(f'/pools/{pool}/trades')
    if not d or not d.get('data'):
        return []
    ts = []
    for t in d['data']:
        a = t['attributes']
        ts.append({'tx': a['tx_hash'], 'kind': a['kind'], 'usd': float(a.get('volume_in_usd') or 0),
                   'de': a.get('tx_from_address') or '', 'quando': a['block_timestamp'],
                   'preco_usd': float(a.get('price_to_in_usd' if a['kind'] == 'buy' else 'price_from_in_usd') or 0)})
    ts.sort(key=lambda x: x['quando'])
    return ts


def ms_por_valor(usd):
    """Duracao do estimulo em ms de cerebro: 150 ms para centavos, ~1,1 s para $100, teto 2,5 s."""
    return float(max(150.0, min(2500.0, 150.0 + 320.0 * math.log10(1.0 + max(0.0, usd)))))


def limiares(historico):
    """p50 e p90 do valor em dolar dos trades recentes: 'grande' e relativo ao token, nao um numero fixo."""
    v = sorted(t['usd'] for t in historico if t['usd'] > 0)
    if len(v) < 10:
        return 20.0, 100.0
    return v[len(v) // 2], v[int(len(v) * 0.9)]


def traduzir_trade(t, p50, p90, novo_holder):
    """Mapa publico mercado -> sentidos. Devolve lista de (estimulo, ms) e um rotulo curto para o card.
    compra = acucar (duracao pelo valor); compra grande ou holder novo = + dopamina;
    venda = amargo; venda media = + empurrao de re; venda grande = + sombra."""
    ms = ms_por_valor(t['usd'])
    if t['kind'] == 'buy':
        lista = [('sugar', ms)]
        extras = []
        if t['usd'] >= p90:
            lista.append(('reward', 500.0)); extras.append('big buy → dopamine')
        elif novo_holder:
            lista.append(('reward', 300.0)); extras.append('new holder → dopamine')
        return lista, 'sugar', extras
    lista = [('bitter', ms)]
    extras = []
    if t['usd'] >= p90:
        lista.append(('lc4', 400.0)); extras.append('big sell → shadow')
    elif t['usd'] >= p50:
        lista.append(('mdn', 300.0)); extras.append('sell → backs away')
    return lista, 'bitter', extras


def estimular(nome, ms, quem='ambos'):
    """Manda o estimulo para o cerebro dela, dele ou dos dois (os dois sentem o mercado)."""
    ok = False
    for alvo in (['ela', 'ele'] if quem == 'ambos' else [quem]):
        try:
            http_json((SERVIDOR if alvo == 'ela' else SERVIDOR_ELE) + '/api/estimulo', {'estimulo': nome, 'ms': ms}, timeout=5)
            ok = True
        except Exception:
            pass
    return ok


class Libido:
    """Medidor do cruzamento: compra sobe (pelo tamanho relativo ao p90), venda derruba, decai em ~90 s.
    Estados: idle (< 0.06) -> courting (< 0.18) -> mating; venda grande = chute (rejected) por 6 s."""
    def __init__(self):
        self.v = 0.0; self.estado = 'idle'; self.chute_ate = 0.0; self.t_pub = 0.0; self.v_pub = -1.0; self.est_pub = ''
        self.ultimo_t = time.time(); self.prox_canto = 0.0

    def compra(self, usd, p90):
        self.v = min(1.0, self.v + 0.12 + 0.28 * min(1.0, usd / max(p90, 1e-9)))

    def venda(self, usd, p50, p90):
        if usd >= p90:
            self.v = max(0.0, self.v - 0.6); self.chute_ate = time.time() + 6.0
            return 'chute'
        self.v = max(0.0, self.v - (0.25 if usd >= p50 else 0.10))
        return 'esfria'

    def passo(self):
        agora = time.time(); dt = agora - self.ultimo_t; self.ultimo_t = agora
        self.v = max(0.0, self.v - dt / 180.0)          # decai em ~3 min: sem compras eles vao devagar, nao param
        if agora < self.chute_ate:
            novo = 'rejected'
        elif agora < self.chute_ate + 5.0:               # depois do chute ele volta cantando por 5 s e monta de novo
            novo = 'courting'
        else:
            novo = 'mating'                              # cruzamento e o estado padrao: as compras so aceleram
        mudou = novo != self.estado; self.estado = novo
        return mudou

    def ritmo_hz(self):
        return round(1.0 + 5.0 * self.v, 2)

    def evento(self, extra=None):
        ev = {'classe': 'sexo', 'libido': round(self.v, 3), 'estado': self.estado, 'ritmo_hz': self.ritmo_hz()}
        if extra:
            ev['evento'] = extra
        return ev


def publicar(ev):
    try:
        http_json(SERVIDOR + '/api/mercado', ev, timeout=5)
    except Exception:
        pass


# ----- leitura das taxas do cerebro (thread) -----
def leitor_ws(estado):
    import websockets

    async def laco():
        while True:
            try:
                async with websockets.connect(WS_URL, max_size=8_000_000) as ws:
                    estado['ligado'] = True
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            n = struct.unpack_from('<I', msg, 0)[0]
                            cab = json.loads(bytes(msg[4:4 + n]).decode('utf-8'))
                            if cab.get('tipo') == 'quadro':
                                estado['dn'] = cab.get('dn', {})
                                estado['estimulos'] = cab.get('stimuli', [])
                                estado['cerebro_estado'] = cab.get('state')
                                estado['dn_t'] = time.time()
            except Exception as e:
                estado['ligado'] = False
                estado['erro'] = str(e)[:80]
                await asyncio.sleep(2)

    asyncio.run(laco())


class Carteira:
    """Saldo virtual. Compra e venda a preco da curva com a taxa de 1%."""

    def __init__(self, eth):
        self.eth0 = eth
        self.eth = eth
        self.tokens = 0.0
        self.ordens = 0
        self.endereco = ''
        self.ultima_tx = ''
        self.eventos = []

    def disponivel(self):
        return self.eth

    def comprar(self, eth, preco):
        if eth <= 0 or preco <= 0 or self.eth < eth:
            return False
        self.eth -= eth
        self.tokens += eth * (1 - TAXA) / preco
        self.ordens += 1
        return True

    def vender(self, preco, fracao=1.0):
        if self.tokens <= 0 or preco <= 0:
            return 0.0
        qtd = self.tokens * max(0.0, min(1.0, fracao))
        if MAX_ORDEM_ETH > 0:
            qtd = min(qtd, MAX_ORDEM_ETH / preco)      # teto por ordem, em ETH equivalente
        recebido = qtd * preco * (1 - TAXA)
        self.eth += recebido
        self.tokens -= qtd
        self.ordens += 1
        return recebido

    def vender_tudo(self, preco):
        return self.vender(preco, 1.0)

    def valor(self, preco):
        return self.eth + self.tokens * preco


class CarteiraReal:
    """Carteira dela na Robinhood Chain: saldo lido da chain, compra e venda na curva da Pons V2 (mercado/pons.py).
    Mesma interface da Carteira de papel. Nao tem funcao de saque: daqui so sai compra e venda na curva."""

    def __init__(self, chain, conta):
        self.chain = chain
        self.conta = conta
        self.endereco = conta.address
        self.token = None
        self.curva = None
        self.decimais = 18
        self.eth = chain.saldo_eth(self.endereco)
        self.eth0 = self.eth                  # base do PnL; depositos e saques do Michel movem esta base
        self.esperado = self.eth              # saldo que a ultima leitura/ordem deixou; diferenca = deposito ou saque
        self.tokens_raw = 0
        self.tokens = 0.0
        self.ordens = 0
        self.ultima_tx = ''
        self.erro_ate = 0.0                   # depois de uma falha, espera 2 min antes de tentar de novo
        self.eventos = []                     # (tipo, dado) que o laco principal publica como card

    def usar_token(self, token):
        """Adota o token se a curva dele estiver ativa (fase 0). Graduado ou desconhecido: False."""
        if not token:
            return False
        try:
            l = self.chain.lancamento(token)
            if not l['na_curva']:
                return False
            self.token, self.curva = l['token'], l['curva']
            self.decimais = int(self.chain.erc20(self.token).functions.decimals().call())
            self.atualizar()
            return True
        except Exception as e:
            self.eventos.append(('error', f'could not read {token[:10]} on chain: {str(e)[:60]}'))
            return False

    def ainda_na_curva(self):
        try:
            return self.chain.lancamento(self.token)['na_curva']
        except Exception:
            return True

    def largar_token(self):
        self.token = self.curva = None
        self.tokens_raw, self.tokens = 0, 0.0

    def atualizar(self):
        eth = self.chain.saldo_eth(self.endereco)
        delta = eth - self.esperado
        if abs(delta) > 1e-6:                 # mexeram na carteira sem ser ordem dela: deposito ou saque do Michel
            self.eth0 += delta
            self.eventos.append(('deposit' if delta > 0 else 'withdrawal', delta))
        self.eth = self.esperado = eth
        if self.token:
            self.tokens_raw = int(self.chain.saldo_token(self.token, self.endereco))
            self.tokens = self.tokens_raw / 10 ** self.decimais
        try:   # contagem de ordens vem da chain (nonce), nao de um contador que zera a cada reinicio
            self.ordens = max(0, int(self.chain.w3.eth.get_transaction_count(self.endereco)) - int(ajustes().get('nonce_base', 0)))   # nonce_base em ajustes.txt: o que veio antes deste lancamento nao conta
        except Exception:
            pass

    def _apos_tx(self):
        antes = self.esperado
        novo = self.chain.saldo_eth(self.endereco)
        self.eth = self.esperado = novo
        self.tokens_raw = int(self.chain.saldo_token(self.token, self.endereco))
        self.tokens = self.tokens_raw / 10 ** self.decimais
        return novo - antes                   # compra: -(eth + gas); venda: recebido - gas

    def disponivel(self):
        return max(0.0, self.eth - RESERVA_GAS_ETH)

    def comprar(self, eth, preco):
        if not self.curva or eth <= 0 or time.time() < self.erro_ate:
            return False
        eth = min(eth, self.disponivel())
        if eth < ORDEM_MIN * 0.5:
            return False
        try:
            e = self.chain.estado_curva(self.curva, self.endereco)
            if e['graduated']:
                raise RuntimeError('curve graduated')
            wei = int(eth * 1e18)
            cot = self.chain.cotar_compra(wei, e['R'], e['T'], e['sellable'], e['feeBps'], e['taxBps'], e['snipeBps'])
            if cot <= 0:
                raise RuntimeError('zero quote')
            self.ultima_tx, _ = self.chain.comprar(self.conta, self.curva, eth, int(cot * (1 - SLIPPAGE)))
        except Exception as ex:
            self.erro_ate = time.time() + 120
            self.eventos.append(('error', f'buy failed: {str(ex)[:90]}'))
            return False
        self.ordens += 1
        self._apos_tx()
        return True

    def vender(self, preco, fracao=1.0):
        if not self.curva or self.tokens_raw <= 0 or time.time() < self.erro_ate:
            return 0.0
        qtd = int(self.tokens_raw * max(0.0, min(1.0, fracao)))
        if MAX_ORDEM_ETH > 0 and preco > 0:
            qtd = min(qtd, int(MAX_ORDEM_ETH / preco * 10 ** self.decimais))   # teto por ordem
        if preco > 0 and (self.tokens_raw - qtd) * preco / 10 ** self.decimais < ORDEM_MIN * 0.5:
            qtd = self.tokens_raw             # nao deixa poeira: se o resto valeria centavos, vai tudo
        if qtd <= 0:
            return 0.0
        try:
            e = self.chain.estado_curva(self.curva, self.endereco)
            if e['graduated']:
                raise RuntimeError('curve graduated')
            cot = self.chain.cotar_venda(qtd, e['R'], e['T'], e['feeBps'], e['taxBps'])
            self.ultima_tx, _ = self.chain.vender(self.conta, self.curva, self.token, qtd, int(cot * (1 - SLIPPAGE)))
        except Exception as ex:
            self.erro_ate = time.time() + 120
            self.eventos.append(('error', f'sell failed: {str(ex)[:90]}'))
            return 0.0
        self.ordens += 1
        return max(0.0, self._apos_tx())

    def vender_tudo(self, preco):
        return self.vender(preco, 1.0)

    def valor(self, preco):
        return self.eth + self.tokens * preco


def main():
    global SALDO0, MAX_ORDEM_ETH, ORDEM_MIN, MAX_ORDEM_USD, ORDEM_FRACAO
    sys.stdout.reconfigure(errors='replace')   # nome de token com emoji nao pode derrubar o console cp1252
    estado = {'dn': {}, 'estimulos': [], 'ligado': False}
    threading.Thread(target=leitor_ws, args=(estado,), name='ws', daemon=True).start()
    carteira = None                        # papel: criada na primeira leitura de preco (conversao dolar -> ETH)
    if MODO == 'real':
        import carteira as mod_carteira
        import pons
        carteira = CarteiraReal(pons.Pons(), mod_carteira.carregar())
        print(f'[mercado] carteira REAL {carteira.endereco}: {carteira.eth:.5f} ETH; reserva de gas {RESERVA_GAS_ETH} ETH', flush=True)
    calibrado = False                      # teto e minimo por ordem calculados em ETH na primeira leitura de preco
    eth_usd = 0.0
    pool = {'pool': POOL, 'nome': '?', 'par': '?', 'token': ''} if POOL else None
    if MODO == 'real' and pool is None and ULTIMO_ARQ.exists():
        # reinicio segurando tokens: continua no mesmo token em vez de abandonar a posicao
        addr = ULTIMO_ARQ.read_text().strip()
        if addr and addr.lower() not in ignorados() and carteira.usar_token(addr) and carteira.tokens_raw > 0:
            try:
                nome_tok = str(carteira.chain.erc20(carteira.token).functions.symbol().call())
            except Exception:
                nome_tok = addr[:8]
            pool = {'pool': carteira.curva, 'nome': nome_tok, 'par': f'{nome_tok} / WETH', 'token': addr}
            print(f'[mercado] retomando {nome_tok}: ela ainda tem {carteira.tokens:,.0f} tokens', flush=True)
    ultima_escolha = 0.0
    ultima_carteira = 0.0
    ultima_curva = 0.0
    ativas = None                          # estado do interruptor das ordens (card quando muda)
    ultimo_posicao = 0.0                   # ultimo sinal vindo do token que ela segura
    libido = Libido()                      # cruzamento: compras sobem, vendas derrubam
    sent = None                            # feed proprio dos sentidos (SentidosChain) ou None
    sent_cfg = ''
    prox_sent_tentativa = 0.0
    ultima_sent = 0.0
    precos_sent = deque(maxlen=90)
    chain_leitura = None

    def processar(novos, trades):
        """Trades novos viram estimulos e cards; a lista recente vira os sinais de vibracao e sombra."""
        nonlocal ultimo_evento, ultimo_trade_real, ultimo_estimulo, ultimo_jo, ultimo_lc4
        agora = time.time()
        if novos:
            ultimo_evento = agora
        p50, p90 = limiares(historico)
        rotulo_token = sent.nome if sent is not None else pool['nome']
        for t in novos:
            historico.append(t)
            ultimo_trade_real = agora
            if t['kind'] == 'buy':
                libido.compra(t['usd'], p90)
                if libido.estado in ('courting', 'mating'):
                    estimular('jo', 300.0, 'ela'); estimular('pc1', 250.0, 'ela')     # cancao dele no orgao de Johnston + receptividade
            else:
                if libido.venda(t['usd'], p50, p90) == 'chute':
                    estimular('reject', 400.0, 'ela'); estimular('lc4', 400.0, 'ele')  # ela rejeita (DNp13), ele ve a sombra
                    publicar(libido.evento('big sell: she kicks him off'))
            novo_holder = t['kind'] == 'buy' and t['de'] not in enderecos
            enderecos.add(t['de'])
            lista, nome, extras = traduzir_trade(t, p50, p90, novo_holder)
            for est, ms in lista:
                estimular(est, ms)
            ultimo_estimulo = (f'{t["kind"]} ${t["usd"]:,.2f}', agora)
            publicar({'classe': 'trade', 'kind': t['kind'], 'usd': round(t['usd'], 2), 'de': t['de'][:10],
                      'tx': t['tx'], 'estimulo': nome, 'ms': round(lista[0][1]), 'extra': ' + '.join(extras) or None,
                      'replay': False, 'quando': t['quando'], 'token': rotulo_token, 'fonte': t.get('fonte', 'gecko')})
        # muita transacao no ultimo minuto: vibracao (uma vez por minuto); onda de vendas: sombra
        recentes = [t for t in trades if t['quando'] >= time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(agora - 60))]
        if len(recentes) >= 6 and agora - ultimo_jo > 60:
            ultimo_jo = agora
            estimular('jo', 300.0)
            publicar({'classe': 'sinal', 'texto': f'{len(recentes)} trades in a minute → vibration', 'estimulo': 'jo'})
        vendas_min = [t for t in recentes if t['kind'] == 'sell']
        if len(vendas_min) >= 4 and agora - ultimo_lc4 > 120:
            ultimo_lc4 = agora
            estimular('lc4', 500.0)
            publicar({'classe': 'sinal', 'texto': f'{len(vendas_min)} sells in a minute → shadow', 'estimulo': 'lc4'})

    def tendencia_posicao(serie):
        """O token que ela OPERA (e segura): cair 2 % em 5 min = empurrao de re; cair 5 % = sombra (fuga = vende).
        Regra nova depois do dia 1 (07/09): ela so sentia o token do Michel e ficou sentada em cima do CAB."""
        nonlocal ultimo_posicao
        agora = time.time()
        antigos = [p for tt, p in serie if agora - tt >= 300]
        atual = serie[-1][1] if serie else 0.0
        if not antigos or atual <= 0 or agora - ultimo_posicao < 120 or carteira.tokens <= 0:
            return
        var = atual / antigos[0] - 1
        if var <= -0.05:
            ultimo_posicao = agora
            estimular('lc4', 500.0)
            publicar({'classe': 'sinal', 'texto': f'{pool["nome"]}, which she holds, {var * 100:.1f}% in 5 min → shadow', 'estimulo': 'lc4'})
        elif var <= -0.02:
            ultimo_posicao = agora
            estimular('mdn', 400.0)
            publicar({'classe': 'sinal', 'texto': f'{pool["nome"]}, which she holds, {var * 100:.1f}% in 5 min → backs away', 'estimulo': 'mdn'})

    def tendencia(serie):
        """Preco em 5 min: subindo firme = impulso de andar; caindo firme = empurrao de re (a cada 2 min)."""
        nonlocal ultimo_p9
        agora = time.time()
        antigos = [p for tt, p in serie if agora - tt >= 300]
        atual = serie[-1][1] if serie else 0.0
        if antigos and atual > 0 and agora - ultimo_p9 > 120:
            var = atual / antigos[0] - 1
            if var >= 0.02:
                ultimo_p9 = agora
                estimular('p9', 400.0)
                publicar({'classe': 'sinal', 'texto': f'price +{var * 100:.1f}% in 5 min → walk drive', 'estimulo': 'p9'})
            elif var <= -0.02:
                ultimo_p9 = agora
                estimular('mdn', 400.0)
                publicar({'classe': 'sinal', 'texto': f'price {var * 100:.1f}% in 5 min → backs away', 'estimulo': 'mdn'})

    vistos = set()
    historico = deque(maxlen=600)          # trades ja lidos (para replay)
    precos = deque(maxlen=60)              # (t, preco_eth) para a tendencia
    ultimo_trade_real = time.time()
    ultima_leitura = 0.0
    ultimo_resumo = 0.0
    ultima_ordem = 0.0
    ultimo_jo = 0.0
    ultimo_p9 = 0.0
    ultimo_lc4 = 0.0
    ultimo_poeira = 0.0
    ultimo_evento = time.time()
    enderecos = set()            # carteiras ja vistas (holder novo = primeira compra)
    enderecos_replay = set()
    ultimo_estimulo = ('none', 0.0)
    acima_desde = {}
    preco = 0.0
    info = {}
    replay_fila = deque()
    prox_replay = 0.0
    print(f'[mercado] modo {MODO}; lote {ORDEM_FRACAO:.0%} do saldo; teto ${MAX_ORDEM_USD:.0f} por ordem; '
          f'leitura a cada {INTERVALO:.0f} s', flush=True)

    while True:
        agora = time.time()
        # ----- escolha do token: maior volume da Pons V2 -----
        # (modo real: nao troca enquanto estiver segurando tokens; so adota curva ativa, fase 0)
        segurando = MODO == 'real' and carteira.tokens_raw > 0
        if (pool is None or (agora - ultimo_trade_real > 3600 and not POOL and not segurando)) and agora - ultima_escolha > 300:
            ultima_escolha = agora
            if POOL:
                candidatos = [pool if pool.get('token') else dict(pool, token=(ler_pool(POOL) or {}).get('token', ''))]
            else:
                candidatos = listar_pools()
            novo = None
            for c in candidatos:
                if proibido(c):
                    continue                          # o token dela (ou outro vetado): nunca
                if MODO == 'real' and not carteira.usar_token(c.get('token')):
                    continue
                novo = c
                break
            if novo and (pool is None or novo['pool'] != pool['pool'] or not pool.get('token')):
                pool = novo
                vistos.clear()
                try:
                    ULTIMO_ARQ.write_text(pool.get('token', ''))     # para um reinicio nao abandonar a posicao
                except Exception:
                    pass
                print(f'[mercado] token escolhido: {pool["nome"]} ({pool["par"]}) pool {pool["pool"]} token {pool.get("token")}', flush=True)
                publicar({'classe': 'info', 'texto': f'watching {pool["nome"]} on Pons, the busiest curve right now'})
        if pool is not None and agora - ultima_leitura >= INTERVALO and proibido(pool):
            # o token que ela olha entrou na lista de vetados (o dela acabou de ser lancado): larga na hora
            print(f'[mercado] {pool["nome"]} entrou na lista de vetados; ela larga e escolhe outro', flush=True)
            publicar({'classe': 'info', 'texto': f'{pool["nome"]} is off limits for her (her own token is never traded) · she moves on'})
            if MODO == 'real':
                carteira.largar_token()
            pool = None
            ultima_escolha = 0.0
            continue
        if pool is None:
            time.sleep(INTERVALO)
            continue

        # ----- cruzamento: decaimento, estados e evento para a pagina -----
        mudou = libido.passo()
        if mudou or agora - libido.t_pub >= 3.0 or abs(libido.v - libido.v_pub) >= 0.05:
            libido.t_pub = agora; libido.v_pub = libido.v
            extra = None
            if mudou:
                extra = {'mating': 'she lets him mount', 'courting': 'he sings with one wing', 'idle': 'they rest', 'rejected': 'rejected'}.get(libido.estado)
            publicar(libido.evento(extra))
            if libido.estado == 'mating' and agora >= libido.prox_canto:            # enquanto cruzam: dopamina nos dois, no ritmo
                libido.prox_canto = agora + max(2.0, 8.0 - 6.0 * libido.v)
                estimular('reward', int(150 + 350 * libido.v), 'ambos')
        # ----- leitura da Pons -----
        if agora - ultima_leitura >= INTERVALO:
            ultima_leitura = agora
            info = ler_pool(pool['pool']) or info
            if info.get('preco_eth'):
                preco = info['preco_eth']
                precos.append((agora, preco))
                if info.get('preco_usd'):
                    eth_usd = info['preco_usd'] / preco
                if not calibrado and eth_usd > 0:
                    calibrado = True
                    MAX_ORDEM_ETH = MAX_ORDEM_USD / eth_usd
                    ORDEM_MIN = ORDEM_MIN_USD / eth_usd
                    if carteira is None:
                        carteira = Carteira(SALDO_ETH if SALDO_ETH > 0 else SALDO_USD / eth_usd)
                    SALDO0 = carteira.eth0
                    rotulo = 'her wallet on Robinhood Chain' if MODO == 'real' else 'her paper wallet'
                    print(f'[mercado] ETH a ${eth_usd:,.0f}: saldo {SALDO0:.4f} ETH = ${SALDO0 * eth_usd:.0f}; '
                          f'teto ${MAX_ORDEM_USD:.0f} = {MAX_ORDEM_ETH:.5f} ETH por ordem', flush=True)
                    publicar({'classe': 'info', 'texto': f'{rotulo}: {SALDO0:.4f} ETH (${SALDO0 * eth_usd:.0f}), max ${MAX_ORDEM_USD:.0f} per order'})
            if carteira is None or not calibrado:
                time.sleep(1.0)
                continue
            if sent is None:                       # sem feed proprio: sente o token que opera (GeckoTerminal)
                trades = ler_trades(pool['pool'])
                novos = [t for t in trades if t['tx'] not in vistos]
                for t in trades:
                    vistos.add(t['tx'])
                if len(vistos) > 5000:
                    vistos = set(t['tx'] for t in trades)
                if not historico and trades:
                    historico.extend(trades)           # primeira leitura: guarda para o replay, sem estimular
                    enderecos.update(t['de'] for t in trades)
                    novos = []
                processar(novos, trades)
                tendencia(precos)
            else:
                tendencia_posicao(precos)              # o token que ela segura tambem e sentido (queda = re/sombra)
        # ----- sentidos direto da chain (o token dela): liga/desliga por mercado/sentidos.txt, sem reiniciar -----
        cfg = sentidos_config()
        if calibrado and cfg != sent_cfg and agora >= prox_sent_tentativa:
            if cfg:
                try:
                    if chain_leitura is None:
                        import pons
                        chain_leitura = pons.Pons()
                    sent = SentidosChain(chain_leitura, cfg, eth_usd)
                    sent_cfg = cfg
                    historico.clear(); enderecos.clear(); replay_fila.clear(); precos_sent.clear()
                    historico.extend(sent.historico)                 # trades recentes da chain: material do replay
                    enderecos.update(t['de'] for t in sent.historico)
                    print(f'[mercado] sentidos: {sent.nome} direto da chain (curva {sent.curva}); '
                          f'{len(sent.historico)} trades recentes carregados para o replay', flush=True)
                    publicar({'classe': 'info', 'texto': f'she now feels every trade of {sent.nome}, straight from the chain'})
                except Exception as e:
                    prox_sent_tentativa = agora + 30
                    print(f'[mercado] feed da chain falhou para {cfg}: {str(e)[:80]}; tento em 30 s', flush=True)
            else:
                sent, sent_cfg = None, ''
                historico.clear(); enderecos.clear(); replay_fila.clear()
                publicar({'classe': 'info', 'texto': f'she feels the trades of {pool["nome"]} again'})
        if sent is not None and agora - ultima_sent >= SENTIDOS_INTERVALO:
            ultima_sent = agora
            novos = sent.ler(eth_usd)
            if sent.preco_eth > 0:
                precos_sent.append((agora, sent.preco_eth))
            processar(novos, list(sent.historico))
            tendencia(precos_sent)

        # mercado parado ha 4 min: poeira assenta nos olhos (limpeza), a cada 4 min
        if agora - ultimo_evento > 240 and agora - ultimo_poeira > 240:
            ultimo_poeira = agora
            estimular('eye_touch', 400.0)
            publicar({'classe': 'sinal', 'texto': 'nothing for 4 min → dust on her eyes', 'estimulo': 'eye_touch'})

        if carteira is None or not calibrado:
            time.sleep(1.0)
            continue
        # ----- modo real: saldo na chain a cada 30 s (deposito do Michel vira card); curva a cada 5 min -----
        if MODO == 'real' and agora - ultima_carteira >= 30:
            ultima_carteira = agora
            try:
                carteira.atualizar()
            except Exception as e:
                print(f'[mercado] leitura da chain falhou: {str(e)[:80]}', flush=True)
            if agora - ultima_curva >= 300:
                ultima_curva = agora
                if carteira.token and not carteira.ainda_na_curva():
                    publicar({'classe': 'info', 'texto': f'{pool["nome"]} graduated off the curve · she moves on'})
                    print(f'[mercado] {pool["nome"]} graduou; trocando de token', flush=True)
                    carteira.largar_token()
                    pool = None
                    ultima_escolha = 0.0
                    continue
            for tipo, dado in carteira.eventos:
                if tipo == 'deposit':
                    publicar({'classe': 'info', 'texto': f'wallet topped up: +{dado:.4f} ETH (${dado * eth_usd:.2f})'})
                elif tipo == 'withdrawal':
                    publicar({'classe': 'info', 'texto': f'{-dado:.4f} ETH left her wallet (${-dado * eth_usd:.2f})'})
                else:
                    publicar({'classe': 'info', 'texto': str(dado)})
                print(f'[mercado] {tipo}: {dado}', flush=True)
            carteira.eventos.clear()
        # ----- mercado quieto: replay de trades antigos, marcado como replay -----
        if REPLAY and historico and agora - ultimo_trade_real > 90 and agora >= prox_replay:
            prox_replay = agora + 8.0
            if not replay_fila:
                replay_fila.extend(list(historico))      # historico inteiro, na ordem em que aconteceu
            t = replay_fila.popleft()
            p50, p90 = limiares(historico)
            lista, nome, extras = traduzir_trade(t, p50, p90, t['de'] not in enderecos_replay)
            enderecos_replay.add(t['de'])
            for est, ms in lista:
                estimular(est, ms)
            ultimo_estimulo = (f'replay {t["kind"]} ${t["usd"]:,.2f}', agora)
            ultimo_evento = agora
            publicar({'classe': 'trade', 'kind': t['kind'], 'usd': round(t['usd'], 2), 'de': t['de'][:10],
                      'tx': t['tx'], 'estimulo': nome, 'ms': round(lista[0][1]), 'extra': ' + '.join(extras) or None,
                      'replay': True, 'quando': t['quando'], 'token': sent.nome if sent is not None else pool['nome']})

        # ----- teto por ordem e lote (relidos a cada volta; mudam sem reiniciar) -----
        aj = ajustes()
        novo_max, novo_lote = aj.get('max_ordem_usd', MAX_ORDEM_USD), aj.get('lote', ORDEM_FRACAO)
        if (novo_max, novo_lote) != (MAX_ORDEM_USD, ORDEM_FRACAO):
            MAX_ORDEM_USD, ORDEM_FRACAO = novo_max, novo_lote
            if eth_usd > 0:
                MAX_ORDEM_ETH = MAX_ORDEM_USD / eth_usd
            print(f'[mercado] ajuste: teto ${MAX_ORDEM_USD:.0f} por ordem, lote {ORDEM_FRACAO:.0%}', flush=True)
            publicar({'classe': 'info', 'texto': f'her order size changed: {ORDEM_FRACAO:.0%} of her ETH, max ${MAX_ORDEM_USD:.0f} per order'})
        elif eth_usd > 0 and MAX_ORDEM_USD > 0:
            MAX_ORDEM_ETH = MAX_ORDEM_USD / eth_usd     # acompanha o preco do ETH
        # ----- interruptor das ordens (relido a cada volta; muda sem reiniciar) -----
        agora_ativas = ordens_ativas()
        if agora_ativas != ativas:
            ativas = agora_ativas
            print(f'[mercado] ordens {"LIGADAS" if ativas else "DESLIGADAS"}', flush=True)
            publicar({'classe': 'info', 'texto': 'orders are ON: her reflexes now place real orders' if ativas
                      else 'orders are paused until the token launches · she still feels every trade'})
        if not ativas:
            acima_desde.clear()            # sem cronometro acumulado: quando ligar, comeca do zero
        # reprise nao compra (regra do dia 1: um lancamento so tem compras, a reprise so dava acucar e ela comprou
        # ate acabar o ETH). Com o mercado em silencio ela reage na tela, mas so abre posicao com trade AO VIVO.
        # Vender continua sempre liberado.
        em_replay = bool(REPLAY and historico and agora - ultimo_trade_real > 90)
        if em_replay:
            acima_desde.pop('compra', None)
        # ----- reflexo -> ordem (regras fixas sobre as barras dos grupos motores) -----
        dn = estado.get('dn', {})
        fresco = agora - estado.get('dn_t', 0) < 5
        for nome_regra, r in REGRAS.items():
            hz = float(dn.get(r['grupo'], 0.0)) if fresco else 0.0
            if hz >= r['hz']:
                acima_desde.setdefault(nome_regra, agora)
            else:
                acima_desde.pop(nome_regra, None)
        # amargo ativo (regra de ponte): conta o tempo de relogio com 'bitter' na lista de estimulos ativos
        if fresco and 'bitter' in (estado.get('estimulos') or []):
            acima_desde.setdefault('amargo', agora)
        else:
            acima_desde.pop('amargo', None)
        if ativas and preco > 0 and agora - ultima_ordem >= INTERVALO_ORDEM:
            if not em_replay and 'compra' in acima_desde and agora - acima_desde['compra'] >= REGRAS['compra']['segundos']:
                dur = agora - acima_desde['compra']
                lote = max(ORDEM_MIN, carteira.disponivel() * ORDEM_FRACAO)
                if MAX_ORDEM_ETH > 0:
                    lote = min(lote, MAX_ORDEM_ETH)
                if carteira.comprar(lote, preco):
                    ultima_ordem = agora
                    acima_desde.pop('compra', None)
                    publicar({'classe': 'ordem', 'lado': 'buy', 'eth': round(lote, 6), 'preco_eth': preco, 'modo': MODO,
                              'motivo': f'proboscis {dn.get("feed", 0):.0f} Hz for {dur:.1f}s after {ultimo_estimulo[0]}',
                              'tokens': carteira.tokens, 'saldo_eth': carteira.eth, 'tx': carteira.ultima_tx, 'token_ordem': pool['nome']})
                    print(f'[mercado] COMPRA {MODO} {lote:.5f} ETH @ {preco:.3e} ({dn.get("feed", 0):.0f} Hz por {dur:.1f}s) {carteira.ultima_tx}', flush=True)
                elif MODO == 'real':
                    acima_desde.pop('compra', None)       # falhou (card de erro ja saiu); nao insiste no mesmo segundo
            if 'amargo' in acima_desde and agora - acima_desde['amargo'] >= AMARGO_S and carteira.tokens > 0:
                recebido = carteira.vender(preco, VENDA_AMARGO_FRACAO)
                ultima_ordem = agora
                acima_desde.pop('amargo', None)
                if recebido > 0:
                    publicar({'classe': 'ordem', 'lado': 'sell', 'eth': round(recebido, 6), 'preco_eth': preco, 'modo': MODO,
                              'motivo': f'bitter taste for {AMARGO_S:.0f}s after {ultimo_estimulo[0]} (bridge rule: sells {VENDA_AMARGO_FRACAO:.0%})',
                              'tokens': carteira.tokens, 'saldo_eth': carteira.eth, 'tx': carteira.ultima_tx, 'token_ordem': pool['nome']})
                    print(f'[mercado] VENDA {MODO} {VENDA_AMARGO_FRACAO:.0%} -> {recebido:.5f} ETH (amargo) {carteira.ultima_tx}', flush=True)
            for chave, rotulo in (('venda_fuga', 'escape'), ('venda_re', 'backing up')):
                if chave in acima_desde and agora - acima_desde[chave] >= REGRAS[chave]['segundos'] and carteira.tokens > 0:
                    recebido = carteira.vender_tudo(preco)
                    ultima_ordem = agora
                    acima_desde.pop(chave, None)
                    if recebido > 0:
                        publicar({'classe': 'ordem', 'lado': 'sell', 'eth': round(recebido, 6), 'preco_eth': preco, 'modo': MODO,
                                  'motivo': f'{rotulo} {dn.get(REGRAS[chave]["grupo"], 0):.0f} Hz after {ultimo_estimulo[0]}',
                                  'tokens': carteira.tokens, 'saldo_eth': carteira.eth, 'tx': carteira.ultima_tx, 'token_ordem': pool['nome']})
                        print(f'[mercado] VENDA {MODO} -> {recebido:.5f} ETH ({rotulo}) {carteira.ultima_tx}', flush=True)
                    break

        # ----- resumo para a pagina -----
        if agora - ultimo_resumo >= 10:
            ultimo_resumo = agora
            valor = carteira.valor(preco)
            publicar({'classe': 'resumo', 'modo': MODO, 'token': pool['nome'], 'par': pool['par'], 'pool': pool['pool'],
                      'preco_eth': preco, 'preco_usd': info.get('preco_usd', 0), 'vol_h24': info.get('vol_h24', 0),
                      'saldo_eth': round(carteira.eth, 6), 'tokens': round(carteira.tokens, 2),
                      'valor_eth': round(valor, 6), 'pnl_eth': round(valor - carteira.eth0, 6),
                      'pnl_pct': round((valor / carteira.eth0 - 1) * 100, 2) if carteira.eth0 else 0,
                      'ordens': carteira.ordens, 'cerebro': estado.get('ligado', False),
                      'max_ordem_eth': MAX_ORDEM_ETH, 'lote_pct': round(ORDEM_FRACAO * 100),
                      'eth_usd': round(eth_usd, 2), 'saldo_usd': round(carteira.eth * eth_usd, 2),
                      'valor_usd': round(valor * eth_usd, 2), 'max_ordem_usd': MAX_ORDEM_USD,
                      'pnl_usd': round((valor - carteira.eth0) * eth_usd, 2),
                      'endereco': carteira.endereco, 'reserva_gas_eth': RESERVA_GAS_ETH if MODO == 'real' else 0,
                      'ordens_ativas': bool(ativas),
                      'sentidos': sent.nome if sent is not None else pool['nome'],
                      'sentidos_fonte': 'chain' if sent is not None else 'gecko',
                      'sentidos_erro': (sent.erro if sent is not None else ''),
                      'quieto_s': round(agora - ultimo_trade_real), 'replay': REPLAY and agora - ultimo_trade_real > 90})
        time.sleep(1.0)


if __name__ == '__main__':
    main()
