"""Regista (ou atualiza) os comandos do FlightBot no Discord.

Uso (uma vez, e de novo sempre que mudares as opções do comando):
    python discord-worker/register_commands.py <APP_ID>      # pede o token do bot
    (ou com as variáveis DISCORD_APP_ID e DISCORD_BOT_TOKEN)
"""
import getpass
import os
import sys

import requests

STRING, INTEGER = 3, 4

DESTINO = {"type": STRING, "name": "destino", "required": True,
           "description": "Cidade, país ou código do aeroporto, ex. Madrid, Holanda, MAD"}
TRIP_OPTIONS = [
    {"type": STRING, "name": "ida",
     "description": "Dia da ida, ex. 23/10 ou 'tarde 23/10' (sair a partir das 17h)"},
    {"type": STRING, "name": "volta",
     "description": "Dia da volta, ex. 25/10 ou 'cedo 25/10' (chegar a casa até às 15h)"},
    {"type": STRING, "name": "quando",
     "description": "Em vez de ida/volta: 'fins de semana de novembro', 'próximos 4 fins de semana'"},
    {"type": INTEGER, "name": "sair_depois", "min_value": 0, "max_value": 23,
     "description": "Só idas a partir desta hora (ex. 18)"},
    {"type": INTEGER, "name": "chegar_ate", "min_value": 0, "max_value": 23,
     "description": "Só voltas a chegar a casa até esta hora (ex. 14)"},
    {"type": STRING, "name": "bagagem", "description": "Incluir mala de cabine no preço",
     "choices": [{"name": "com mala de cabine", "value": "cabine"}]},
    {"type": STRING, "name": "origem",
     "description": "Aeroporto(s) de partida: OPO (defeito), 'OPO,LIS' ou 'todas' (Porto, Lisboa, Vigo...)"},
]

COMMANDS = [
    {
        "name": "flight",
        "description": "Procura os voos mais baratos (ida e volta) a partir do Porto",
        "options": [DESTINO] + TRIP_OPTIONS + [
            {"type": INTEGER, "name": "top", "min_value": 1, "max_value": 10,
             "description": "Quantas opções mostrar (por defeito 3)"},
        ],
    },
    {
        "name": "vigiar",
        "description": "Vigia uma viagem 3x por dia e avisa quando ficar abaixo do preço",
        "options": [DESTINO,
                    {"type": INTEGER, "name": "max", "required": True, "min_value": 1,
                     "description": "Preço máximo por pessoa, ida e volta (€)"}] + TRIP_OPTIONS,
    },
    {
        "name": "explorar",
        "description": "Para onde é mais barato? Testa ~25 destinos e mostra o ranking",
        "options": [o for o in TRIP_OPTIONS if o["name"] != "origem"] + [
            {"type": INTEGER, "name": "max", "min_value": 1,
             "description": "Só destinos até este preço por pessoa (€)"},
            {"type": INTEGER, "name": "top", "min_value": 1, "max_value": 25,
             "description": "Quantos destinos mostrar (por defeito 10)"},
        ],
    },
    {
        "name": "historico",
        "description": "Gráfico da evolução do preço (Koningsdag ou uma vigilância)",
        "options": [{"type": INTEGER, "name": "id", "min_value": 1,
                     "description": "Número da vigilância (vazio = Koningsdag)"}],
    },
    {
        "name": "vigilancias",
        "description": "Mostra as vigilâncias ativas",
    },
    {
        "name": "parar",
        "description": "Para uma vigilância",
        "options": [{"type": INTEGER, "name": "id", "required": True, "min_value": 1,
                     "description": "Número da vigilância (vê com /vigilancias)"}],
    },
]


def main() -> int:
    app_id = os.environ.get("DISCORD_APP_ID") or (sys.argv[1] if len(sys.argv) > 1 else "")
    token = os.environ.get("DISCORD_BOT_TOKEN") or (
        getpass.getpass("Token do bot (não aparece): ").strip() if sys.stdin.isatty() else "")
    if not app_id or not token:
        print("Uso: python discord-worker/register_commands.py <APP_ID>  (pede o token do bot)")
        return 1
    if token.startswith(("github_pat_", "ghp_")):
        print("Isso é o token do GitHub. Precisas do token do bot do Discord (Bot -> Reset Token).")
        return 1
    if token.count(".") != 2:
        print(f"Isso não parece um token de bot do Discord ({len(token)} caracteres; um token de bot "
              "tem ~70 e duas partes separadas por pontos). Confirma que colaste tudo.")
        return 1
    r = requests.put(f"https://discord.com/api/v10/applications/{app_id}/commands",
                     headers={"Authorization": f"Bot {token}"}, json=COMMANDS, timeout=15)
    if r.ok:
        print(r.status_code, "registados:", ", ".join(f"/{c['name']}" for c in r.json()))
    else:
        print(r.status_code, r.text[:500])
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
