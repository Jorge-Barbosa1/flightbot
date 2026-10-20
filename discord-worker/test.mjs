// Testes do worker sem rede: `node discord-worker/test.mjs`
import assert from "node:assert/strict";
import worker from "./src/index.js";

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

console.log("worker: todos os testes passaram");
