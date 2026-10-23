#!/usr/bin/env python3
"""
pg_perf_check.py - Dashboard di check performance per PostgreSQL 11 -> 18.

Si collega a un'istanza PostgreSQL, rileva la versione (server_version_num)
e lancia query diagnostiche adattate alla versione, poi genera un report HTML
autonomo (nessuna dipendenza esterna, apribile offline) e, opzionalmente, un JSON.

Esempi:
    python3 pg_perf_check.py -H dbhost -U postgres -d mydb -o report.html
    python3 pg_perf_check.py --dsn "postgresql://user@host:5432/mydb" --json report.json
    PGHOST=... PGUSER=... python3 pg_perf_check.py        # usa le variabili libpq

Per risultati completi usare un utente superuser o con ruolo pg_monitor.
"""

import argparse
import datetime
import html
import json
import sys

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("Serve psycopg2: pip install psycopg2-binary")

MIN_VERSION = 110000

# Le dimensioni delle relazioni sono calcolate da pg_class.relpages (aggiornato da
# VACUUM/ANALYZE) invece che con pg_relation_size(): quest'ultima prende un lock e
# resterebbe bloccata dietro a DDL o LOCK TABLE in corso sul server analizzato.
BLK = "current_setting('block_size')::bigint"
TOTAL_BYTES = ("((c.relpages + coalesce((SELECT t.relpages FROM pg_class t WHERE t.oid = c.reltoastrelid), 0)"
               " + coalesce((SELECT sum(ic.relpages) FROM pg_index ix JOIN pg_class ic"
               " ON ic.oid = ix.indexrelid WHERE ix.indrelid = c.oid), 0))::bigint * " + BLK + ")")

SECTION_TITLES = {
    "server": "Server", "settings": "Parametri di configurazione", "connections": "Connessioni",
    "long_queries": "Query in esecuzione e wait event", "locks": "Lock e sessioni bloccate",
    "databases": "Statistiche per database", "wraparound": "Età delle transazioni (wraparound)",
    "checkpoints": "Checkpoint e background writer", "wal": "WAL", "io": "I/O per tipo di processo",
    "statements": "Query più costose (pg_stat_statements)", "vacuum": "Vacuum e tuple morte",
    "seq_scans": "Sequential scan", "indexes": "Indici", "table_sizes": "Tabelle più grandi",
    "replication": "Replica e slot",
}

OK, WARN, CRIT, INFO, NA = "ok", "warn", "crit", "info", "na"
STATUS_LABEL = {OK: "OK", WARN: "Attenzione", CRIT: "Critico", INFO: "Info", NA: "N/D"}
STATUS_RANK = {NA: 0, INFO: 1, OK: 2, WARN: 3, CRIT: 4}


def worst(*statuses):
    return max(statuses, key=lambda s: STATUS_RANK[s]) if statuses else OK


class Section:
    def __init__(self, key, title, description=""):
        self.key = key
        self.title = title
        self.description = description
        self.status = OK
        self.findings = []  # (status, testo)
        self.tables = []    # (titolo, colonne, righe)

    def add(self, status, text):
        self.findings.append((status, text))
        self.status = worst(self.status, status)

    def table(self, title, rows, columns=None):
        if rows:
            columns = columns or list(rows[0].keys())
        self.tables.append((title, columns or [], rows))

    def to_dict(self):
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "findings": [{"status": s, "text": t} for s, t in self.findings],
            "tables": [{"title": t, "columns": c, "rows": r} for t, c, r in self.tables],
        }


class Checker:
    def __init__(self, conn, top_n=15):
        self.conn = conn
        self.top_n = top_n
        self.ver = int(self.scalar("SHOW server_version_num"))
        self.in_recovery = self.scalar("SELECT pg_is_in_recovery()")
        self.sections = []
        self.kpi = {}

    # --- helper -----------------------------------------------------------
    def query(self, sql, params=None):
        sql = sql.replace("@TOTAL_BYTES@", TOTAL_BYTES).replace("@BLK@", BLK)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    def scalar(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None

    def setting(self, name):
        return self.scalar("SELECT setting FROM pg_settings WHERE name = %s", (name,))

    def run(self):
        if self.ver < MIN_VERSION:
            print("Attenzione: versione %s non supportata (minimo 11); alcune sezioni "
                  "potrebbero fallire." % self.ver, file=sys.stderr)
        checks = [
            self.check_server,
            self.check_settings,
            self.check_connections,
            self.check_long_queries,
            self.check_locks,
            self.check_databases,
            self.check_wraparound,
            self.check_checkpoints,
            self.check_wal,
            self.check_io,
            self.check_statements,
            self.check_vacuum,
            self.check_seq_scans,
            self.check_indexes,
            self.check_table_sizes,
            self.check_replication,
        ]
        for check in checks:
            before = len(self.sections)
            try:
                check()
            except psycopg2.Error as e:
                # Una sezione che fallisce (es. permessi, lock_timeout) non blocca il report
                del self.sections[before:]
                key = check.__name__.replace("check_", "")
                sec = Section(key, SECTION_TITLES.get(key, key))
                sec.add(NA, "Check non eseguito: %s" % str(e).strip().splitlines()[0])
                self.sections.append(sec)
        return self

    # --- check ------------------------------------------------------------
    def check_server(self):
        sec = Section("server", "Server", "Versione, uptime e dimensione dei database.")
        info = self.query("""
            SELECT version() AS versione,
                   current_setting('server_version') AS server_version,
                   pg_postmaster_start_time()::timestamp(0) AS avvio,
                   date_trunc('second', now() - pg_postmaster_start_time())::text AS uptime,
                   current_database() AS database_corrente,
                   pg_is_in_recovery() AS standby,
                   (SELECT pg_size_pretty(sum(pg_database_size(oid))::bigint) FROM pg_database
                     WHERE datallowconn) AS dimensione_totale
        """)[0]
        self.kpi["version"] = info["server_version"]
        self.kpi["uptime"] = info["uptime"]
        self.kpi["role"] = "Standby" if info["standby"] else "Primario"
        self.kpi["database"] = info["database_corrente"]
        major = self.ver // 10000
        sec.add(INFO, "PostgreSQL %s (%s), avviato da %s." % (
            info["server_version"], self.kpi["role"].lower(), info["uptime"]))
        if major < 14:
            sec.add(WARN, "La versione %d non riceve più aggiornamenti dalla community: "
                          "pianificare un upgrade." % major)
        sec.table("Dettagli", [info])
        sec.table("Database", self.query("""
            SELECT datname AS database, pg_size_pretty(pg_database_size(oid)) AS dimensione
            FROM pg_database WHERE datallowconn ORDER BY pg_database_size(oid) DESC
        """))
        self.sections.append(sec)

    def check_settings(self):
        sec = Section("settings", "Parametri di configurazione",
                      "Principali parametri che incidono sulle performance.")
        names = ["max_connections", "shared_buffers", "effective_cache_size", "work_mem",
                 "maintenance_work_mem", "random_page_cost", "effective_io_concurrency",
                 "max_wal_size", "min_wal_size", "checkpoint_timeout",
                 "checkpoint_completion_target", "wal_buffers", "max_worker_processes",
                 "max_parallel_workers", "max_parallel_workers_per_gather", "autovacuum",
                 "autovacuum_max_workers", "autovacuum_vacuum_scale_factor",
                 "autovacuum_vacuum_cost_limit", "track_io_timing", "fsync",
                 "synchronous_commit", "shared_preload_libraries", "jit", "huge_pages",
                 "log_min_duration_statement", "idle_in_transaction_session_timeout",
                 "statement_timeout"]
        rows = self.query("""
            SELECT name AS parametro,
                   CASE WHEN unit = '8kB' THEN pg_size_pretty(setting::bigint * 8192)
                        WHEN unit = 'kB' THEN pg_size_pretty(setting::bigint * 1024)
                        WHEN unit = 'MB' THEN pg_size_pretty(setting::bigint * 1024 * 1024)
                        ELSE setting || coalesce(' ' || unit, '') END AS valore,
                   source AS origine
            FROM pg_settings WHERE name = ANY(%s) ORDER BY name
        """, (names,))
        s = {r["parametro"]: r["valore"] for r in rows}
        if s.get("fsync") == "off":
            sec.add(CRIT, "fsync è disattivato: rischio di corruzione dei dati in caso di crash.")
        if s.get("autovacuum") == "off":
            sec.add(CRIT, "autovacuum è disattivato: bloat e rischio di wraparound degli XID.")
        if s.get("track_io_timing") == "off":
            sec.add(INFO, "track_io_timing è off: i tempi di I/O non vengono misurati "
                          "(attivarlo costa poco sulla maggior parte dei sistemi).")
        if int(self.setting("max_connections")) > 500:
            sec.add(WARN, "max_connections è molto alto: valutare un connection pooler "
                          "(es. PgBouncer).")
        if float(self.setting("random_page_cost")) >= 4:
            sec.add(INFO, "random_page_cost è al default (4): su SSD/NVMe valori 1.1-2 "
                          "favoriscono l'uso degli indici.")
        shared_buffers_bytes = int(self.setting("shared_buffers")) * 8192
        if shared_buffers_bytes <= 128 * 1024 * 1024:
            sec.add(WARN, "shared_buffers è al default (128MB): di solito si usa ~25% della RAM.")
        if "pg_stat_statements" not in (s.get("shared_preload_libraries") or ""):
            sec.add(INFO, "pg_stat_statements non è in shared_preload_libraries: "
                          "nessuna statistica sulle query.")
        if not sec.findings:
            sec.add(OK, "Nessun parametro critico rilevato.")
        sec.table("Parametri", rows)
        self.sections.append(sec)

    def check_connections(self):
        sec = Section("connections", "Connessioni",
                      "Utilizzo delle connessioni e sessioni idle in transaction.")
        max_conn = int(self.setting("max_connections"))
        reserved = int(self.setting("superuser_reserved_connections") or 0)
        rows = self.query("""
            SELECT coalesce(state, 'n/d') AS stato, count(*) AS sessioni
            FROM pg_stat_activity WHERE backend_type = 'client backend'
            GROUP BY 1 ORDER BY 2 DESC
        """)
        total = sum(r["sessioni"] for r in rows)
        pct = 100.0 * total / max(max_conn - reserved, 1)
        self.kpi["connections"] = (total, max_conn, pct)
        status = CRIT if pct >= 90 else WARN if pct >= 75 else OK
        sec.add(status, "%d connessioni client su %d disponibili (%.0f%%)." % (
            total, max_conn - reserved, pct))
        idle_tx = self.query("""
            SELECT pid, usename AS utente, datname AS database, application_name AS applicazione,
                   client_addr::text AS client,
                   date_trunc('second', now() - xact_start)::text AS durata_transazione,
                   date_trunc('second', now() - state_change)::text AS idle_da,
                   left(query, 200) AS ultima_query
            FROM pg_stat_activity
            WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
            ORDER BY state_change LIMIT %s
        """, (self.top_n,))
        long_idle = [r for r in self.query("""
            SELECT pid FROM pg_stat_activity
            WHERE state LIKE 'idle in transaction%%' AND now() - state_change > interval '5 minutes'
        """)]
        if long_idle:
            sec.add(WARN, "%d sessioni idle in transaction da oltre 5 minuti: tengono lock e "
                          "bloccano il vacuum." % len(long_idle))
        sec.table("Sessioni per stato", rows)
        sec.table("Sessioni idle in transaction", idle_tx)
        sec.table("Sessioni per utente/applicazione", self.query("""
            SELECT usename AS utente, datname AS database, application_name AS applicazione,
                   count(*) AS sessioni,
                   count(*) FILTER (WHERE state = 'active') AS attive
            FROM pg_stat_activity WHERE backend_type = 'client backend'
            GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT %s
        """, (self.top_n,)))
        self.sections.append(sec)

    def check_long_queries(self):
        sec = Section("activity", "Query in esecuzione e wait event",
                      "Query attive ordinate per durata e attese correnti.")
        rows = self.query("""
            SELECT pid, usename AS utente, datname AS database,
                   date_trunc('second', now() - query_start)::text AS durata,
                   extract(epoch FROM now() - query_start)::int AS secondi,
                   wait_event_type AS tipo_attesa, wait_event AS attesa,
                   left(query, 300) AS query
            FROM pg_stat_activity
            WHERE state = 'active' AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
            ORDER BY query_start LIMIT %s
        """, (self.top_n,))
        longest = max([r["secondi"] or 0 for r in rows] or [0])
        if longest > 1800:
            sec.add(CRIT, "Query attiva da oltre 30 minuti.")
        elif longest > 300:
            sec.add(WARN, "Query attiva da oltre 5 minuti.")
        else:
            sec.add(OK, "%d query attive, nessuna oltre 5 minuti." % len(rows))
        for r in rows:
            r.pop("secondi", None)
        sec.table("Query attive", rows)
        waits = self.query("""
            SELECT wait_event_type AS tipo_attesa, wait_event AS attesa, count(*) AS sessioni
            FROM pg_stat_activity
            WHERE state = 'active' AND wait_event IS NOT NULL AND pid <> pg_backend_pid()
            GROUP BY 1, 2 ORDER BY 3 DESC
        """)
        sec.table("Wait event delle sessioni attive", waits)
        progress = self.query("""
            SELECT p.pid, p.datname AS database, p.relid::regclass::text AS tabella, p.phase AS fase,
                   date_trunc('second', now() - a.xact_start)::text AS durata
            FROM pg_stat_progress_vacuum p JOIN pg_stat_activity a USING (pid)
        """)
        if progress:
            sec.table("Vacuum in corso", progress)
        self.sections.append(sec)

    def check_locks(self):
        sec = Section("locks", "Lock e sessioni bloccate",
                      "Sessioni in attesa di lock tenuti da altre sessioni.")
        rows = self.query("""
            SELECT a.pid AS pid_bloccato, pg_blocking_pids(a.pid)::text AS bloccato_da,
                   a.usename AS utente, a.datname AS database,
                   a.wait_event_type AS tipo_attesa, a.wait_event AS attesa,
                   date_trunc('second', now() - a.query_start)::text AS in_attesa_da,
                   left(a.query, 200) AS query
            FROM pg_stat_activity a
            WHERE cardinality(pg_blocking_pids(a.pid)) > 0
            ORDER BY a.query_start
        """)
        self.kpi["blocked"] = len(rows)
        if rows:
            sec.add(CRIT if len(rows) >= 5 else WARN, "%d sessioni bloccate da lock." % len(rows))
            sec.table("Sessioni bloccate", rows)
            sec.table("Sessioni bloccanti", self.query("""
                SELECT DISTINCT b.pid, b.usename AS utente, b.state AS stato,
                       date_trunc('second', now() - b.xact_start)::text AS durata_transazione,
                       left(b.query, 200) AS query
                FROM pg_stat_activity a
                JOIN pg_stat_activity b ON b.pid = ANY(pg_blocking_pids(a.pid))
            """))
        else:
            sec.add(OK, "Nessuna sessione bloccata.")
        self.sections.append(sec)

    def check_databases(self):
        sec = Section("databases", "Statistiche per database",
                      "Cache hit ratio, rollback, deadlock e file temporanei (da pg_stat_database).")
        extra = ""
        if self.ver >= 120000:
            extra += ", checksum_failures AS errori_checksum"
        if self.ver >= 140000:
            extra += (", sessions_abandoned AS sessioni_abbandonate, sessions_fatal AS sessioni_fatal,"
                      " sessions_killed AS sessioni_terminate")
        rows = self.query("""
            SELECT datname AS database, numbackends AS connessioni,
                   round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS cache_hit_pct,
                   blks_hit + blks_read AS letture_blocchi,
                   xact_commit AS commit, xact_rollback AS rollback,
                   round(100.0 * xact_rollback / nullif(xact_commit + xact_rollback, 0), 2) AS rollback_pct,
                   deadlocks, conflicts AS conflitti, temp_files AS file_temp,
                   pg_size_pretty(temp_bytes) AS dati_temp, temp_bytes,
                   stats_reset::timestamp(0) AS reset_statistiche %s
            FROM pg_stat_database
            WHERE datname IS NOT NULL AND datname NOT IN ('template0', 'template1')
            ORDER BY blks_hit + blks_read DESC
        """ % extra)
        for r in rows:
            name = r["database"]
            busy = (r["letture_blocchi"] or 0) > 10000
            hit = r["cache_hit_pct"]
            if busy and hit is not None:
                if hit < 95:
                    sec.add(CRIT, "%s: cache hit ratio %.2f%% (< 95%%)." % (name, hit))
                elif hit < 99:
                    sec.add(WARN, "%s: cache hit ratio %.2f%% (< 99%%)." % (name, hit))
            if (r["rollback_pct"] or 0) > 5 and (r["commit"] or 0) > 1000:
                sec.add(WARN, "%s: %.1f%% delle transazioni va in rollback." % (name, r["rollback_pct"]))
            if r["deadlocks"]:
                sec.add(WARN, "%s: %d deadlock dall'ultimo reset delle statistiche." % (
                    name, r["deadlocks"]))
            if (r["temp_bytes"] or 0) > 10 * 1024 ** 3:
                sec.add(WARN, "%s: %s scritti in file temporanei; valutare work_mem." % (
                    name, r["dati_temp"]))
            if r.get("errori_checksum"):
                sec.add(CRIT, "%s: %d errori di checksum sui dati!" % (name, r["errori_checksum"]))
            r.pop("temp_bytes", None)
            r.pop("letture_blocchi", None)
        total_hit = self.scalar("""
            SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit + blks_read), 0), 2)
            FROM pg_stat_database
        """)
        self.kpi["cache_hit"] = float(total_hit) if total_hit is not None else None
        if not sec.findings:
            sec.add(OK, "Cache hit, rollback e file temporanei nella norma.")
        sec.table("Database", rows)
        self.sections.append(sec)

    def check_wraparound(self):
        sec = Section("wraparound", "Età delle transazioni (wraparound)",
                      "Distanza dal limite di 2 miliardi di XID: oltre questa soglia il DB si ferma.")
        rows = self.query("""
            SELECT datname AS database, age(datfrozenxid) AS eta_xid,
                   round(100.0 * age(datfrozenxid) / 2147483647, 2) AS pct_limite_xid,
                   mxid_age(datminmxid) AS eta_multixact,
                   current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age
            FROM pg_database ORDER BY age(datfrozenxid) DESC
        """)
        top = max([float(r["pct_limite_xid"]) for r in rows] or [0])
        self.kpi["xid_pct"] = top
        if top >= 75:
            sec.add(CRIT, "Un database ha superato il 75% del limite XID: VACUUM FREEZE urgente.")
        elif top >= 50:
            sec.add(WARN, "Un database ha superato il 50% del limite XID.")
        else:
            sec.add(OK, "Età massima XID al %.1f%% del limite." % top)
        sec.table("Database", rows)
        sec.table("Tabelle più vecchie (database corrente)", self.query("""
            SELECT c.oid::regclass::text AS tabella, age(c.relfrozenxid) AS eta_xid,
                   pg_size_pretty(@TOTAL_BYTES@) AS dimensione
            FROM pg_class c WHERE c.relkind IN ('r', 'm', 't')
            ORDER BY age(c.relfrozenxid) DESC LIMIT 10
        """))
        self.sections.append(sec)

    def check_checkpoints(self):
        sec = Section("checkpoints", "Checkpoint e background writer",
                      "Checkpoint richiesti (per WAL pieno) rispetto a quelli a tempo.")
        if self.ver >= 170000:
            # PG17: le statistiche dei checkpoint passano in pg_stat_checkpointer
            cp = self.query("""
                SELECT num_timed AS a_tempo, num_requested AS richiesti,
                       round(write_time / 1000) AS scrittura_s, round(sync_time / 1000) AS sync_s,
                       buffers_written AS buffer_scritti, stats_reset::timestamp(0) AS reset_statistiche
                FROM pg_stat_checkpointer
            """)[0]
            bg = self.query("""
                SELECT buffers_clean AS buffer_bgwriter, maxwritten_clean AS stop_maxwritten,
                       buffers_alloc AS buffer_allocati, stats_reset::timestamp(0) AS reset_statistiche
                FROM pg_stat_bgwriter
            """)[0]
        else:
            row = self.query("""
                SELECT checkpoints_timed AS a_tempo, checkpoints_req AS richiesti,
                       round(checkpoint_write_time / 1000) AS scrittura_s,
                       round(checkpoint_sync_time / 1000) AS sync_s,
                       buffers_checkpoint AS buffer_scritti, buffers_clean AS buffer_bgwriter,
                       maxwritten_clean AS stop_maxwritten, buffers_backend AS buffer_backend,
                       buffers_backend_fsync AS fsync_backend, buffers_alloc AS buffer_allocati,
                       stats_reset::timestamp(0) AS reset_statistiche
                FROM pg_stat_bgwriter
            """)[0]
            cp = {k: row[k] for k in ("a_tempo", "richiesti", "scrittura_s", "sync_s",
                                      "buffer_scritti", "reset_statistiche")}
            bg = {k: row[k] for k in ("buffer_bgwriter", "stop_maxwritten", "buffer_backend",
                                      "fsync_backend", "buffer_allocati", "reset_statistiche")}
            if row["fsync_backend"]:
                sec.add(WARN, "I backend hanno dovuto eseguire fsync direttamente (%d): "
                              "possibile saturazione dell'I/O." % row["fsync_backend"])
            total_written = (row["buffer_scritti"] or 0) + (row["buffer_bgwriter"] or 0) + \
                (row["buffer_backend"] or 0)
            if total_written > 100000 and row["buffer_backend"] / total_written > 0.3:
                sec.add(WARN, "Oltre il 30% dei buffer è scritto dai backend: valutare "
                              "bgwriter_lru_maxpages / shared_buffers.")
        total = (cp["a_tempo"] or 0) + (cp["richiesti"] or 0)
        if total >= 10:
            req_pct = 100.0 * cp["richiesti"] / total
            if req_pct > 50:
                sec.add(CRIT, "%.0f%% dei checkpoint è forzato dal volume di WAL: "
                              "aumentare max_wal_size." % req_pct)
            elif req_pct > 20:
                sec.add(WARN, "%.0f%% dei checkpoint è forzato dal volume di WAL: "
                              "valutare max_wal_size." % req_pct)
        if (bg["stop_maxwritten"] or 0) > 1000:
            sec.add(INFO, "Il bgwriter si è fermato %d volte per bgwriter_lru_maxpages." %
                    bg["stop_maxwritten"])
        if not sec.findings:
            sec.add(OK, "Checkpoint nella norma (%d totali)." % total)
        sec.table("Checkpoint", [cp])
        sec.table("Background writer", [bg])
        self.sections.append(sec)

    def check_wal(self):
        sec = Section("wal", "WAL", "Volume di WAL generato (pg_stat_wal, da PostgreSQL 14).")
        if self.ver < 140000:
            sec.add(NA, "pg_stat_wal non disponibile prima della versione 14.")
            sec.table("Posizione WAL", self.query(
                "SELECT pg_current_wal_lsn()::text AS lsn_corrente" if not self.in_recovery else
                "SELECT pg_last_wal_replay_lsn()::text AS lsn_replay"))
            self.sections.append(sec)
            return
        row = self.query("""
            SELECT wal_records AS record, wal_fpi AS full_page_images,
                   pg_size_pretty(wal_bytes) AS volume, wal_buffers_full AS wal_buffers_pieni,
                   stats_reset::timestamp(0) AS reset_statistiche,
                   round(wal_bytes / greatest(extract(epoch FROM now() -
                         coalesce(stats_reset, pg_postmaster_start_time())), 1)) AS byte_al_secondo
            FROM pg_stat_wal
        """)[0]
        row["volume_al_secondo"] = self.scalar("SELECT pg_size_pretty(%s::bigint)",
                                               (row.pop("byte_al_secondo"),))
        if row["record"] and row["full_page_images"] / max(row["record"], 1) > 0.3:
            sec.add(INFO, "Molte full page images: checkpoint frequenti aumentano il volume di WAL.")
        if (row["wal_buffers_pieni"] or 0) > 1000:
            sec.add(WARN, "wal_buffers si è riempito %d volte: valutare di aumentarlo." %
                    row["wal_buffers_pieni"])
        if not sec.findings:
            sec.add(OK, "Generati %s di WAL (%s/s in media)." % (row["volume"], row["volume_al_secondo"]))
        sec.table("Statistiche WAL", [row])
        self.sections.append(sec)

    def check_io(self):
        sec = Section("io", "I/O per tipo di processo",
                      "Letture, scritture ed eviction da pg_stat_io (da PostgreSQL 16).")
        if self.ver < 160000:
            sec.add(NA, "pg_stat_io non disponibile prima della versione 16.")
            self.sections.append(sec)
            return
        rows = self.query("""
            SELECT backend_type AS processo, object AS oggetto, context AS contesto,
                   coalesce(reads, 0) AS letture, coalesce(writes, 0) AS scritture,
                   coalesce(extends, 0) AS estensioni, coalesce(hits, 0) AS hit,
                   coalesce(evictions, 0) AS eviction, coalesce(fsyncs, 0) AS fsync
            FROM pg_stat_io
            WHERE coalesce(reads, 0) + coalesce(writes, 0) + coalesce(hits, 0) > 0
            ORDER BY coalesce(reads, 0) + coalesce(writes, 0) DESC LIMIT %s
        """, (self.top_n,))
        client_writes = sum(r["scritture"] for r in rows
                            if r["processo"] == "client backend" and r["contesto"] == "normal")
        all_writes = sum(r["scritture"] for r in rows if r["contesto"] == "normal")
        if all_writes > 100000 and client_writes / all_writes > 0.3:
            sec.add(WARN, "I backend client eseguono oltre il 30% delle scritture: "
                          "shared_buffers o bgwriter sottodimensionati.")
        else:
            sec.add(OK, "Distribuzione dell'I/O nella norma.")
        sec.table("pg_stat_io", rows)
        self.sections.append(sec)

    def check_statements(self):
        sec = Section("statements", "Query più costose (pg_stat_statements)",
                      "Top query per tempo totale di esecuzione.")
        schema = self.scalar("""
            SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
            WHERE e.extname = 'pg_stat_statements'
        """)
        if not schema:
            sec.add(NA, "Estensione pg_stat_statements non installata nel database corrente "
                        "(CREATE EXTENSION pg_stat_statements; richiede shared_preload_libraries).")
            self.sections.append(sec)
            return
        # PG13 rinomina total_time/mean_time in total_exec_time/mean_exec_time
        total, mean = ("total_exec_time", "mean_exec_time") if self.ver >= 130000 \
            else ("total_time", "mean_time")
        rel = '"%s".pg_stat_statements' % schema.replace('"', '""')
        rows = self.query("""
            SELECT s.queryid::text AS queryid, r.rolname AS utente, s.calls AS chiamate,
                   round(s.{total}::numeric / 1000, 1) AS totale_s,
                   round(s.{mean}::numeric, 2) AS media_ms,
                   round((100.0 * s.{total} / nullif(sum(s.{total}) OVER (), 0))::numeric, 1) AS pct_tempo,
                   s.rows AS righe,
                   round(100.0 * s.shared_blks_hit / nullif(s.shared_blks_hit + s.shared_blks_read, 0), 1)
                       AS cache_hit_pct,
                   s.temp_blks_written AS blocchi_temp,
                   left(regexp_replace(s.query, '\\s+', ' ', 'g'), 300) AS query
            FROM {rel} s LEFT JOIN pg_roles r ON r.oid = s.userid
            ORDER BY s.{total} DESC LIMIT %s
        """.format(total=total, mean=mean, rel=rel), (self.top_n,))
        if not rows:
            sec.add(INFO, "Nessuna statistica raccolta ancora.")
        else:
            sec.add(INFO, "La query più pesante occupa il %s%% del tempo totale." % rows[0]["pct_tempo"])
            slow = [r for r in rows if (r["media_ms"] or 0) > 1000 and r["chiamate"] > 10]
            if slow:
                sec.add(WARN, "%d query frequenti hanno tempo medio > 1 s." % len(slow))
        sec.table("Top query per tempo totale", rows)
        sec.table("Top query per tempo medio (almeno 10 chiamate)", self.query("""
            SELECT s.queryid::text AS queryid, s.calls AS chiamate,
                   round(s.{mean}::numeric, 2) AS media_ms,
                   round(s.{total}::numeric / 1000, 1) AS totale_s,
                   left(regexp_replace(s.query, '\\s+', ' ', 'g'), 300) AS query
            FROM {rel} s WHERE s.calls >= 10
            ORDER BY s.{mean} DESC LIMIT %s
        """.format(total=total, mean=mean, rel=rel), (self.top_n,)))
        self.sections.append(sec)

    def check_vacuum(self):
        sec = Section("vacuum", "Vacuum e tuple morte",
                      "Tabelle con molte tuple morte o mai analizzate (database corrente).")
        rows = self.query("""
            SELECT s.schemaname || '.' || s.relname AS tabella, n_live_tup AS tuple_vive,
                   n_dead_tup AS tuple_morte,
                   round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) AS pct_morte,
                   greatest(last_vacuum, last_autovacuum)::timestamp(0) AS ultimo_vacuum,
                   greatest(last_analyze, last_autoanalyze)::timestamp(0) AS ultimo_analyze,
                   autovacuum_count AS n_autovacuum,
                   pg_size_pretty(@TOTAL_BYTES@) AS dimensione
            FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
            WHERE n_dead_tup > 1000
            ORDER BY n_dead_tup DESC LIMIT %s
        """, (self.top_n,))
        bad = [r for r in rows if (r["pct_morte"] or 0) > 20 and r["tuple_morte"] > 10000]
        if bad:
            sec.add(WARN, "%d tabelle con oltre il 20%% di tuple morte: autovacuum non tiene il "
                          "passo (valutare scale_factor per tabella)." % len(bad))
        never = self.query("""
            SELECT schemaname || '.' || relname AS tabella, n_live_tup AS tuple_vive,
                   n_mod_since_analyze AS modifiche_da_analyze
            FROM pg_stat_user_tables
            WHERE coalesce(last_analyze, last_autoanalyze) IS NULL AND n_live_tup > 10000
            ORDER BY n_live_tup DESC LIMIT %s
        """, (self.top_n,))
        if never:
            sec.add(WARN, "%d tabelle grandi senza statistiche (mai analizzate)." % len(never))
        if not sec.findings:
            sec.add(OK, "Nessuna tabella con bloat significativo da tuple morte.")
        sec.table("Tabelle con più tuple morte", rows)
        sec.table("Tabelle mai analizzate", never)
        self.sections.append(sec)

    def check_seq_scans(self):
        sec = Section("seqscan", "Sequential scan",
                      "Tabelle grandi lette spesso per intero: possibili indici mancanti.")
        rows = self.query("""
            SELECT s.schemaname || '.' || s.relname AS tabella, seq_scan, seq_tup_read AS tuple_lette_seq,
                   coalesce(idx_scan, 0) AS idx_scan,
                   round(100.0 * seq_scan / nullif(seq_scan + coalesce(idx_scan, 0), 0), 1) AS pct_seq,
                   n_live_tup AS tuple_vive,
                   pg_size_pretty(c.relpages * @BLK@) AS dimensione
            FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
            WHERE c.relpages * @BLK@ > 10 * 1024 * 1024 AND seq_scan > 0
            ORDER BY seq_tup_read DESC LIMIT %s
        """, (self.top_n,))
        heavy = [r for r in rows if (r["pct_seq"] or 0) > 50 and r["seq_scan"] > 100]
        if heavy:
            sec.add(WARN, "%d tabelle sopra i 10MB lette prevalentemente in sequential scan." % len(heavy))
        else:
            sec.add(OK, "Nessuna tabella grande dominata da sequential scan.")
        sec.table("Tabelle per tuple lette in sequential scan", rows)
        self.sections.append(sec)

    def check_indexes(self):
        sec = Section("indexes", "Indici", "Indici non utilizzati, non validi e duplicati.")
        unused = self.query("""
            SELECT s.schemaname || '.' || s.relname AS tabella, s.indexrelname AS indice,
                   pg_size_pretty(ic.relpages * @BLK@) AS dimensione, s.idx_scan
            FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid
            JOIN pg_class ic ON ic.oid = s.indexrelid
            WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
              AND ic.relpages * @BLK@ > 1024 * 1024
            ORDER BY ic.relpages DESC LIMIT %s
        """, (self.top_n,))
        invalid = self.query("""
            SELECT i.indrelid::regclass::text AS tabella, i.indexrelid::regclass::text AS indice,
                   pg_size_pretty(ic.relpages * @BLK@) AS dimensione
            FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid WHERE NOT i.indisvalid
        """)
        duplicate = self.query("""
            SELECT indrelid::regclass::text AS tabella,
                   string_agg(indexrelid::regclass::text, ', ') AS indici,
                   pg_size_pretty(sum(ic.relpages)::bigint * @BLK@) AS dimensione_totale
            FROM pg_index JOIN pg_class ic ON ic.oid = indexrelid
            GROUP BY indrelid, indkey::text, indclass::text, coalesce(indexprs::text, ''),
                     coalesce(indpred::text, '')
            HAVING count(*) > 1
        """)
        if invalid:
            sec.add(WARN, "%d indici non validi (es. CREATE INDEX CONCURRENTLY fallito): "
                          "ricrearli o eliminarli." % len(invalid))
        if duplicate:
            sec.add(WARN, "%d gruppi di indici duplicati." % len(duplicate))
        if unused:
            sec.add(INFO, "%d indici (> 1MB) mai usati dall'ultimo reset delle statistiche." % len(unused))
        if not sec.findings:
            sec.add(OK, "Nessun indice inutilizzato, duplicato o non valido.")
        sec.table("Indici non utilizzati", unused)
        sec.table("Indici non validi", invalid)
        sec.table("Indici duplicati", duplicate)
        index_hit = self.scalar("""
            SELECT round(100.0 * sum(idx_blks_hit) / nullif(sum(idx_blks_hit + idx_blks_read), 0), 2)
            FROM pg_statio_user_indexes
        """)
        if index_hit is not None:
            sec.add(INFO, "Cache hit ratio degli indici: %s%%." % index_hit)
        self.sections.append(sec)

    def check_table_sizes(self):
        sec = Section("sizes", "Tabelle più grandi", "Dimensione totale (dati + indici + TOAST), stimata da pg_class.")
        rows = self.query("""
            SELECT n.nspname || '.' || c.relname AS tabella,
                   pg_size_pretty(@TOTAL_BYTES@) AS totale,
                   pg_size_pretty(c.relpages * @BLK@) AS dati,
                   c.reltuples::bigint AS righe_stimate
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'm', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%%'
            ORDER BY @TOTAL_BYTES@ DESC LIMIT %s
        """, (self.top_n,))
        sec.add(INFO, "Le %d tabelle più grandi del database corrente." % len(rows))
        sec.table("Tabelle", rows)
        self.sections.append(sec)

    def check_replication(self):
        sec = Section("replication", "Replica e slot", "Stato delle repliche e degli slot di replica.")
        if self.in_recovery:
            row = self.query("""
                SELECT pg_last_wal_receive_lsn()::text AS lsn_ricevuto,
                       pg_last_wal_replay_lsn()::text AS lsn_applicato,
                       date_trunc('second', now() - pg_last_xact_replay_timestamp())::text AS ritardo,
                       extract(epoch FROM now() - pg_last_xact_replay_timestamp())::int AS ritardo_s
            """)[0]
            delay = row.pop("ritardo_s") or 0
            if delay > 300:
                sec.add(WARN, "Lo standby è indietro di %s (attenzione: su un primario poco "
                              "attivo il ritardo cresce anche senza problemi)." % row["ritardo"])
            else:
                sec.add(OK, "Standby allineato (ritardo %s)." % row["ritardo"])
            sec.table("Stato standby", [row])
            sec.table("WAL receiver", self.query("""
                SELECT status AS stato, sender_host AS primario, slot_name AS slot,
                       last_msg_receipt_time::timestamp(0) AS ultimo_messaggio
                FROM pg_stat_wal_receiver
            """))
        else:
            reps = self.query("""
                SELECT application_name AS nome, client_addr::text AS client, state AS stato,
                       sync_state AS sincronia, write_lag::text AS ritardo_write,
                       flush_lag::text AS ritardo_flush, replay_lag::text AS ritardo_replay,
                       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS ritardo_bytes,
                       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_b
                FROM pg_stat_replication
            """)
            for r in reps:
                if (r.pop("lag_b") or 0) > 1024 ** 3:
                    sec.add(WARN, "La replica %s è indietro di %s." % (r["nome"], r["ritardo_bytes"]))
            sec.add(INFO, "%d repliche collegate." % len(reps))
            sec.table("Repliche", reps)
        wal_status = ", wal_status AS stato_wal" if self.ver >= 130000 else ""
        lsn_fn = "pg_last_wal_receive_lsn()" if self.in_recovery else "pg_current_wal_lsn()"
        slots = self.query("""
            SELECT slot_name AS slot, slot_type AS tipo, database, active AS attivo,
                   pg_size_pretty(pg_wal_lsn_diff(%s, restart_lsn)) AS wal_trattenuto,
                   pg_wal_lsn_diff(%s, restart_lsn) AS ret_b %s
            FROM pg_replication_slots
        """ % (lsn_fn, lsn_fn, wal_status))
        for s in slots:
            retained = s.pop("ret_b") or 0
            if not s["attivo"]:
                sec.add(CRIT if retained > 10 * 1024 ** 3 else WARN,
                        "Lo slot %s non è attivo e trattiene %s di WAL." % (s["slot"], s["wal_trattenuto"]))
            if s.get("stato_wal") == "lost":
                sec.add(CRIT, "Lo slot %s ha perso WAL necessari (wal_status = lost)." % s["slot"])
        sec.table("Slot di replica", slots)
        self.sections.append(sec)

    def to_dict(self):
        return {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "server_version_num": self.ver,
            "kpi": self.kpi,
            "status": worst(*[s.status for s in self.sections]),
            "sections": [s.to_dict() for s in self.sections],
        }


# --- report HTML ----------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--muted:#5d6675;--line:#e2e5ea;
--ok:#1f8a4c;--okbg:#e5f4ea;--warn:#a86400;--warnbg:#fdf1dc;--crit:#c0352b;--critbg:#fbe4e2;
--info:#2f5fb3;--infobg:#e6eefb;--na:#6b7280;--nabg:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#9aa3b2;
--line:#2c3340;--ok:#5cc98a;--okbg:#173524;--warn:#f0b454;--warnbg:#3a2c12;--crit:#f27c72;
--critbg:#3d1d1a;--info:#7fa8f0;--infobg:#1b2a44;--na:#a0a7b4;--nabg:#262b34}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0}h3{font-size:13px;margin:16px 0 6px;
color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin-bottom:20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.tile .l{color:var(--muted);font-size:12px}.tile .v{font-size:22px;font-weight:600;margin-top:2px}
.tile .d{color:var(--muted);font-size:12px}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%}
.nav{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:20px}
.nav a{text-decoration:none;color:var(--fg);background:var(--card);border:1px solid var(--line);
border-radius:999px;padding:3px 10px;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.desc{color:var(--muted);margin:4px 0 8px}
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}
.s-ok{color:var(--ok);background:var(--okbg)}.s-warn{color:var(--warn);background:var(--warnbg)}
.s-crit{color:var(--crit);background:var(--critbg)}.s-info{color:var(--info);background:var(--infobg)}
.s-na{color:var(--na);background:var(--nabg)}
.dot{background:none}
ul.f{list-style:none;padding:0;margin:8px 0}ul.f li{display:flex;gap:8px;align-items:baseline;margin:4px 0}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--bg);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:0}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.q{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;min-width:280px}
.empty{color:var(--muted);font-style:italic}
footer{color:var(--muted);font-size:12px;margin-top:24px}
"""


def esc(v):
    if v is None:
        return "<span class='empty'>-</span>"
    if isinstance(v, bool):
        return "sì" if v else "no"
    if isinstance(v, float):
        v = round(v, 2)
    return html.escape(str(v))


def badge(status):
    return "<span class='badge s-%s'>%s</span>" % (status, STATUS_LABEL[status])


def tile(label, value, detail="", pct=None, status=None):
    bar = ""
    if pct is not None:
        color = {OK: "var(--ok)", WARN: "var(--warn)", CRIT: "var(--crit)"}.get(status, "var(--info)")
        bar = "<div class='bar'><i style='width:%.1f%%;background:%s'></i></div>" % (
            max(0, min(pct, 100)), color)
    return ("<div class='tile'><div class='l'>%s</div><div class='v'>%s</div>"
            "<div class='d'>%s</div>%s</div>") % (esc(label), esc(value), esc(detail), bar)


def render_table(title, columns, rows):
    out = ["<h3>%s</h3>" % esc(title)]
    if not rows:
        out.append("<div class='empty'>Nessun dato.</div>")
        return "".join(out)
    out.append("<div class='tw'><table><thead><tr>")
    out.extend("<th>%s</th>" % esc(c) for c in columns)
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for c in columns:
            v = r.get(c)
            cls = "q" if c in ("query", "ultima_query", "versione") else \
                "n" if isinstance(v, (int, float)) and not isinstance(v, bool) else ""
            out.append("<td class='%s'>%s</td>" % (cls, esc(v)))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_html(checker):
    data = checker.to_dict()
    k = data["kpi"]
    counts = {s: sum(1 for sec in checker.sections if sec.status == s) for s in (CRIT, WARN)}
    tiles = [tile("Stato generale", STATUS_LABEL[data["status"]],
                  "critici: %d · attenzione: %d" % (counts[CRIT], counts[WARN]))]
    tiles.append(tile("Versione", k.get("version", "-"), k.get("role", "")))
    tiles.append(tile("Uptime", k.get("uptime", "-"), "database: %s" % k.get("database", "-")))
    if "connections" in k:
        used, mx, pct = k["connections"]
        st = CRIT if pct >= 90 else WARN if pct >= 75 else OK
        tiles.append(tile("Connessioni", "%d / %d" % (used, mx), "%.0f%% in uso" % pct, pct, st))
    if k.get("cache_hit") is not None:
        h = k["cache_hit"]
        st = CRIT if h < 95 else WARN if h < 99 else OK
        tiles.append(tile("Cache hit ratio", "%.2f%%" % h, "tutti i database", h, st))
    if "xid_pct" in k:
        x = k["xid_pct"]
        st = CRIT if x >= 75 else WARN if x >= 50 else OK
        tiles.append(tile("Età XID massima", "%.1f%%" % x, "del limite di wraparound", x, st))
    if "blocked" in k:
        tiles.append(tile("Sessioni bloccate", k["blocked"], "in attesa di lock"))

    body = []
    for sec in checker.sections:
        body.append("<section class='card' id='%s'><div class='head'><h2>%s</h2>%s</div>" % (
            sec.key, esc(sec.title), badge(sec.status)))
        if sec.description:
            body.append("<div class='desc'>%s</div>" % esc(sec.description))
        body.append("<ul class='f'>")
        for st, text in sec.findings:
            body.append("<li>%s<span>%s</span></li>" % (badge(st), esc(text)))
        body.append("</ul>")
        for t in sec.tables:
            body.append(render_table(*t))
        body.append("</section>")
    nav = "".join("<a href='#%s'>%s%s</a>" % (
        s.key, "<span class='dot s-%s'>●</span> " % s.status if s.status in (WARN, CRIT) else "",
        esc(s.title)) for s in checker.sections)
    return """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Check performance PostgreSQL</title><style>%s</style></head><body><div class="wrap">
<h1>Check performance PostgreSQL</h1>
<div class="sub">Generato il %s · server_version_num %s · statistiche per tabella/indice riferite al database <b>%s</b></div>
<div class="tiles">%s</div><nav class="nav">%s</nav>%s
<footer>Generato da pg_perf_check.py. Le statistiche cumulative partono dall'ultimo reset (pg_stat_reset) o riavvio.</footer>
</div></body></html>""" % (CSS, data["generated_at"], data["server_version_num"],
                          esc(k.get("database", "")), "".join(tiles), nav, "".join(body))


def main():
    p = argparse.ArgumentParser(description="Check performance PostgreSQL 11-18 con report HTML.")
    p.add_argument("--dsn", default="", help="stringa di connessione libpq o URI")
    p.add_argument("-H", "--host")
    p.add_argument("-p", "--port")
    p.add_argument("-U", "--user")
    p.add_argument("-d", "--dbname")
    p.add_argument("-o", "--output", default="pg_perf_report.html", help="file HTML di output")
    p.add_argument("--json", help="salva anche i risultati in JSON")
    p.add_argument("--top", type=int, default=15, help="righe per le classifiche (default 15)")
    args = p.parse_args()

    kwargs = {k: v for k, v in (("host", args.host), ("port", args.port), ("user", args.user),
                                ("dbname", args.dbname)) if v}
    conn = psycopg2.connect(args.dsn, application_name="pg_perf_check", **kwargs)
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as cur:
        # Evita che il check resti in attesa dietro a lock di DDL o query lunghe
        cur.execute("SET statement_timeout = '30s'")
        cur.execute("SET lock_timeout = '3s'")
    checker = Checker(conn, top_n=args.top).run()
    conn.close()

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(render_html(checker))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(checker.to_dict(), f, indent=2, default=str, ensure_ascii=False)
    status = checker.to_dict()["status"]
    print("Report scritto in %s - stato: %s" % (args.output, STATUS_LABEL[status]))
    sys.exit(2 if status == CRIT else 1 if status == WARN else 0)


if __name__ == "__main__":
    main()
