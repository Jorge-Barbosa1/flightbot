"""Regista (ou atualiza) o comando /flight na app do Discord.

Uso (uma vez, e de novo sempre que mudares as opções do comando):
    DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... python discord-worker/register_commands.py
"""
import os
import sys

import requests

STRING, INTEGER = 3, 4

COMMANDS = [
    {
        "name": "flight",
        "description": "Procura os voos mais baratos (ida e volta) a partir do Porto",
        "options": [
            {"type": STRING, "name": "destino", "required": True,
             "description": "Cidade ou código do aeroporto, ex. Madrid, Londres, MAD"},
            {"type": STRING, "name": "ida", "required": True,
             "description": "Dia da ida, ex. 23/10 ou 'tarde 23/10' (sair a partir das 17h)"},
            {"type": STRING, "name": "volta", "required": True,
             "description": "Dia da volta, ex. 25/10 ou 'cedo 25/10' (chegar a casa até às 15h)"},
            {"type": INTEGER, "name": "sair_depois", "min_value": 0, "max_value": 23,
             "description": "Só idas a partir desta hora (ex. 18)"},
            {"type": INTEGER, "name": "chegar_ate", "min_value": 0, "max_value": 23,
             "description": "Só voltas a chegar a casa até esta hora (ex. 14)"},
            {"type": INTEGER, "name": "top", "min_value": 1, "max_value": 10,
             "description": "Quantas opções mostrar (por defeito 3)"},
            {"type": STRING, "name": "origem",
             "description": "Aeroporto de partida (por defeito OPO)"},
        ],
    }
]


def main() -> int:
    app_id, token = os.environ.get("DISCORD_APP_ID"), os.environ.get("DISCORD_BOT_TOKEN")
    if not app_id or not token:
        print("Define DISCORD_APP_ID e DISCORD_BOT_TOKEN.")
        return 1
    r = requests.put(f"https://discord.com/api/v10/applications/{app_id}/commands",
                     headers={"Authorization": f"Bot {token}"}, json=COMMANDS, timeout=15)
    print(r.status_code, r.text[:500])
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
