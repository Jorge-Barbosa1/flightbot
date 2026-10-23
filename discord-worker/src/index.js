// Recebe os comandos do Discord (/flight, /vigiar, /explorar...) e os cliques no
// botão "👀 Vigiar", e pede ao GitHub Actions para fazer a pesquisa. A resposta com os voos é enviada pelo próprio workflow (follow-up
// da interação), por isso aqui só se valida, se confirma e se dispara.
//
// Variáveis (wrangler.toml / `wrangler secret put`):
//   DISCORD_PUBLIC_KEY  chave pública da app do Discord (General Information)
//   GITHUB_TOKEN        token fine-grained com "Actions: read and write" no repo
//   GITHUB_REPO         ex. Jorge-Barbosa1/flightbot
//   GITHUB_REF          branch, ex. master
//   ALLOWED_USERS       (opcional) IDs do Discord autorizados, separados por vírgulas
//   DISCORD_WEBHOOK_URL (opcional) canal para avisar se um agendamento falhar

const WORKFLOW = "check.yml";
const SUMMARY_CRON = "30 8 * * *"; // tem de ser igual ao de wrangler.toml
const COMMANDS = ["flight", "vigiar", "vigilancias", "parar", "explorar", "historico"];

// o que aparece logo no Discord enquanto o GitHub trabalha
function pendingText(p) {
  const when = p.quando ? p.quando : `ida ${p.ida ?? "?"}, volta ${p.volta ?? "?"}`;
  switch (p.acao) {
    case "vigiar":
      return `👀 A criar a vigilância para **${p.destino}** (${when}, até ${p.max} €)… resposta em 1-3 minutos.`;
    case "vigilancias":
      return "📋 A ir buscar a lista de vigilâncias…";
    case "parar":
      return `🛑 A parar a vigilância #${p.id}…`;
    case "explorar":
      return `🌍 A ver para onde é mais barato (${when})… são muitos destinos, a resposta chega em 2-4 minutos.`;
    case "historico":
      return p.id ? `📉 A desenhar o histórico da vigilância #${p.id}…` : "📉 A desenhar o histórico do Koningsdag…";
    default:
      return `🔎 A procurar voos para **${p.destino}** (${when})… a resposta chega aqui em 1-3 minutos.`;
  }
}

// botão "👀 Vigiar": custom_id = v|destino|ida|volta|hora_min|chegar_ate|mala|origem
function parseWatchId(id) {
  const [, destino, ida, volta, sair, chegar, mala, origem] = id.split("|");
  return {
    destino, ida, volta,
    sair_depois: sair ? Number(sair) : null,
    chegar_ate: chegar ? Number(chegar) : null,
    bagagem: mala === "1" ? "cabine" : null,
    origem: origem || null,
  };
}

function findInput(components, id) {
  for (const c of components || []) {
    if (c.custom_id === id && "value" in c) return c.value;
    const inner = findInput(c.components ?? (c.component ? [c.component] : []), id);
    if (inner !== undefined) return inner;
  }
  return undefined;
}

export default {
  scheduled: (event, env, ctx) => ctx.waitUntil(scheduled(event, env)),

  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("flightwatch: ok");
    }
    const body = await request.text();
    const ok = await verifySignature(
      env.DISCORD_PUBLIC_KEY,
      request.headers.get("X-Signature-Ed25519"),
      request.headers.get("X-Signature-Timestamp"),
      body,
    );
    if (!ok) {
      return new Response("assinatura inválida", { status: 401 });
    }

    const interaction = JSON.parse(body);
    if (interaction.type === 1) {
      return json({ type: 1 }); // PING do Discord ao configurar o endpoint
    }

    const user = interaction.member?.user ?? interaction.user ?? {};
    const allowed = (env.ALLOWED_USERS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (allowed.length && !allowed.includes(user.id)) {
      return reply("Não tens autorização para usar este comando.");
    }
    const base = { user: user.id ?? null, app_id: interaction.application_id, token: interaction.token };

    // clique no botão "👀 Vigiar": pede o preço máximo numa janela
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("v|")) {
      const w = parseWatchId(interaction.data.custom_id);
      return json({
        type: 9,
        data: {
          custom_id: `m${interaction.data.custom_id}`,
          title: `Vigiar ${w.destino}`.slice(0, 45),
          components: [{
            type: 1,
            components: [{
              type: 4, custom_id: "max", style: 1, label: "Preço máximo por pessoa (€, ida e volta)",
              placeholder: "ex. 150", min_length: 1, max_length: 5, required: true,
            }],
          }],
        },
      });
    }

    // janela submetida: cria a vigilância
    if (interaction.type === 5 && interaction.data?.custom_id?.startsWith("mv|")) {
      const max = parseInt(String(findInput(interaction.data.components, "max") ?? "").replace(/\D/g, ""), 10);
      if (!max) {
        return reply("Escreve só o número, por exemplo 150.");
      }
      const pedido = { acao: "vigiar", ...parseWatchId(interaction.data.custom_id.slice(1)), max, ...base };
      ctx.waitUntil(dispatch(env, pedido));
      return json({ type: 5 });
    }

    const acao = interaction.data?.name;
    if (interaction.type !== 2 || !COMMANDS.includes(acao)) {
      return reply("Comando desconhecido.");
    }
    const options = Object.fromEntries(
      (interaction.data.options || []).map((o) => [o.name, o.value]),
    );
    const pedido = { acao, ...options, ...base };

    ctx.waitUntil(dispatch(env, pedido));
    return json({ type: 5 }); // "a pensar..." até a resposta chegar
  },
};

// Arranca o workflow do GitHub com estes inputs.
function runWorkflow(env, inputs) {
  return fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "flightwatch-discord",
      },
      body: JSON.stringify({ ref: env.GITHUB_REF || "master", inputs }),
    },
  );
}

// Agendamentos (Cloudflare Cron Triggers, em wrangler.toml). O agendador do
// GitHub chegava a atrasar 3-6 horas; este dispara à hora certa.
export async function scheduled(event, env) {
  const summary = event.cron === SUMMARY_CRON;
  let problem;
  try {
    const res = await runWorkflow(env, { summary: String(summary) });
    if (!res.ok) problem = `HTTP ${res.status} ${(await res.text()).slice(0, 200)}`;
  } catch (e) {
    problem = String(e);
  }
  if (problem) {
    if (env.DISCORD_WEBHOOK_URL) {
      await fetch(env.DISCORD_WEBHOOK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: `⚠️ **Não consegui arrancar a pesquisa agendada** (${event.cron}): ${problem}\n` +
            "Se for 401, o token do GitHub no worker expirou ou foi apagado.",
        }),
      });
    }
    throw new Error(`GitHub recusou o agendamento ${event.cron}: ${problem}`);
  }
}

async function dispatch(env, pedido) {
  const original = `https://discord.com/api/v10/webhooks/${pedido.app_id}/${pedido.token}/messages/@original`;
  let text;
  try {
    const res = await runWorkflow(env, { pedido: JSON.stringify(pedido) });
    text = res.ok
      ? pendingText(pedido)
      :`❌ O GitHub recusou o pedido (HTTP ${res.status}): ${(await res.text()).slice(0, 300)}`;
  } catch (e) {
    text = `❌ Não consegui falar com o GitHub: ${e}`;
  }
  await fetch(original, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: text }),
  });
}

async function verifySignature(publicKey, signature, timestamp, body) {
  if (!publicKey || !signature || !timestamp) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw", hexToBytes(publicKey), { name: "Ed25519" }, false, ["verify"],
    );
    return await crypto.subtle.verify(
      "Ed25519", key, hexToBytes(signature), new TextEncoder().encode(timestamp + body),
    );
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

function json(data) {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

function reply(content) {
  return json({ type: 4, data: { content, flags: 64 } }); // 64 = só quem pediu vê
}
