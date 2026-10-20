# Koningsdag flight watch

Bot que vigia preços de voos **Porto → Países Baixos** à volta do **Koningsdag 2027
(terça, 27 de abril)** e avisa no **Discord** quando aparece algo barato.
Não usa IA nem chaves pagas: é um script Python que corre em cron e fala com um webhook do Discord.

## Como pesquisa

- **Fonte:** Google Flights (via `fast-flights`, scraping, sem chave). O Google Flights inclui
  Ryanair, Vueling, TAP, KLM, Air Europa, Eurowings, etc. O link de compra é sempre o do Google.
- **Só ida, nos dois sentidos:** cada execução pesquisa Porto → cada aeroporto em cada dia de ida
  e cada aeroporto → Porto em cada dia de volta. Assim a viagem pode misturar companhias e
  aeroportos (ex.: ida pela Ryanair para Weeze, volta de Amesterdão pela Vueling).
- **Aeroportos vizinhos + comboio:** além de AMS e EIN, procura em Weeze, Düsseldorf, Colónia,
  Bruxelas e Charleroi, e soma uma estimativa do comboio/autocarro até Amesterdão (`ground_cost`
  em `config.yaml`).
- **Alerta:** para cada par de datas fica a combinação mais barata. Se o total por pessoa
  (2 voos + comboios) ficar abaixo de `max_price_per_person`, manda tudo numa mensagem.

> Estado: a correr no GitHub Actions desde 19 out 2026.

## Pôr a funcionar em casa

```bash
# 1. Criar o repositório PRIVADO na tua conta pessoal (não na org FitnessUP)
cd koningsdag-flight-watch
git init -b main && git add . && git commit -m "Versão inicial"
gh repo create koningsdag-flight-watch --private --source . --push
#   (confirma com `gh auth status` que estás na tua conta pessoal e não na da empresa)

# 2. Ambiente local
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Webhook do Discord
cp .env.example .env        # e cola lá o URL do webhook
python src/flightwatch.py --test-discord

# 4. Primeira corrida a sério, sem enviar nada
python src/flightwatch.py --dry-run
```

Se o passo 4 mostrar preços, está tudo bem. Se der erro/sem resultados, vai a "Problemas prováveis" em baixo.

## Correr sozinho (GitHub Actions)

1. No repo: *Settings → Secrets and variables → Actions → New repository secret* →
   `DISCORD_WEBHOOK_URL`.
2. O workflow (`.github/workflows/check.yml`) corre 3x por dia + um resumo diário, e faz commit de
   `data/state.json` e `data/legs.csv` (o histórico de preços fica no próprio repo).
3. Para testar já: *Actions → flight-watch → Run workflow*.

**Aviso:** o Google pode bloquear os IPs do GitHub (são de datacenter). Se as corridas falharem sempre,
corre no teu PC/Raspberry com cron (`0 */6 * * * cd /caminho && .venv/bin/python src/flightwatch.py`)
ou muda o fornecedor (ver abaixo).

## Configuração

Tudo em `config.yaml`: aeroportos e custo do comboio até Amesterdão, janelas de datas, nº de
noites, escalas, bagagem, limite de preço por pessoa e anti-spam dos alertas. Para acrescentar um
aeroporto basta uma linha em `airports` (cada aeroporto a mais custa 2 pesquisas por dia de viagem).
O histórico de cada voo fica em `data/legs.csv`.

## Estrutura

```
src/flightwatch.py      script principal (pesquisa, alertas, Discord, estado)
config.yaml             parâmetros da viagem e dos alertas
tests/                  testes da lógica (pytest), sem rede
data/state.json         último total visto/alertado por par de datas (gerado)
data/legs.csv           histórico de preços de cada voo só de ida (gerado)
data/history.csv        histórico antigo, de quando só pesquisava ida+volta (já não é escrito)
docs/CONTEXT.md         contexto, decisões, dúvidas por confirmar, ideias, checklist da viagem
.github/workflows/check.yml  agendamento do GitHub Actions
```

## Problemas prováveis

- **`RequestError` / 403 / página de consentimento**: o Google a bloquear o IP. Tenta correr de casa.
- **Sem resultados em tudo**: confirma a rota no site do Google Flights com o link que o script gera.
- **Preço diferente do site**: confere bagagem (`carry_on_bags`, `checked_bags`) e escalas em `config.yaml`.
- **A biblioteca `fast-flights` partiu**: é scraping, pode acontecer. Plano B em `docs/CONTEXT.md` (SerpApi).

## Testes

```bash
pytest -q
```
