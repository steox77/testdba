# AI locale per PostgreSQL/EDB

Analisi di PostgreSQL/EDB in linguaggio naturale con un modello eseguito in locale
(Ollama + Qwen) e [pg-airman-mcp](https://pypi.org/project/pg-airman-mcp/), senza servizi AI cloud.

| File | Fase | Dove si esegue |
| --- | --- | --- |
| `01_raccolta_info.sh` | 1 | Server della soluzione esistente (solo lettura, maschera le password) |
| `02_prepara_bundle.sh` | 3 | Macchina Linux con Internet: prepara il pacchetto offline |
| `03_installa_sandbox.sh` | 3 | Server RHEL 8 della sandbox, come root |
| `04_utente_readonly.sql` | 4 | Database di test, come superuser |
| `collaudo.py` | 5 | Server sandbox, nel venv di pg-airman-mcp |
| `domande_collaudo.json` | 5 | Domande di collaudo e query di controllo |

Note su pg-airman-mcp 1.1.1: installare con il vincolo `mcp<2`, aggiungere `python-dotenv`,
avviarlo sempre con `--access-mode=restricted` e `ALLOW_COMMENT_IN_RESTRICTED=false`,
non impostare mai `OPENAI_API_KEY`.
