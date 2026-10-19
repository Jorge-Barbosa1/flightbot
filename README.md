# Koningsdag flight watch

Bot que vigia preços de voos **Porto → Países Baixos** à volta do **Koningsdag 2027
(terça, 27 de abril)** e avisa no **Discord** quando aparece algo barato.
Não usa IA nem chaves pagas: é um script Python que corre em cron e fala com um webhook do Discord.

> Estado: **código escrito e a lógica testada com dados simulados, mas ainda nunca corrido contra o
> Google Flights a sério** (onde foi criado não havia acesso). O primeiro passo em casa é fazer um
> `--dry-run` e ver se devolve preços. Ver `docs/CONTEXT.md` para o que falta confirmar.

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
2. **Passo manual:** o ficheiro `github-workflow-check.yml` (na raiz) tem de ser movido para
   `.github/workflows/check.yml` (criar as pastas). O Claude não tem permissão para escrever nessa pasta.
   O workflow corre 3x por dia + um resumo diário, e faz commit de
   `data/state.json` e `data/history.csv` (o histórico de preços fica no próprio repo).
3. Para testar já: *Actions → flight-watch → Run workflow*.

**Aviso:** o Google pode bloquear os IPs do GitHub (são de datacenter). Se as corridas falharem sempre,
corre no teu PC/Raspberry com cron (`0 */6 * * * cd /caminho && .venv/bin/python src/flightwatch.py`)
ou muda o fornecedor (ver abaixo).

## Configuração

Tudo em `config.yaml`: aeroportos de destino, janelas de datas, nº de noites, escalas, bagagem,
limite de preço por pessoa e anti-spam dos alertas. **Ajusta `max_price_per_person` depois de
uma semana ou duas de histórico**: abre `data/history.csv` e vê o que é "normal" para cada rota.

## Estrutura

```
src/flightwatch.py      script principal (pesquisa, alertas, Discord, estado)
config.yaml             parâmetros da viagem e dos alertas
tests/                  testes da lógica (pytest), sem rede
data/state.json         último preço visto/alertado por combinação (gerado)
data/history.csv        histórico de todos os preços (gerado)
docs/CONTEXT.md         contexto, decisões, dúvidas por confirmar, ideias, checklist da viagem
github-workflow-check.yml  agendamento do GitHub Actions (mover para .github/workflows/check.yml)
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
