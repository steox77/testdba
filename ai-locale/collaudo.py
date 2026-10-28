#!/usr/bin/env python3
"""
collaudo.py - collaudo automatico di Ollama + Qwen + pg-airman-mcp (Fase 5).

Per ogni domanda in linguaggio naturale:
  * avvia la conversazione con il modello Ollama passando i tool di pg-airman-mcp
  * esegue i tool richiesti dal modello (pg-airman-mcp in stdio, modalita' restricted)
  * misura tempi (totale, modello, tool), token/s, numero di chiamate
  * misura l'impatto sul DB (delta di pg_stat_statements per l'utenza usata)
  * salva risposta e traccia in JSON, CSV e un report Markdown da far validare al DBA

Uso (sul server sandbox, nel venv di pg-airman-mcp):
  export AIRMAN_MCP_DATABASE_URL='postgresql://ai_readonly:***@dbtest:5432/testdb'
  python collaudo.py --model qwen3:14b --domande domande_collaudo.json --out risultati/

Nessuna dipendenza oltre a quelle di pg-airman-mcp (mcp, psycopg).
"""
import argparse
import asyncio
import csv
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SYSTEM_PROMPT = (
    "Sei un assistente DBA per PostgreSQL/EDB. Rispondi in italiano. "
    "Usa i tool disponibili per ottenere dati reali dal database: non inventare valori. "
    "Il database e' in sola lettura: non proporre di eseguire modifiche, ma se utile "
    "suggerisci comandi che il DBA potra' valutare. Cita i numeri su cui basi le conclusioni."
)

# Tool che non servono al collaudo o che scrivono metadati: non vengono esposti al modello.
TOOL_ESCLUSI = {
    "add_comment_to_object", "remove_comment", "refresh_kb",
    "create_alias", "delete_alias",
}

STATS_SQL = """
SELECT coalesce(sum(calls), 0)::bigint,
       coalesce(sum(total_exec_time), 0)::float,
       coalesce(sum(shared_blks_hit + shared_blks_read), 0)::bigint,
       coalesce(sum(shared_blks_read), 0)::bigint,
       coalesce(sum(temp_blks_written), 0)::bigint
FROM pg_stat_statements s
JOIN pg_roles r ON r.oid = s.userid
WHERE r.rolname = current_user
"""


def ollama_chat(url, payload, timeout):
    req = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def db_stats(dsn):
    """Snapshot pg_stat_statements per l'utenza corrente (None se non disponibile)."""
    try:
        with psycopg.connect(dsn, autocommit=True, application_name="airman-collaudo") as c:
            row = c.execute(STATS_SQL).fetchone()
        return dict(zip(("calls", "exec_ms", "blks", "blks_read", "temp_blks"), row))
    except Exception as e:  # pg_stat_statements assente o permessi insufficienti
        print(f"  [avviso] statistiche DB non disponibili: {e}", file=sys.stderr)
        return None


def tool_to_ollama(t):
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": (t.description or "")[:1024],
            "parameters": t.inputSchema or {"type": "object", "properties": {}},
        },
    }


def risultato_testo(res):
    parti = []
    for c in res.content:
        parti.append(getattr(c, "text", None) or str(c))
    return "\n".join(parti)


async def esegui_domanda(sess, tools, d, a):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": d["domanda"]}]
    traccia, chiamate = [], []
    t_modello = t_tool = 0.0
    tok_out = tok_in = 0
    eval_ns = 0
    errore = None
    t0 = time.perf_counter()
    for giro in range(a.max_giri):
        payload = {
            "model": a.model, "messages": messages, "tools": tools, "stream": False,
            "options": {"temperature": a.temperature, "num_ctx": a.num_ctx},
            "keep_alive": "30m",
        }
        if a.think is not None:
            payload["think"] = a.think
        tm = time.perf_counter()
        try:
            r = ollama_chat(a.ollama, payload, a.timeout)
        except Exception as e:
            errore = f"Ollama: {e}"
            break
        t_modello += time.perf_counter() - tm
        tok_out += r.get("eval_count", 0)
        tok_in += r.get("prompt_eval_count", 0)
        eval_ns += r.get("eval_duration", 0)
        msg = r.get("message", {})
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        for c in calls:
            nome = c["function"]["name"]
            args = c["function"].get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tt = time.perf_counter()
            try:
                if nome in TOOL_ESCLUSI:
                    raise RuntimeError("tool non consentito nel collaudo")
                res = await asyncio.wait_for(sess.call_tool(nome, args), a.timeout)
                out = risultato_testo(res)
                # pg-airman-mcp restituisce gli errori come testo "Error: ..." senza isError
                ok = not getattr(res, "isError", False) and not out.lstrip().startswith("Error")
            except Exception as e:
                out, ok = f"ERRORE: {e}", False
            dur = time.perf_counter() - tt
            t_tool += dur
            chiamate.append(nome)
            traccia.append({"tool": nome, "args": args, "ok": ok, "secondi": round(dur, 2),
                            "output": out[:4000]})
            messages.append({"role": "tool", "tool_name": nome, "content": out[: a.max_tool_chars]})
    else:
        errore = f"superato il limite di {a.max_giri} giri di tool"

    risposta = messages[-1].get("content", "") if messages[-1]["role"] == "assistant" else ""
    attesi = set(d.get("tool_attesi", []))
    return {
        "id": d["id"], "categoria": d.get("categoria", ""), "domanda": d["domanda"],
        "risposta": risposta, "errore": errore,
        "tool_chiamati": chiamate,
        "tool_in_errore": [t["tool"] for t in traccia if not t["ok"]],
        "tool_attesi_usati": bool(attesi & set(chiamate)) if attesi else None,
        "secondi_totali": round(time.perf_counter() - t0, 2),
        "secondi_modello": round(t_modello, 2), "secondi_tool": round(t_tool, 2),
        "token_input": tok_in, "token_output": tok_out,
        "token_al_secondo": round(tok_out / (eval_ns / 1e9), 1) if eval_ns else None,
        "controllo_dba": d.get("controllo", ""), "traccia": traccia,
    }


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", required=True, help="es. qwen3:14b")
    p.add_argument("--domande", default="domande_collaudo.json")
    p.add_argument("--out", default="risultati")
    p.add_argument("--ollama", default=os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434"))
    p.add_argument("--dsn", default=os.environ.get("AIRMAN_MCP_DATABASE_URL"),
                   help="URI del DB (default: AIRMAN_MCP_DATABASE_URL)")
    p.add_argument("--airman-cmd", default=shutil.which("pg-airman-mcp") or "pg-airman-mcp")
    p.add_argument("--solo", help="esegue solo gli id indicati, separati da virgola")
    p.add_argument("--ripetizioni", type=int, default=1, help="ripete ogni domanda N volte")
    p.add_argument("--num-ctx", type=int, default=16384)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--think", choices=["true", "false"], help="attiva/disattiva il 'thinking' di Qwen3")
    p.add_argument("--max-giri", type=int, default=8)
    p.add_argument("--max-tool-chars", type=int, default=12000)
    p.add_argument("--timeout", type=float, default=600)
    a = p.parse_args()
    if a.think is not None:
        a.think = a.think == "true"
    if not a.dsn:
        sys.exit("Impostare AIRMAN_MCP_DATABASE_URL o --dsn")

    domande = json.loads(Path(a.domande).read_text(encoding="utf-8"))
    if a.solo:
        ids = set(a.solo.split(","))
        domande = [d for d in domande if d["id"] in ids]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    env = dict(os.environ, AIRMAN_MCP_DATABASE_URL=a.dsn, ALLOW_COMMENT_IN_RESTRICTED="false")
    env.pop("OPENAI_API_KEY", None)  # mai chiamate a servizi cloud
    server = StdioServerParameters(
        command=a.airman_cmd,
        args=["--access-mode=restricted", "--purpose=collaudo-ai"],
        env=env,
    )
    risultati = []
    async with stdio_client(server) as (r, w), ClientSession(r, w) as sess:
        await sess.initialize()
        elenco = (await sess.list_tools()).tools
        tools = [tool_to_ollama(t) for t in elenco if t.name not in TOOL_ESCLUSI]
        print(f"pg-airman-mcp: {len(elenco)} tool, {len(tools)} esposti al modello {a.model}")
        for d in domande:
            for rep in range(a.ripetizioni):
                print(f"- [{d['id']}] {d['domanda'][:70]}", flush=True)
                prima = db_stats(a.dsn)
                res = await esegui_domanda(sess, tools, d, a)
                dopo = db_stats(a.dsn)
                if prima and dopo:
                    res["db_impatto"] = {k: round(dopo[k] - prima[k], 2) for k in prima}
                    res["db_impatto"]["calls"] -= 1  # esclude la query di snapshot stessa
                res["ripetizione"] = rep + 1
                risultati.append(res)
                print(f"    {res['secondi_totali']}s, tool={res['tool_chiamati']}"
                      f"{', ERRORE: ' + res['errore'] if res['errore'] else ''}", flush=True)

    meta = {"modello": a.model, "data": stamp, "num_ctx": a.num_ctx,
            "temperature": a.temperature, "think": a.think, "ollama": a.ollama}
    (out / f"collaudo_{stamp}.json").write_text(
        json.dumps({"meta": meta, "risultati": risultati}, ensure_ascii=False, indent=2), encoding="utf-8")

    campi = ["id", "ripetizione", "categoria", "secondi_totali", "secondi_modello", "secondi_tool",
             "token_al_secondo", "token_input", "token_output", "tool_chiamati", "tool_in_errore",
             "tool_attesi_usati",
             "db_calls", "db_exec_ms", "db_blks_read", "errore"]
    with open(out / f"collaudo_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=campi, delimiter=";")
        wr.writeheader()
        for r in risultati:
            imp = r.get("db_impatto") or {}
            wr.writerow({**{k: r.get(k) for k in campi},
                         "tool_chiamati": " ".join(r["tool_chiamati"]),
                         "tool_in_errore": " ".join(r["tool_in_errore"]),
                         "db_calls": imp.get("calls"), "db_exec_ms": imp.get("exec_ms"),
                         "db_blks_read": imp.get("blks_read")})

    righe = [f"# Collaudo {a.model} - {stamp}", "",
             "Valutazione DBA: per ogni domanda indicare Corretta / Parziale / Errata.", ""]
    for r in risultati:
        righe += [f"## {r['id']} - {r['domanda']}", "",
                  f"Tempo {r['secondi_totali']} s (modello {r['secondi_modello']} s, tool {r['secondi_tool']} s), "
                  f"tool: {', '.join(r['tool_chiamati']) or 'nessuno'}, "
                  f"impatto DB: {r.get('db_impatto')}", ""]
        if r["errore"]:
            righe += [f"**Errore:** {r['errore']}", ""]
        righe += [r["risposta"] or "_nessuna risposta_", "",
                  f"Controllo DBA: {r['controllo_dba']}", "",
                  "Esito DBA: [ ] Corretta  [ ] Parziale  [ ] Errata", "", "---", ""]
    (out / f"collaudo_{stamp}.md").write_text("\n".join(righe), encoding="utf-8")

    tempi = sorted(r["secondi_totali"] for r in risultati)
    if tempi:
        p50 = tempi[len(tempi) // 2]
        p90 = tempi[min(len(tempi) - 1, int(len(tempi) * 0.9))]
        print(f"\n{len(risultati)} esecuzioni, errori: {sum(1 for r in risultati if r['errore'])}, "
              f"tempo p50={p50}s p90={p90}s")
    print(f"Report in {out}/collaudo_{stamp}.(json|csv|md)")


if __name__ == "__main__":
    asyncio.run(main())
