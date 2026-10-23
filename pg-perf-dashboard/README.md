# pg-perf-dashboard

Check performance per PostgreSQL **dalla 11 alla 18**: uno script Python che si collega
all'istanza, rileva la versione e genera una dashboard HTML autonoma (un solo file,
nessuna risorsa esterna, tema chiaro/scuro automatico) più un JSON opzionale.

## Uso

```bash
pip install -r requirements.txt
python3 pg_perf_check.py -H dbhost -p 5432 -U postgres -d mydb -o report.html
python3 pg_perf_check.py --dsn "postgresql://monitor@dbhost/mydb" --json report.json
```

Valgono anche le variabili libpq (`PGHOST`, `PGUSER`, `PGPASSWORD`, …) e `~/.pgpass`.
Per vedere tutte le sessioni e le statistiche serve un superuser o un utente con il ruolo
`pg_monitor` (`GRANT pg_monitor TO monitor;`).

Lo script è sicuro da lanciare in produzione: la sessione è `read only`, con
`statement_timeout = 30s` e `lock_timeout = 3s`, e le dimensioni delle tabelle sono lette da
`pg_class` per non restare bloccato dietro a DDL o `LOCK TABLE` in corso.

Codice di uscita: `0` tutto OK, `1` almeno un'attenzione, `2` almeno un critico. Comodo per
cron o per un sistema di monitoraggio.

## Cosa controlla

| Sezione | Fonte | Esempi di soglie |
|---|---|---|
| Server | `version()`, `pg_database` | versione fuori supporto |
| Parametri | `pg_settings` | `fsync`/`autovacuum` off, `shared_buffers` di default |
| Connessioni | `pg_stat_activity` | uso > 75% / 90%, idle in transaction > 5 min |
| Query attive e wait event | `pg_stat_activity`, `pg_stat_progress_vacuum` | query > 5 / 30 min |
| Lock | `pg_blocking_pids()` | sessioni bloccate e bloccanti |
| Database | `pg_stat_database` | cache hit < 99% / 95%, rollback, deadlock, file temporanei, checksum |
| Wraparound | `age(datfrozenxid)` | > 50% / 75% del limite XID |
| Checkpoint e bgwriter | `pg_stat_bgwriter`, `pg_stat_checkpointer` (17+) | checkpoint forzati > 20% / 50% |
| WAL | `pg_stat_wal` (14+) | `wal_buffers` pieni |
| I/O | `pg_stat_io` (16+) | scritture fatte dai backend client |
| Query più costose | `pg_stat_statements` | top per tempo totale e medio |
| Vacuum | `pg_stat_user_tables` | tuple morte > 20%, tabelle mai analizzate |
| Sequential scan | `pg_stat_user_tables` | tabelle > 10MB lette soprattutto in seq scan |
| Indici | `pg_stat_user_indexes`, `pg_index` | non usati, non validi, duplicati |
| Tabelle più grandi | `pg_class` | dimensione dati + indici + TOAST |
| Replica e slot | `pg_stat_replication`, `pg_replication_slots` | lag, slot inattivi o `lost` |

Le statistiche di tabelle, indici e `pg_stat_statements` si riferiscono al database a cui ci si
collega: per analizzare un altro database rilanciare lo script con `-d`.

## Differenze tra versioni gestite

- **13**: in `pg_stat_statements` `total_time`/`mean_time` diventano `total_exec_time`/`mean_exec_time`;
  `wal_status` in `pg_replication_slots`.
- **12**: `checksum_failures` in `pg_stat_database`.
- **14**: `pg_stat_wal` e le statistiche di sessione in `pg_stat_database`.
- **16**: `pg_stat_io`.
- **17**: i contatori dei checkpoint passano da `pg_stat_bgwriter` a `pg_stat_checkpointer`;
  `buffers_backend` e `buffers_backend_fsync` spariscono.

Le sezioni non disponibili sulla versione analizzata compaiono come "N/D". Se un check fallisce
(per esempio per permessi), la sezione lo segnala e il resto del report viene generato comunque.

## Test

`./test_matrix.sh` avvia PostgreSQL 11–18 in Docker, genera carico con `pgbench` e produce un
report per versione in `reports/`. Richiede Docker, `psql` e `pgbench`.
