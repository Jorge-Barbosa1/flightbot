# Contexto e notas

Notas escritas a 19 out 2026 a partir da conversa inicial, para retomar o trabalho sem perder o fio.

## Objetivo

Jorge e a namorada querem ir aos Países Baixos no **Koningsdag** (o "dia nacional" de que se fala é
o Dia do Rei, **27 de abril**; em 2027 cai a uma **terça-feira**). Querem um bot que mande para o
Discord preços muito bons de voos a partir do **Porto (OPO)**. Não há chave de API do Claude (nem é
preciso): o bot é só um script com um limite de preço.

## Decisões tomadas

- **Sem IA**: um script + webhook do Discord chega. Mais simples e grátis.
- **Fornecedor de preços**: `fast-flights` (Python, faz scraping do Google Flights, sem chave).
  Está isolado na função `fetch_cheapest()`, trocar é fácil.
- **Preço por pessoa**: a pesquisa é feita para 1 adulto e o total para 2 é só multiplicado nas
  mensagens. Simplifica e evita dúvidas sobre se o Google mostra preço por pessoa ou total.
- **Vários aeroportos**: AMS, EIN (Eindhoven) e RTM (Roterdão). Costumam ter low-cost mais baratas
  e há comboio para Amesterdão.
- **Janela de datas**: ida 24 a 26 abril, volta 28 a 30 abril, 2 a 6 noites. A noite de 26 para 27
  (Koningsnacht) também tem festa.
- **Anti-spam**: só alerta se estiver abaixo do limite e for novo ou tiver baixado >= 5 EUR.
- **Estado em ficheiros** no próprio repo (`data/`), sem base de dados.

## Por confirmar (não consegui verificar, pesquisa web indisponível na altura)

- [ ] **O `fast-flights` funciona mesmo?** O código foi escrito contra a API v3.1.0 e a lógica está
      testada com dados simulados, mas **nunca fez uma pesquisa real**. Primeiro teste: `--dry-run`.
- [ ] **Quem voa OPO → AMS / EIN / RTM** e com que frequência (e em que dias). Ver no Google Flights.
- [ ] **O Google bloqueia os IPs do GitHub Actions?** Se sim, correr em casa.
- [ ] **Amadeus Self-Service**: lembro-me de ter sido anunciado o fim do portal; confirmar se ainda
      aceita registos antes de o considerar.
- [ ] **Cidade do Koningsdag 2027**: o rei visita uma cidade diferente cada ano. A festa é no país
      todo, mas convém saber para onde.
- [ ] **Preço do Google com 2 adultos** vs 1 adulto x 2: se houver diferença, ajustar.

## Plano B para os preços (se o scraping falhar)

1. **SerpApi** (engine `google_flights`): plano grátis com ~100 pesquisas/mês. Cada pesquisa é
   ida+volta, por isso 40 combinações x 3 corridas/dia gasta isso em horas. Teria de baixar
   `max_queries_per_run` ou pesquisar menos vezes.
2. **Travelpayouts / Aviasales API**: grátis, preços em cache (menos exatos).
3. **Alertas nativos do Google Flights** (email, sem código): seguir Porto → Amesterdão com datas
   flexíveis. Útil como segunda rede de segurança em paralelo ao bot.

## Ideias para depois

- [ ] Gráfico do histórico (`data/history.csv`) por rota.
- [ ] Alerta quando o preço **sobe** muito (sinal para comprar já).
- [ ] Adicionar **alojamento** ao radar (é provavelmente o mais caro e o que esgota primeiro).
- [ ] Modo "datas flexíveis": procurar o dia de ida mais barato dentro da janela.
- [ ] Mensagens com embeds do Discord em vez de texto simples.

## Checklist da viagem (para além dos voos)

- [ ] **Alojamento**: Amesterdão dispara e esgota para o Koningsdag. Ver cedo, incluindo cidades
      à volta (Haarlem, Utrecht, Roterdão, Haia) com comboio.
- [ ] Documentos: cartão de cidadão/passaporte válidos (Países Baixos estão no espaço Schengen).
- [ ] Bagagem: nas low-cost o preço base quase só inclui um item pequeno. Comparar preços com a mesma
      bagagem (config `carry_on_bags` / `checked_bags`).
- [ ] Transporte local: comboio (NS) a partir dos aeroportos; no dia do Koningsdag há muita gente e
      os transportes enchem.
- [ ] Orçamento: voos, alojamento, comida, transportes. Em Amesterdão no Koningsdag tudo está mais caro.
- [ ] Seguro de viagem e cartão europeu de seguro de doença.
- [ ] Vestir de laranja.

## Segurança

- O repo é **privado**. O URL do webhook do Discord fica **só** em `.env` (local) e nos *Secrets*
  do GitHub. Nunca em código, nem em `config.yaml`. Quem tiver o URL pode escrever no teu canal.
- Este projeto é pessoal: criar na conta pessoal do Jorge, **não** na organização FitnessUP.
