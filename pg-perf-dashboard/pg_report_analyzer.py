#!/usr/bin/env python3
"""Agent analyzer per i report di pg_perf_check.py.

Legge il report (JSON prodotto con --json oppure il file HTML), lo fa analizzare a Claude e
produce indicazioni di tuning ordinate per priorità: configurazione, indici, vacuum, query,
connessioni, replica. Con --dsn l'agente può anche lanciare query di verifica in sola lettura
sul database per approfondire quello che il report da solo non dice.

Uso:
    python3 pg_report_analyzer.py report.json -o analisi.md --html analisi.html
    python3 pg_report_analyzer.py report.html --dsn "postgresql://monitor@dbhost/mydb"
"""

import argparse
import datetime
import html
import json
import os
import re
import sys
from html.parser import HTMLParser

MODEL = "claude-opus-5-5"
MAX_ROWS = 200          # righe massime restituite a Claude per ogni query di verifica
SQL_TIMEOUT = "15s"

STATUS_FROM_CLASS = {"s-ok": "ok", "s-warn": "warn", "s-crit": "crit", "s-info": "info", "s-na": "na"}

AREE = ["configurazione", "connessioni", "query", "indici", "vacuum", "lock", "checkpoint_wal",
        "io", "replica", "wraparound", "capacita", "altro"]

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "stato_generale": {"type": "string", "enum": ["ok", "attenzione", "critico"]},
        "sintesi": {"type": "string"},
        "raccomandazioni": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priorita": {"type": "string", "enum": ["alta", "media", "bassa"]},
                    "area": {"type": "string", "enum": AREE},
                    "titolo": {"type": "string"},
                    "problema": {"type": "string"},
                    "evidenza": {"type": "string"},
                    "azione": {"type": "string"},
                    "comandi": {"type": "array", "items": {"type": "string"}},
                    "rischio": {"type": "string"},
                    "richiede_riavvio": {"type": "boolean"},
                },
                "required": ["priorita", "area", "titolo", "problema", "evidenza", "azione",
                             "comandi", "rischio", "richiede_riavvio"],
                "additionalProperties": False,
            },
        },
        "punti_positivi": {"type": "array", "items": {"type": "string"}},
        "dati_mancanti": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stato_generale", "sintesi", "raccomandazioni", "punti_positivi", "dati_mancanti"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Sei un DBA PostgreSQL senior. Ricevi il report di un check di performance \
(generato da pg_perf_check.py su PostgreSQL 11-18) e devi dire al team cosa fare, in italiano.

Il report è diviso in sezioni, ognuna con stato (ok, warn = attenzione, crit = critico, info, \
na = non disponibile), osservazioni e tabelle. Le statistiche cumulative partono dall'ultimo \
reset o riavvio: tienilo presente prima di trarre conclusioni da contatori bassi o uptime brevi.

Come lavorare:
- Collega i sintomi tra sezioni invece di ripeterle una per una (esempio: tuple morte alte, \
idle in transaction lunghe e wraparound in crescita sono spesso lo stesso problema).
- Ogni raccomandazione deve citare nell'evidenza i numeri del report che la giustificano. \
Non inventare valori che non ci sono: se per decidere manca un dato, scrivilo in dati_mancanti \
o, se hai lo strumento run_sql, verificalo.
- Priorità alta: rischio per disponibilità o integrità (wraparound, slot lost, fsync off, \
connessioni quasi esaurite, lock che bloccano) o impatto forte e immediato sulle performance. \
Media: tuning con beneficio chiaro. Bassa: pulizia e buone pratiche.
- Le azioni devono essere concrete e adatte alla versione del server: ALTER SYSTEM con valori \
proposti e motivati dalla RAM o dal carico quando noti, CREATE INDEX CONCURRENTLY, DROP INDEX \
CONCURRENTLY, VACUUM (VERBOSE, ANALYZE), parametri di autovacuum per tabella, riscritture di \
query. Metti i comandi pronti da eseguire in comandi, uno per elemento.
- In rischio spiega cosa può andare storto e come verificare l'effetto. Indica \
richiede_riavvio = true per i parametri che richiedono il riavvio (shared_buffers, \
max_connections, wal_buffers, ecc.).
- Prima di proporre di eliminare un indice ricorda che le statistiche d'uso valgono solo per \
questo nodo: su una replica in lettura l'indice potrebbe servire.
- Non proporre nulla per le sezioni OK senza un motivo. Elenca in punti_positivi le cose che \
vanno bene e che è utile sapere.
- Ordina le raccomandazioni dalla più urgente. Meglio poche e solide che tante e generiche."""

TOOL_PROMPT = """
Hai lo strumento run_sql per lanciare query di sola lettura sul database analizzato \
(SELECT, WITH, SHOW, EXPLAIN senza ANALYZE). Usalo per approfondire quando cambia la \
raccomandazione: definizione degli indici (pg_indexes), colonne usate dalle query lente, \
parametri non presenti nel report. La RAM del server non è leggibile da SQL: se serve, \
segnalala in dati_mancanti. Fai poche \
query mirate e leggere: il server potrebbe essere in produzione. Quando hai finito rispondi \
solo con l'analisi finale."""

RUN_SQL_TOOL = {
    "name": "run_sql",
    "description": ("Esegue una query di sola lettura (SELECT, WITH, SHOW o EXPLAIN senza ANALYZE) "
                    "sul database analizzato e restituisce al massimo %d righe in JSON. Timeout %s."
                    % (MAX_ROWS, SQL_TIMEOUT)),
    "strict": True,
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Una sola istruzione SQL, senza punto e virgola finale."},
            "motivo": {"type": "string", "description": "Cosa vuoi verificare con questa query, in una frase."},
        },
        "required": ["sql", "motivo"],
        "additionalProperties": False,
    },
}


# --- lettura del report -----------------------------------------------------

class ReportHTMLParser(HTMLParser):
    """Ricostruisce la struttura del report dall'HTML di pg_perf_check.py."""

    def __init__(self):
        super().__init__()
        self.data = {"kpi": {}, "sections": []}
        self.embedded = None
        self.sec = None
        self.table = None
        self.row = None
        self.capture = None   # (tipo, buffer)
        self.tile = None
        self.li_status = None
        self.in_head = False

    def _start_capture(self, kind):
        self.capture = (kind, [])

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "script" and a.get("id") == "pg-perf-data":
            self._start_capture("embedded")
        elif tag == "section" and "card" in cls:
            self.sec = {"key": a.get("id", ""), "title": "", "status": "ok", "findings": [], "tables": []}
            self.data["sections"].append(self.sec)
        elif tag == "div" and "sub" in cls and self.sec is None:
            self._start_capture("sub")
        elif tag == "div" and "tile" in cls:
            self.tile = {}
        elif tag == "div" and self.tile is not None and cls and cls[0] in ("l", "v", "d"):
            self._start_capture("tile_" + cls[0])
        elif tag == "div" and "head" in cls and self.sec is not None:
            self.in_head = True
        elif tag == "h2" and self.sec is not None:
            self._start_capture("title")
        elif tag == "span" and "badge" in cls and self.sec is not None:
            status = next((STATUS_FROM_CLASS[c] for c in cls if c in STATUS_FROM_CLASS), "info")
            if self.in_head:
                self.sec["status"] = status
                self.in_head = False
            else:
                self.li_status = status
        elif tag == "span" and self.li_status is not None and self.capture is None:
            self._start_capture("finding")
        elif tag == "h3" and self.sec is not None:
            self.table = {"title": "", "columns": [], "rows": []}
            self.sec["tables"].append(self.table)
            self._start_capture("table_title")
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("th", "td") and self.table is not None:
            self._start_capture(tag)

    def handle_endtag(self, tag):
        if self.capture:
            kind, buf = self.capture
            text = "".join(buf).strip()
            done = {"script": "embedded", "div": "sub", "h2": "title", "h3": "table_title",
                    "th": "th", "td": "td", "span": "finding"}.get(tag)
            if tag == "div" and kind.startswith("tile_"):
                done = kind
            if done == kind:
                self.capture = None
                if kind == "embedded":
                    self.embedded = text
                elif kind == "sub":
                    self.data["header"] = text
                elif kind.startswith("tile_"):
                    self.tile[kind[5:]] = text
                elif kind == "title":
                    self.sec["title"] = text
                elif kind == "table_title":
                    self.table["title"] = text
                elif kind == "th":
                    self.table["columns"].append(text)
                elif kind == "td":
                    self.row.append(None if text == "-" else text)
                elif kind == "finding":
                    self.sec["findings"].append({"status": self.li_status, "text": text})
                    self.li_status = None
        if tag == "tr" and self.table is not None and self.row:
            self.table["rows"].append(dict(zip(self.table["columns"], self.row)))
            self.row = None
        elif tag == "div" and self.tile is not None and self.capture is None and "v" in self.tile \
                and "d" in self.tile:
            label = self.tile.get("l", "?")
            self.data["kpi"][label] = ("%s (%s)" % (self.tile["v"], self.tile["d"])).replace(" ()", "")
            self.tile = None
        elif tag == "section":
            self.sec, self.table = None, None

    def handle_data(self, data):
        if self.capture:
            self.capture[1].append(data)


def load_report(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.lstrip().startswith("{"):
        return json.loads(text)
    parser = ReportHTMLParser()
    parser.feed(text)
    if parser.embedded:
        return json.loads(parser.embedded)
    data = parser.data
    if not data["sections"]:
        sys.exit("%s non sembra un report di pg_perf_check.py (né JSON né HTML)." % path)
    m = re.search(r"server_version_num (\d+)", data.get("header", ""))
    if m:
        data["server_version_num"] = int(m.group(1))
    ranks = {"na": 0, "info": 1, "ok": 2, "warn": 3, "crit": 4}
    data["status"] = max((s["status"] for s in data["sections"]), key=ranks.get)
    return data


# --- query di verifica in sola lettura --------------------------------------

READ_ONLY_START = re.compile(r"^\s*(select|with|show|explain|table|values)\b", re.I)


def check_sql(sql):
    """Ritorna un messaggio d'errore se la query non è ammessa, altrimenti None."""
    stripped = sql.strip().rstrip(";").strip()
    if not READ_ONLY_START.match(stripped):
        return "Sono ammesse solo SELECT, WITH, SHOW ed EXPLAIN."
    if ";" in stripped:
        return "Una sola istruzione per chiamata."
    if re.match(r"^\s*explain\b", stripped, re.I) and re.search(r"\banaly[sz]e\b", stripped, re.I):
        return "EXPLAIN ANALYZE esegue davvero la query: usa EXPLAIN senza ANALYZE."
    return None


class SqlRunner:
    def __init__(self, dsn, max_queries):
        import psycopg2
        import psycopg2.extras
        self.psycopg2 = psycopg2
        self.conn = psycopg2.connect(dsn, application_name="pg_report_analyzer")
        # Sessione in sola lettura: anche una query sbagliata non può modificare nulla
        self.conn.set_session(readonly=True, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (SQL_TIMEOUT,))
            cur.execute("SET lock_timeout = '2s'")
        self.cursor_factory = psycopg2.extras.RealDictCursor
        self.max_queries = max_queries
        self.count = 0

    def run(self, sql):
        if self.count >= self.max_queries:
            return "Limite di %d query raggiunto: concludi l'analisi con i dati che hai." % self.max_queries, True
        error = check_sql(sql)
        if error:
            return error, True
        self.count += 1
        try:
            with self.conn.cursor(cursor_factory=self.cursor_factory) as cur:
                cur.execute(sql.strip().rstrip(";"))
                rows = [dict(r) for r in cur.fetchmany(MAX_ROWS + 1)] if cur.description else []
        except self.psycopg2.Error as e:
            return "Errore PostgreSQL: %s" % str(e).strip(), True
        out = {"righe": rows[:MAX_ROWS]}
        if len(rows) > MAX_ROWS:
            out["troncato"] = "mostrate solo le prime %d righe" % MAX_ROWS
        return json.dumps(out, default=str, ensure_ascii=False), False

    def close(self):
        self.conn.close()


# --- agente -------------------------------------------------------------------

def valid_tool_input(data):
    return isinstance(data, dict) and isinstance(data.get("sql"), str) and \
        isinstance(data.get("motivo"), str)


def analyze(report, model=MODEL, effort="high", sql_runner=None, client=None, log=print):
    import anthropic
    client = client or anthropic.Anthropic()

    system = SYSTEM_PROMPT + (TOOL_PROMPT if sql_runner else "")
    report_json = json.dumps(report, default=str, ensure_ascii=False)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "<report>\n%s\n</report>" % report_json},
        {"type": "text", "text": "Analizza il report e dammi le raccomandazioni."},
    ]}]
    params = dict(
        model=model,
        max_tokens=64000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        # Se il modello declina la richiesta, il server la rilancia sul modello di riserva
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        cache_control={"type": "ephemeral"},
    )
    if sql_runner:
        params["tools"] = [RUN_SQL_TOOL]

    while True:
        with client.beta.messages.stream(messages=messages, **params) as stream:
            response = stream.get_final_message()
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            raise RuntimeError("Claude ha rifiutato la richiesta: %s" % (response.stop_details,))
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Risposta troncata (max_tokens): riprova con --effort più basso.")
        if response.stop_reason == "tool_use" and sql_runner:
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name != "run_sql" or not valid_tool_input(block.input):
                    content, is_error = "Input non valido: servono i campi sql e motivo.", True
                else:
                    log("  query di verifica: %s" % block.input["motivo"])
                    content, is_error = sql_runner.run(block.input["sql"])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            continue

        texts = [b.text for b in response.content if b.type == "text"]
        if not texts:
            raise RuntimeError("Nessuna analisi nella risposta (stop_reason=%s)." % response.stop_reason)
        return json.loads(texts[-1])


# --- output -------------------------------------------------------------------

PRIO_LABEL = {"alta": "Alta", "media": "Media", "bassa": "Bassa"}
STATO_LABEL = {"ok": "OK", "attenzione": "Attenzione", "critico": "Critico"}


def render_markdown(analysis, report):
    out = ["# Analisi del report di performance PostgreSQL", ""]
    out.append("Stato generale: **%s** · server_version_num %s · report del %s · analisi del %s" % (
        STATO_LABEL[analysis["stato_generale"]], report.get("server_version_num", "?"),
        report.get("generated_at", "?"), datetime.datetime.now().isoformat(timespec="minutes")))
    out += ["", analysis["sintesi"], ""]
    recs = analysis["raccomandazioni"]
    if recs:
        out += ["## Raccomandazioni", "", "| # | Priorità | Area | Raccomandazione |", "|---|---|---|---|"]
        for i, r in enumerate(recs, 1):
            out.append("| %d | %s | %s | %s |" % (i, PRIO_LABEL[r["priorita"]], r["area"],
                                                 r["titolo"].replace("|", "\\|")))
        for i, r in enumerate(recs, 1):
            out += ["", "### %d. %s" % (i, r["titolo"]), "",
                    "*Priorità %s · area %s%s*" % (PRIO_LABEL[r["priorita"]].lower(), r["area"],
                                                    " · richiede riavvio" if r["richiede_riavvio"] else ""),
                    "", "**Problema.** " + r["problema"], "", "**Evidenza.** " + r["evidenza"], "",
                    "**Azione.** " + r["azione"]]
            if r["comandi"]:
                out += ["", "```sql"] + r["comandi"] + ["```"]
            out += ["", "**Rischi e verifica.** " + r["rischio"]]
    for title, key in (("Cosa va bene", "punti_positivi"), ("Dati mancanti", "dati_mancanti")):
        if analysis[key]:
            out += ["", "## " + title, ""] + ["- " + p for p in analysis[key]]
    return "\n".join(out) + "\n"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--muted:#5d6675;--line:#e2e5ea;--code:#f0f2f5;
--ok:#1f8a4c;--okbg:#e5f4ea;--warn:#a86400;--warnbg:#fdf1dc;--crit:#c0352b;--critbg:#fbe4e2}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#9aa3b2;
--line:#2c3340;--code:#232934;--ok:#5cc98a;--okbg:#173524;--warn:#f0b454;--warnbg:#3a2c12;
--crit:#f27c72;--critbg:#3d1d1a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}h3{font-size:15px;margin:0}
.sub{color:var(--muted);margin-bottom:16px}.sum{font-size:15px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}
.head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}
.alta,.critico{color:var(--crit);background:var(--critbg)}.media,.attenzione{color:var(--warn);background:var(--warnbg)}
.bassa,.ok{color:var(--ok);background:var(--okbg)}.tag{color:var(--muted);border:1px solid var(--line)}
p{margin:6px 0}b{font-weight:600}
pre{background:var(--code);border-radius:8px;padding:10px;overflow-x:auto;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace}
ul{padding-left:20px}footer{color:var(--muted);font-size:12px;margin-top:24px}
"""


def render_html(analysis, report):
    e = html.escape
    cards = []
    for i, r in enumerate(analysis["raccomandazioni"], 1):
        tags = "<span class='badge %s'>Priorità %s</span><span class='badge tag'>%s</span>" % (
            r["priorita"], r["priorita"], e(r["area"]))
        if r["richiede_riavvio"]:
            tags += "<span class='badge tag'>richiede riavvio</span>"
        cmds = "<pre>%s</pre>" % e("\n".join(r["comandi"])) if r["comandi"] else ""
        cards.append(
            "<section class='card'><div class='head'><h3>%d. %s</h3>%s</div>"
            "<p><b>Problema.</b> %s</p><p><b>Evidenza.</b> %s</p><p><b>Azione.</b> %s</p>%s"
            "<p><b>Rischi e verifica.</b> %s</p></section>" % (
                i, e(r["titolo"]), tags, e(r["problema"]), e(r["evidenza"]), e(r["azione"]), cmds,
                e(r["rischio"])))
    lists = ""
    for title, key in (("Cosa va bene", "punti_positivi"), ("Dati mancanti", "dati_mancanti")):
        if analysis[key]:
            lists += "<h2>%s</h2><div class='card'><ul>%s</ul></div>" % (
                title, "".join("<li>%s</li>" % e(p) for p in analysis[key]))
    stato = analysis["stato_generale"]
    return """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analisi performance PostgreSQL</title><style>%s</style></head><body><div class="wrap">
<h1>Analisi performance PostgreSQL</h1>
<div class="sub"><span class="badge %s">%s</span> · server_version_num %s · report del %s</div>
<div class="card sum">%s</div><h2>Raccomandazioni (%d)</h2>%s%s
<footer>Generato da pg_report_analyzer.py con %s. Verifica sempre i comandi prima di eseguirli in produzione.</footer>
</div></body></html>""" % (CSS, stato, STATO_LABEL[stato], e(str(report.get("server_version_num", "?"))),
                          e(str(report.get("generated_at", "?"))), e(analysis["sintesi"]),
                          len(analysis["raccomandazioni"]), "".join(cards), lists, MODEL)


def main():
    p = argparse.ArgumentParser(description="Analizza con Claude un report di pg_perf_check.py.")
    p.add_argument("report", help="report JSON (--json di pg_perf_check.py) o HTML")
    p.add_argument("-o", "--output", default="pg_analysis.md", help="analisi in Markdown")
    p.add_argument("--html", help="salva anche l'analisi in HTML")
    p.add_argument("--json", help="salva anche l'analisi in JSON")
    p.add_argument("--dsn", help="se indicato, l'agente può fare query di verifica in sola lettura")
    p.add_argument("--max-queries", type=int, default=15, help="query di verifica massime (default 15)")
    p.add_argument("--model", default=MODEL, help="modello Claude (default %s)" % MODEL)
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    args = p.parse_args()

    try:
        import anthropic
    except ImportError:
        sys.exit("Serve l'SDK Anthropic: pip install anthropic")
    report = load_report(args.report)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Nota: ANTHROPIC_API_KEY non impostata, provo il profilo di `ant auth login`.",
              file=sys.stderr)
    runner = SqlRunner(args.dsn, args.max_queries) if args.dsn else None
    print("Analisi in corso con %s (può richiedere qualche minuto)..." % args.model)
    try:
        analysis = analyze(report, model=args.model, effort=args.effort, sql_runner=runner)
    except anthropic.AuthenticationError:
        sys.exit("Chiave API non valida: imposta ANTHROPIC_API_KEY (console.anthropic.com).")
    except anthropic.APIStatusError as e:
        sys.exit("Errore dall'API Claude (%s): %s" % (e.status_code, e.message))
    except anthropic.APIConnectionError:
        sys.exit("Impossibile raggiungere l'API Claude: controlla la rete o il proxy.")
    except RuntimeError as e:
        sys.exit(str(e))
    finally:
        if runner:
            runner.close()

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(render_markdown(analysis, report))
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(analysis, report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
    counts = {k: sum(1 for r in analysis["raccomandazioni"] if r["priorita"] == k)
              for k in ("alta", "media", "bassa")}
    print("Analisi scritta in %s - stato: %s, raccomandazioni: %d alta, %d media, %d bassa" % (
        args.output, STATO_LABEL[analysis["stato_generale"]], counts["alta"], counts["media"],
        counts["bassa"]))


if __name__ == "__main__":
    main()
