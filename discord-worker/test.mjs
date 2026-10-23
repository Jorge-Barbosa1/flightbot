// Testes do worker sem rede: `node discord-worker/test.mjs`
import assert from "node:assert/strict";
import worker, { scheduled } from "./src/index.js";

const { publicKey, privateKey } = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const pubHex = Buffer.from(await crypto.subtle.exportKey("raw", publicKey)).toString("hex");

async function signed(payload, { badSig = false } = {}) {
  const body = JSON.stringify(payload);
  const ts = "1760950000";
  let sig = Buffer.from(await crypto.subtle.sign("Ed25519", privateKey, new TextEncoder().encode(ts + body)));
  if (badSig) sig[0] ^= 1;
  return new Request("https://worker.test/", {
    method: "POST",
    headers: { "X-Signature-Ed25519": sig.toString("hex"), "X-Signature-Timestamp": ts },
    body,
  });
}

const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url, init });
  return new Response(null, { status: url.includes("api.github.com") ? 204 : 200 });
};
const env = { DISCORD_PUBLIC_KEY: pubHex, GITHUB_TOKEN: "t", GITHUB_REPO: "u/r", GITHUB_REF: "master" };
const waits = [];
const ctx = { waitUntil: (p) => waits.push(p) };

// assinatura errada -> 401 (o Discord testa isto ao configurar o endpoint)
let res = await worker.fetch(await signed({ type: 1 }, { badSig: true }), env, ctx);
assert.equal(res.status, 401);

// PING
res = await worker.fetch(await signed({ type: 1 }), env, ctx);
assert.deepEqual(await res.json(), { type: 1 });

// /flight -> resposta adiada + pedido ao GitHub + mensagem "a procurar"
const cmd = {
  type: 2, application_id: "123", token: "tok",
  member: { user: { id: "42" } },
  data: { name: "flight", options: [
    { name: "destino", value: "Madrid" }, { name: "ida", value: "tarde 23/10" },
    { name: "volta", value: "cedo 25/10" }, { name: "top", value: 2 },
  ] },
};
res = await worker.fetch(await signed(cmd), env, ctx);
assert.deepEqual(await res.json(), { type: 5 });
await Promise.all(waits);
const gh = calls.find((c) => c.url.includes("api.github.com"));
assert.equal(gh.url, "https://api.github.com/repos/u/r/actions/workflows/check.yml/dispatches");
const pedido = JSON.parse(JSON.parse(gh.init.body).inputs.pedido);
assert.deepEqual(pedido, { acao: "flight", destino: "Madrid", ida: "tarde 23/10", volta: "cedo 25/10",
  top: 2, user: "42", app_id: "123", token: "tok" });
const edit = calls.find((c) => c.url.endsWith("/webhooks/123/tok/messages/@original"));
assert.match(JSON.parse(edit.init.body).content, /A procurar voos para \*\*Madrid\*\*/);

// /vigiar e /parar também seguem para o GitHub, com a ação certa
for (const [name, options, text] of [
  ["vigiar", [{ name: "destino", value: "Roma" }, { name: "max", value: 120 },
    { name: "quando", value: "fins de semana de novembro" }], /vigilância para \*\*Roma\*\*.*até 120/],
  ["parar", [{ name: "id", value: 3 }], /parar a vigilância #3/],
  ["vigilancias", [], /lista de vigilâncias/],
  ["explorar", [{ name: "quando", value: "fins de semana de novembro" }], /para onde é mais barato/],
  ["historico", [], /histórico do Koningsdag/],
]) {
  calls.length = 0;
  waits.length = 0;
  res = await worker.fetch(await signed({ ...cmd, data: { name, options } }), env, ctx);
  assert.deepEqual(await res.json(), { type: 5 });
  await Promise.all(waits);
  const p = JSON.parse(JSON.parse(calls.find((c) => c.url.includes("api.github.com")).init.body).inputs.pedido);
  assert.equal(p.acao, name);
  const msg = JSON.parse(calls.find((c) => c.url.endsWith("@original")).init.body).content;
  assert.match(msg, text);
}

// utilizador fora da lista
res = await worker.fetch(await signed(cmd), { ...env, ALLOWED_USERS: "7,8" }, ctx);
assert.match((await res.json()).data.content, /autorização/);

// botão "👀 Vigiar" -> janela com o preço máximo
const button = { ...cmd, type: 3, data: { custom_id: "v|Madrid|2026-10-23|2026-10-25|17||1|" } };
res = await worker.fetch(await signed(button), env, ctx);
const modal = await res.json();
assert.equal(modal.type, 9);
assert.equal(modal.data.custom_id, "mv|Madrid|2026-10-23|2026-10-25|17||1|");
assert.equal(modal.data.components[0].components[0].custom_id, "max");

// janela submetida -> /vigiar com as datas do botão
calls.length = 0;
waits.length = 0;
const submit = { ...cmd, type: 5, data: { custom_id: modal.data.custom_id,
  components: [{ type: 1, components: [{ type: 4, custom_id: "max", value: "150 €" }] }] } };
res = await worker.fetch(await signed(submit), env, ctx);
assert.deepEqual(await res.json(), { type: 5 });
await Promise.all(waits);
const vp = JSON.parse(JSON.parse(calls.find((c) => c.url.includes("api.github.com")).init.body).inputs.pedido);
assert.deepEqual(vp, { acao: "vigiar", destino: "Madrid", ida: "2026-10-23", volta: "2026-10-25",
  sair_depois: 17, chegar_ate: null, bagagem: "cabine", origem: null, max: 150,
  user: "42", app_id: "123", token: "tok" });

// preço inválido na janela
res = await worker.fetch(await signed({ ...submit, data: { ...submit.data,
  components: [{ type: 1, components: [{ type: 4, custom_id: "max", value: "barato" }] }] } }), env, ctx);
assert.match((await res.json()).data.content, /só o número/);

// agendamentos: o resumo diário pede summary=true, os outros false
for (const [cron, summary] of [["30 8 * * *", "true"], ["0 7,13,19 * * *", "false"]]) {
  calls.length = 0;
  await worker.scheduled({ cron }, env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  const body = JSON.parse(calls.find((c) => c.url.includes("api.github.com")).init.body);
  assert.deepEqual(body, { ref: "master", inputs: { summary } });
}
// se o GitHub recusar, o erro aparece nos logs da Cloudflare
const posts = [];
globalThis.fetch = async (url, init) => {
  posts.push({ url, init });
  return url.includes("api.github.com") ? new Response("bad credentials", { status: 401 }) : new Response(null, { status: 204 });
};
await assert.rejects(scheduled({ cron: "30 8 * * *" }, { ...env, DISCORD_WEBHOOK_URL: "https://hook.test/x" }), /HTTP 401/);
assert.match(JSON.parse(posts.find((c) => c.url === "https://hook.test/x").init.body).content, /Não consegui arrancar/);

console.log("worker: todos os testes passaram");
