// Recebe o comando /flight do Discord e pede ao GitHub Actions para fazer a
// pesquisa. A resposta com os voos é enviada pelo próprio workflow (follow-up
// da interação), por isso aqui só se valida, se confirma e se dispara.
//
// Variáveis (wrangler.toml / `wrangler secret put`):
//   DISCORD_PUBLIC_KEY  chave pública da app do Discord (General Information)
//   GITHUB_TOKEN        token fine-grained com "Actions: read and write" no repo
//   GITHUB_REPO         ex. Jorge-Barbosa1/koningsdag-flight-watch
//   GITHUB_REF          branch, ex. master
//   ALLOWED_USERS       (opcional) IDs do Discord autorizados, separados por vírgulas

const WORKFLOW = "check.yml";

export default {
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
    if (interaction.type !== 2 || interaction.data?.name !== "flight") {
      return reply("Comando desconhecido.");
    }

    const user = interaction.member?.user ?? interaction.user ?? {};
    const allowed = (env.ALLOWED_USERS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (allowed.length && !allowed.includes(user.id)) {
      return reply("Não tens autorização para usar este comando.");
    }

    const options = Object.fromEntries(
      (interaction.data.options || []).map((o) => [o.name, o.value]),
    );
    const pedido = {
      destino: options.destino,
      ida: options.ida,
      volta: options.volta,
      top: options.top ?? 3,
      sair_depois: options.sair_depois ?? null,
      chegar_ate: options.chegar_ate ?? null,
      origem: options.origem ?? null,
      app_id: interaction.application_id,
      token: interaction.token,
    };

    ctx.waitUntil(dispatch(env, pedido));
    return json({ type: 5 }); // "a pensar..." até a resposta chegar
  },
};

async function dispatch(env, pedido) {
  const original = `https://discord.com/api/v10/webhooks/${pedido.app_id}/${pedido.token}/messages/@original`;
  let text;
  try {
    const res = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "flightwatch-discord",
        },
        body: JSON.stringify({
          ref: env.GITHUB_REF || "master",
          inputs: { pedido: JSON.stringify(pedido) },
        }),
      },
    );
    text = res.ok
      ? `🔎 A procurar voos para **${pedido.destino}** (ida ${pedido.ida}, volta ${pedido.volta})… ` +
        "a resposta chega aqui em 1-3 minutos."
      : `❌ O GitHub recusou o pedido (HTTP ${res.status}): ${(await res.text()).slice(0, 300)}`;
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
