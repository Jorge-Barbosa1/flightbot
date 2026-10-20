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
- **Ida e volta na mesma companhia:** nos aeroportos de `roundtrip_airports` (por defeito AMS)
  pesquisa também o bilhete de ida e volta, que nas companhias tradicionais às vezes sai mais
  barato que duas idas soltas. Fica sempre a opção mais barata (em empate, a de ida e volta).
- **Aeroportos vizinhos + comboio:** além de AMS e EIN, procura em Weeze, Düsseldorf, Colónia,
  Bruxelas e Charleroi, e soma o comboio/autocarro até Amesterdão (`ground_cost` em `config.yaml`,
  valores pesquisados a 20 out 2026). Paris e Roterdão foram testados e postos de parte.
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

## Pedidos à medida

Para veres os melhores resultados para outras datas sem mexer nos alertas: no GitHub (ou na app
do GitHub no telemóvel) vai a *Actions → flight-watch → Run workflow* e preenche:

- **ida**: `23/04` ou um intervalo `23/04 a 24/04`
- **volta**: `01/05` ou `30/04 a 01/05`
- **top**: quantas viagens mostrar (por defeito 3)
- **hora**: só idas a partir desta hora, ex. `17` (vazio = qualquer hora)

A resposta chega ao Discord em 2-4 minutos. Pelo terminal:
`gh workflow run flight-watch -f ida=23/04 -f volta=01/05 -f top=3 -f hora=17`
(ou localmente: `python src/flightwatch.py --ida 23/04 --volta 01/05 --top 3 --hora 17`).

## Comando /flight no Discord

Escreves no Discord, por exemplo:

```
/flight destino:Madrid ida:tarde 23/10 volta:cedo 25/10
/flight destino:Londres ida:23/10 volta:25/10 sair_depois:18 chegar_ate:14 top:5
```

- **destino**: cidade (Madrid, Londres, Paris, Amesterdão…) ou código(s) de aeroporto (`MAD`, `MAD,BCN`)
- **ida / volta**: `23/10`, `23/10/2026` ou um intervalo `23/10 a 24/10`. Sem ano, usa a próxima vez
  que esse dia acontece. `tarde` na ida = sair a partir das 17h; `cedo` na volta = chegar até às 15h.
- **sair_depois / chegar_ate**: horas exatas, se quiseres outras que não 17h/15h
- **top** (por defeito 3) e **origem** (por defeito OPO)

O bot responde logo "a procurar…" e 1-3 minutos depois aparecem as opções por baixo.

**Como funciona:** Discord → Cloudflare Worker (`discord-worker/`, grátis, sempre ligado) →
GitHub Actions (`workflow_dispatch` com o pedido) → o script responde à interação do Discord.

### Configurar (uma vez)

1. **App do Discord**: <https://discord.com/developers/applications> → *New Application*.
   Guarda o *Application ID* e a *Public Key* (General Information). Em *Bot* → *Reset Token*,
   guarda o token (só serve para registar o comando).
2. **Token do GitHub**: <https://github.com/settings/personal-access-tokens/new> → fine-grained,
   só o repo `koningsdag-flight-watch`, permissão *Actions: Read and write*.
3. **Cloudflare** (conta grátis em <https://dash.cloudflare.com/sign-up>):
   ```bash
   cd discord-worker
   npx wrangler@4 login
   npx wrangler@4 deploy                         # dá o URL do worker
   npx wrangler@4 secret put DISCORD_PUBLIC_KEY
   npx wrangler@4 secret put GITHUB_TOKEN
   npx wrangler@4 secret put ALLOWED_USERS       # opcional: o teu ID do Discord
   ```
4. No portal do Discord, *General Information* → **Interactions Endpoint URL** = URL do worker → *Save*.
5. Registar o comando: `DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... python discord-worker/register_commands.py`
6. Adicionar a app ao servidor: `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=applications.commands`

Testes do worker: `node discord-worker/test.mjs`.

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
