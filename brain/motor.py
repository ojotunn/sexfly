# Motor do cerebro: envolve o modelo LIF do conectoma (vendor/modelo.py) e produz, a cada quadro,
# o que a tela precisa: quais neuronios dispararam, contagem por regiao e a taxa dos grupos motores.
# Roda numa thread propria e entrega os quadros numa fila; o servidor so consome.
import json
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from vendor.modelo import TorchModel, MODEL_PARAMS, DT, get_weights, get_hash_tables
from vendor.neuronios import DN_NEURONS, DN_GROUPS, STIMULI
import estimulos_extra

# Plasticidade hebbiana, constantes do fly-brain: sinapses co-ativas se fortalecem. O decaimento do fly-brain
# puxa todas para ZERO (1e-7 por atualizacao = x0,88 em 21 min de cerebro, x0,34 em 3 h): numa vida de dias ela
# ficaria muda. Aqui o decaimento puxa de volta para o valor ORIGINAL do conectoma (homeostase): o que ela
# aprendeu esquece devagar, mas a fiacao de base nunca some. Medido em 07/09.
HEBB_BATCH = 10
HEBB_ETA = 1e-4
HEBB_ALPHA = 1e-7

MS_MIN, MS_MAX = 50.0, 5000.0          # duracao permitida de um estimulo (em ms de cerebro)
JANELA_QUADROS = 8                     # quadros usados p/ a taxa dos grupos motores (~50 ms de cerebro)

# Crise: a rede nao tem adaptacao nem depressao sinaptica, entao um empurrao difuso a prende num estado
# de ~8 mil neuronios a ~58 Hz que se sustenta sozinho (medido: 467 mil spikes/s, sem entrada). Nesse
# estado os reflexos morrem. Quando isso acontece sem estimulo ativo, o motor apaga (reinicia a membrana,
# mantem as sinapses) e registra o evento. Estimulos normais ficam entre 8 e 21 mil spikes/s.
CRISE_SPIKES_S = 100_000.0
CRISE_MS = 200.0                        # ms de cerebro acima do limiar, sem estimulo, antes de apagar
ESTADO_QUIETO_SPIKES_S = 300.0
# Estimulos que, medidos, prendem a rede no estado de crise mesmo por 300 ms. Ficam fora ate haver
# uma versao mais fraca testada. Os demais (sugar, bitter, lc4, jo, p9) sao seguros ate 2 s.
ESTIMULOS_BLOQUEADOS = {'or56a'} | estimulos_extra.BLOQUEADOS


class Cerebro:
    def __init__(self, dir_dados, dir_estado, meta_parquet, device='cuda',
                 plasticidade=True, ambiente_hz=0.0, intervalo_quadro=0.1,
                 salvar_a_cada_s=600):
        # ambiente_hz: ruido difuso nos neuronios sensoriais. Padrao 0: qualquer valor prende a rede
        # no estado de crise (ver CRISE_SPIKES_S). Fica como opcao so para experimento.
        self.device = device if torch.cuda.is_available() else 'cpu'
        self.dir_estado = Path(dir_estado)
        self.dir_estado.mkdir(parents=True, exist_ok=True)
        self.intervalo_quadro = intervalo_quadro
        self.salvar_a_cada_s = salvar_a_cada_s
        self.plasticidade = plasticidade
        self.ambiente_hz = float(ambiente_hz)

        dir_dados = Path(dir_dados)
        comp = dir_dados / '2025_Completeness_783.csv'
        conn = dir_dados / '2025_Connectivity_783.parquet'
        self.flyid2i, self.i2flyid = get_hash_tables(str(comp))
        self.n = len(self.flyid2i)
        pesos = get_weights(str(conn), str(comp), str(dir_dados), csr=True).to(self.device)
        self.n_sinapses = int(pesos.values().numel())
        self.model = TorchModel(1, self.n, DT, MODEL_PARAMS, pesos, device=self.device)
        self.state = self.model.state_init()
        self.rates = torch.zeros(1, self.n, device=self.device)

        # neuronios de entrada (estimulos) e de saida (descendentes), por indice do tensor.
        # Os do fly-brain vem por id (vendor/neuronios.py); os extras vem por tipo celular (estimulos_extra).
        extras_in, extras_out = estimulos_extra.montar(meta_parquet)
        todos = dict(STIMULI)
        todos.update(extras_in)
        self.stim_idx = {
            k: torch.tensor([self.flyid2i[i] for i in v['neurons'] if i in self.flyid2i],
                            device=self.device, dtype=torch.long)
            for k, v in todos.items()
        }
        self.stim_rate = {k: float(v['rate']) for k, v in todos.items()}
        self.stim_desc = {k: v.get('description', k) for k, v in todos.items()}
        self.dn_names = [nm for nm in DN_NEURONS if DN_NEURONS[nm] in self.flyid2i]
        dn_ids = [DN_NEURONS[nm] for nm in self.dn_names]
        self.grupos = {g: [self.dn_names.index(nm) for nm in nomes if nm in self.dn_names]
                       for g, nomes in DN_GROUPS.items()}
        for g, ids in extras_out.items():          # grupos extras (parada, recompensa) entram na mesma leitura
            ini = len(dn_ids)
            ids = [i for i in ids if i in self.flyid2i]
            self.dn_names += [f'{g}_{k}' for k in range(len(ids))]
            dn_ids += ids
            self.grupos[g] = list(range(ini, ini + len(ids)))
        self.dn_idx = torch.tensor([self.flyid2i[i] for i in dn_ids], device=self.device, dtype=torch.long)

        # regiao de cada neuronio (contagem por regiao) e neuronios sensoriais (ruido ambiente)
        meta = pd.read_parquet(meta_parquet)
        assert len(meta) == self.n, 'neuronios.parquet nao bate com o modelo; rode preparar_dados.py'
        self.regiao = torch.tensor(meta['regiao'].to_numpy(np.int64), device=self.device)
        self.n_regioes = int(self.regiao.max().item()) + 1
        sens = np.flatnonzero(meta['super_class'].to_numpy() == 'sensory')
        self.sens_idx = torch.tensor(sens, device=self.device, dtype=torch.long)

        # acumuladores do quadro
        self.acc = torch.zeros(self.n, device=self.device)
        self.passos = 0
        self.passos_quadro = 0
        self.janela = deque(maxlen=JANELA_QUADROS)
        self.ativos = {}                 # estimulo -> passo em que expira
        self._mudou_estimulo = True
        self._proximo_fim = None         # menor passo de expiracao entre os ativos
        self.lock = threading.Lock()     # protege ativos
        self.fila = queue.Queue(maxsize=3)
        self.rodando = False
        self.info = {'sinapses_mudadas': 0, 'passos_por_s': 0.0}
        self.eventos = deque(maxlen=200)  # ultimos estimulos e crises (p/ historico)
        self.crises = 0
        self._crise_passos = 0
        self.estado = 'quiet'

        self._init_plasticidade()
        self._carregar_vida()
        print(f'[cerebro] {self.n} neuronios, {self.n_sinapses} sinapses em {self.device}; '
              f'saidas {len(self.dn_names)} em {len(self.grupos)} grupos; estimulos '
              f'{[k for k in self.stim_idx if k not in ESTIMULOS_BLOQUEADOS]}; bloqueados {sorted(ESTIMULOS_BLOQUEADOS)}')

    # ----- plasticidade (copiado do fly-brain, com estado proprio) -----
    def _init_plasticidade(self):
        w = self.model.weights
        self._col_idx = w.col_indices()
        row_ptr = w.crow_indices()
        self._syn = w.values()                     # vista mutavel dos pesos
        self._w0 = self._syn.abs().clone()         # magnitudes originais (antes de carregar estado)
        self._sinal = torch.sign(self._syn)
        self._base = self._syn.clone()             # pesos originais com sinal: alvo do decaimento
        row_len = row_ptr[1:] - row_ptr[:-1]
        self._post_idx = torch.repeat_interleave(torch.arange(self.n, device=self.device), row_len)
        self._spike_acc = torch.zeros(self.n, device=self.device)
        self._hebb_count = 0
        maxm = 3.0 * self._w0
        self._cmin = torch.where(self._sinal < 0, -maxm, torch.zeros_like(maxm))
        self._cmax = torch.where(self._sinal > 0, maxm, torch.zeros_like(maxm))
        arq = self.dir_estado / 'sinapses.pt'
        if arq.exists():
            salvo = torch.load(arq, map_location=self.device, weights_only=True)
            if salvo.shape == self._syn.shape:
                self._syn.copy_(salvo)
                print('[cerebro] sinapses carregadas de', arq)

    @torch.no_grad()
    def _hebb(self):
        avg = self._spike_acc / HEBB_BATCH
        self._spike_acc.zero_()
        dW = HEBB_ETA * avg[self._col_idx] * avg[self._post_idx] * self._sinal - HEBB_ALPHA * (self._syn - self._base)
        self._syn.add_(dW)
        self._syn.clamp_(min=self._cmin, max=self._cmax)

    @torch.no_grad()
    def _contar_mudadas(self):
        return int(((self._syn.abs() - self._w0).abs() > 0.01 * self._w0).sum().item())

    # ----- vida (continuidade entre reinicios) -----
    def _carregar_vida(self):
        arq = self.dir_estado / 'vida.json'
        if arq.exists():
            v = json.loads(arq.read_text())
            self.passos_total = int(v.get('passos_total', 0))
            self.nascimento = v.get('nascimento')
        else:
            self.passos_total = 0
            self.nascimento = datetime.now(timezone.utc).isoformat()

    def salvar(self):
        torch.save(self._syn.detach().cpu(), self.dir_estado / 'sinapses.pt')
        (self.dir_estado / 'vida.json').write_text(json.dumps({
            'nascimento': self.nascimento,
            'passos_total': self.passos_total,
            'segundos_vividos': self.passos_total * DT / 1000.0,
            'salvo_em': datetime.now(timezone.utc).isoformat(),
        }, indent=1))

    # ----- estimulos -----
    def estimular(self, nome, ms, origem='site'):
        if nome not in self.stim_idx or nome in ESTIMULOS_BLOQUEADOS:
            return False
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            ms = 500.0
        ms = max(MS_MIN, min(MS_MAX, ms))
        with self.lock:
            self.ativos[nome] = self.passos + int(ms / DT)
            self._proximo_fim = min(self.ativos.values())
            self._mudou_estimulo = True
        self.eventos.append({'t': time.time(), 'tipo': 'stimulus', 'estimulo': nome, 'ms': ms, 'origem': origem})
        return True

    def _recalc_rates(self):
        self.rates.zero_()
        if self.ambiente_hz > 0:
            self.rates[0, self.sens_idx] = self.ambiente_hz
        for nome in self.ativos:
            idx = self.stim_idx[nome]
            self.rates[0, idx] = torch.maximum(
                self.rates[0, idx], torch.full((len(idx),), self.stim_rate[nome], device=self.device))

    # ----- um passo de 0,1 ms de cerebro -----
    @torch.no_grad()
    def passo(self):
        if self._mudou_estimulo or (self._proximo_fim is not None and self.passos >= self._proximo_fim):
            with self.lock:
                vencidos = [k for k, fim in self.ativos.items() if self.passos >= fim]
                for k in vencidos:
                    del self.ativos[k]
                self._proximo_fim = min(self.ativos.values()) if self.ativos else None
                self._recalc_rates()
                self._mudou_estimulo = False
        c, d, s, v, r = self.state
        self.state = self.model(self.rates, c, d, s, v, r)
        spk = self.state[2][0]
        self.acc += spk
        self.passos += 1
        self.passos_quadro += 1
        self.passos_total += 1
        if self.plasticidade:
            self._spike_acc += spk
            self._hebb_count += 1
            if self._hebb_count >= HEBB_BATCH:
                self._hebb()
                self._hebb_count = 0

    # ----- fecha um quadro: o que disparou desde o ultimo -----
    @torch.no_grad()
    def _quadro(self):
        passos = self.passos_quadro
        self.passos_quadro = 0
        if passos == 0:
            return None
        dn = self.acc[self.dn_idx].cpu().numpy()
        reg = torch.bincount(self.regiao, weights=self.acc, minlength=self.n_regioes).cpu().numpy()
        idx = torch.nonzero(self.acc).flatten().cpu().numpy().astype(np.uint32)
        total = float(self.acc.sum().item())
        self.acc.zero_()
        with self.lock:
            ativos = list(self.ativos)
        # detector de crise (ver constantes no topo)
        seg_q = passos * DT / 1000.0
        spikes_s = total / seg_q if seg_q > 0 else 0.0
        if spikes_s > CRISE_SPIKES_S:
            self._crise_passos += passos
            self.estado = 'seizure'
            if not ativos and self._crise_passos * DT >= CRISE_MS:
                self.apagar(spikes_s)
        else:
            self._crise_passos = 0
            self.estado = 'quiet' if spikes_s < ESTADO_QUIETO_SPIKES_S else 'active'
        self.janela.append((dn, passos))
        soma = sum(d for d, _ in self.janela)
        p = sum(p for _, p in self.janela)
        seg = p * DT / 1000.0
        taxas = {g: (float(soma[ii].sum() / (len(ii) * seg)) if ii and seg > 0 else 0.0)
                 for g, ii in self.grupos.items()}
        return {
            'idx': idx, 'dn': taxas, 'regioes': reg.astype(int).tolist(), 'total': total,
            'passos': passos, 'seg_cerebro': passos * DT / 1000.0, 'ativos': ativos,
            't_cerebro': self.passos * DT / 1000.0,
            'vivo_s': self.passos_total * DT / 1000.0,
            'estado': self.estado, 'crises': self.crises,
        }

    @torch.no_grad()
    def apagar(self, spikes_s=0.0):
        """Apagao: reinicia a membrana e as sinapses continuam como estao. Registra o evento."""
        self.state = self.model.state_init()
        self.acc.zero_()
        self._spike_acc.zero_()
        self.crises += 1
        self._crise_passos = 0
        self.estado = 'quiet'
        self.eventos.append({'t': time.time(), 'tipo': 'seizure', 'spikes_s': round(spikes_s),
                             't_cerebro': self.passos * DT / 1000.0})
        print(f'[cerebro] crise #{self.crises}: {spikes_s:,.0f} spikes/s sem estimulo; apaguei e religuei')

    # ----- thread -----
    def iniciar(self):
        self.rodando = True
        self.thread = threading.Thread(target=self._loop, name='cerebro', daemon=True)
        self.thread.start()

    def parar(self):
        self.rodando = False
        if hasattr(self, 'thread'):
            self.thread.join(timeout=10)
        self.salvar()

    def _ler_teto(self):
        """Teto de passos por segundo (poupa a placa): variavel FLY_PASSOS_S ou o arquivo brain/data/passos_s.txt
        (relido a cada 2 s, muda sem reiniciar). 0 = sem teto (roda o mais rapido que a placa aguenta)."""
        teto = float(os.environ.get('FLY_PASSOS_S', '0') or 0)
        try:
            arq = Path(__file__).resolve().parent / 'data' / 'passos_s.txt'
            if arq.exists():
                teto = float(arq.read_text().strip() or 0)
        except Exception:
            pass
        return max(0.0, teto)

    def _loop(self):
        t_quadro = time.time()
        t_salvo = t_quadro
        t_mudadas = t_quadro
        t_taxa = t_quadro
        passos_taxa = 0
        teto = self._ler_teto(); lote = 8; t_lote = time.time(); n_lote = 0   # dorme a cada 8 passos para segurar a taxa
        while self.rodando:
            self.passo()
            passos_taxa += 1
            if teto > 0:
                n_lote += 1
                if n_lote >= lote:
                    t_lote += lote / teto
                    atraso = t_lote - time.time()
                    if atraso > 0:
                        time.sleep(atraso)
                    elif atraso < -1.0:
                        t_lote = time.time()
                    n_lote = 0
            agora = time.time()
            if agora - t_quadro >= self.intervalo_quadro:
                t_quadro = agora
                q = self._quadro()
                if q is not None:
                    q['passos_por_s'] = self.info['passos_por_s']
                    q['sinapses_mudadas'] = self.info['sinapses_mudadas']
                    if self.fila.full():
                        try:
                            self.fila.get_nowait()
                        except queue.Empty:
                            pass
                    self.fila.put_nowait(q)
            if agora - t_taxa >= 2.0:
                self.info['passos_por_s'] = passos_taxa / (agora - t_taxa)
                passos_taxa = 0
                t_taxa = agora
                novo = self._ler_teto()
                if novo != teto:
                    teto = novo; t_lote = time.time(); n_lote = 0
            if self.plasticidade and agora - t_mudadas >= 30.0:
                self.info['sinapses_mudadas'] = self._contar_mudadas()
                t_mudadas = agora
            if agora - t_salvo >= self.salvar_a_cada_s:
                self.salvar()
                t_salvo = agora
