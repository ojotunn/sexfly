# Comando unico do LANCAMENTO. Com o CA do token dela (e o link do X, se houver):
#   1. o token entra em mercado/sentidos.txt (ela passa a sentir cada trade dele, direto da chain)
#   2. e em mercado/ignorar.txt (ela nunca opera o proprio token; se estiver olhando, larga na hora)
#   3. o CA e o X vao para a pagina ao vivo (POST /api/config no servidor local -> relay -> paginas abertas)
#   4. o historico de cards e zerado (os "logs" de replay/avisos somem)
#   5. as ordens continuam desligadas (SEX FLY nao opera: elas so sentem o token)
# Nada reinicia. Uso:
#   py\Scripts\python.exe mercado\lancar.py 0xCA_DO_TOKEN [https://x.com/...]
#   py\Scripts\python.exe mercado\lancar.py --desligar        (so desliga as ordens de novo)
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
SERVIDOR = 'http://localhost:8435'


def post(caminho, dados):
    req = urllib.request.Request(SERVIDOR + caminho, data=json.dumps(dados).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    args = [a for a in sys.argv[1:]]
    if '--desligar' in args:
        (AQUI / 'ordens.txt').write_text('off\n')
        print('ordens DESLIGADAS')
        return
    if not args or not args[0].lower().startswith('0x') or len(args[0]) != 42:
        sys.exit('uso: lancar.py 0xCA_DO_TOKEN [link do X]')
    ca = args[0]
    x = args[1] if len(args) > 1 else ''
    (AQUI / 'sentidos.txt').write_text(f'# lancamento {time.strftime("%Y-%m-%d %H:%M")}: o token dela\n{ca}\n')
    (AQUI / 'ignorar.txt').write_text(f'# lancamento {time.strftime("%Y-%m-%d %H:%M")}: ela nunca opera o proprio token\n{ca}\n')
    print('1-2. sentidos.txt e ignorar.txt =', ca)
    try:   # contador de ordens do site comeca do zero neste lancamento: nonce atual da carteira dela vira a base
        from web3 import Web3
        end = '0x' + json.loads((AQUI / 'carteira.json').read_text())['address']
        w3 = Web3(Web3.HTTPProvider('https://rpc.mainnet.chain.robinhood.com', request_kwargs={'timeout': 20}))
        base = int(w3.eth.get_transaction_count(Web3.to_checksum_address(end)))
        aj = (AQUI / 'ajustes.txt').read_text()
        if 'nonce_base=' in aj:
            aj = re.sub(r'^nonce_base=.*$', f'nonce_base={base}', aj, flags=re.M)
        else:
            aj = aj.rstrip('\n') + f'\nnonce_base={base}\n'
        (AQUI / 'ajustes.txt').write_text(aj); print('     nonce_base =', base)
    except Exception as e:
        print('     nonce_base nao ajustado:', e)
    print('3.   pagina:', post('/api/config', {'ca': ca, 'x': x}))
    print('4.   feed limpo (PC):', post('/api/mercado/limpar', {}))
    # o relay guarda a propria lista de cards: limpa la tambem (token de relay.token) e as paginas abertas zeram
    try:
        token = (AQUI.parent / 'relay.token').read_text().strip()
        req = urllib.request.Request(f'https://sexfly-production.up.railway.app/fonte/limpar?token={token}', data=b'{}', method='POST',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            print('     feed limpo (relay):', json.loads(r.read().decode('utf-8')))
    except Exception as e:
        print('     relay nao limpou:', str(e)[:80])
    (AQUI / 'ordens.txt').write_text('off\n')   # SEX FLY: sem carteira, sem ordens; elas so sentem
    print('5.   ordens continuam DESLIGADAS (SEX FLY nao opera)')
    time.sleep(12)
    with urllib.request.urlopen(SERVIDOR + '/api/mercado', timeout=10) as r:
        resumo = (json.loads(r.read().decode('utf-8')).get('resumo') or {})
    print('agora:', {k: resumo.get(k) for k in ('sentidos', 'sentidos_fonte', 'token', 'ordens_ativas')})


if __name__ == '__main__':
    main()
