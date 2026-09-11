@echo off
cd /d "%~dp0"
rem Piso do corpo: preto, ardosia, bancada, madeira, banana, musgo ou xadrez. Cores: real ou original.
set FLY_CORPO_PISO=preto
set FLY_CORPO_CORES=real
rem Corpo: pose (a mosca e desenhada no navegador; leve p/ internet), jpeg (render local) ou ambos.
set FLY_CORPO_SAIDA=pose
rem Site publico: relay no Railway (relay/servidor.py) em flybrain.finance. O token fica em relay.token (fora do git);
rem o MESMO valor vai na variavel FLY_RELAY_TOKEN do servico no Railway.
rem (o PC entra pelo endereco do Railway, que nao depende do DNS do dominio; flybrain.finance e so para quem assiste)
set FLY_RELAY_URL=wss://flybrain-production.up.railway.app/fonte
if exist relay.token set /p FLY_RELAY_TOKEN=<relay.token
rem Mercado: modo real (carteira dela em mercado\carteira.json) ou papel. Em DOLAR: teto por ordem e lote (fracao do saldo).
set FLY_MERCADO_MODO=real
set FLY_MERCADO_MAX_ORDEM_USD=10
set FLY_MERCADO_ORDEM=0.10
rem so no modo papel: saldo virtual inicial em ETH
set FLY_MERCADO_SALDO_ETH=0.05
echo ==== SEXFLY - ela (8435) + ele (8436) + corpo 3D + mercado ====
echo Abra http://localhost:8435 no navegador. Ctrl+C aqui encerra o cerebro e salva as sinapses.
start "SEXFLY ele (cerebro do macho)" cmd /c "set FLY_QUEM=ele&& set FLY_PORT=8436&& py\Scripts\python.exe brain\servidor.py"
start "SEXFLY corpo 3D" "py\Scripts\python.exe" "corpo\corpo.py"
start "SEXFLY mercado" "py\Scripts\python.exe" "mercado\mercado.py"
"py\Scripts\python.exe" "brain\servidor.py"
pause
