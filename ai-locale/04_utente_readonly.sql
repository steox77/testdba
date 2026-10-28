-- =====================================================================
-- 04_utente_readonly.sql
-- Utenza dedicata in SOLA LETTURA per pg-airman-mcp (PostgreSQL / EDB)
--
-- Eseguire come superuser (postgres / enterprisedb) collegati al DB di test:
--   psql -h <host> -U postgres -d <db_test> \
--        -v ai_password="'<password_robusta>'" -f 04_utente_readonly.sql
--
-- Requisiti:
--   * PostgreSQL / EPAS 14+ per il ruolo predefinito pg_read_all_data
--     (su versioni precedenti vedere la sezione "Alternativa PG < 14")
--   * pg_stat_statements in shared_preload_libraries (richiede riavvio)
--   * pacchetto hypopg installato a livello di sistema (opzionale ma consigliato)
-- =====================================================================

\set ON_ERROR_STOP on

-- 1. Ruolo di login dedicato ------------------------------------------------
CREATE ROLE ai_readonly WITH LOGIN
    PASSWORD :ai_password
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 5;

-- 2. Lettura dati e viste di monitoraggio (ruoli predefiniti) ---------------
GRANT pg_read_all_data TO ai_readonly;   -- SELECT su tutte le tabelle (PG14+)
GRANT pg_monitor       TO ai_readonly;   -- pg_stat_*, pg_stat_statements, settings

-- Se i dati applicativi sono sensibili, NON concedere pg_read_all_data e
-- concedere SELECT solo sugli schemi da analizzare:
--   GRANT USAGE ON SCHEMA app TO ai_readonly;
--   GRANT SELECT ON ALL TABLES IN SCHEMA app TO ai_readonly;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO ai_readonly;
-- (EXPLAIN, statistiche e health check funzionano anche cosi')

-- 3. Difese a livello di sessione (valgono anche se il client sbaglia) ------
ALTER ROLE ai_readonly SET default_transaction_read_only = on;
ALTER ROLE ai_readonly SET statement_timeout = '30s';
ALTER ROLE ai_readonly SET lock_timeout = '2s';
ALTER ROLE ai_readonly SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE ai_readonly SET work_mem = '16MB';
ALTER ROLE ai_readonly SET log_statement = 'all';   -- audit delle query generate dall'AI

-- 4. Accesso al database e nessun privilegio di scrittura -------------------
SELECT format('GRANT CONNECT ON DATABASE %I TO ai_readonly', current_database()) \gexec
REVOKE CREATE ON SCHEMA public FROM ai_readonly;

-- 5. Estensioni usate da pg-airman-mcp ---------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- hypopg: indici ipotetici in memoria di sessione, nessuna scrittura su disco.
-- Commentare se il pacchetto non e' installato sul server.
CREATE EXTENSION IF NOT EXISTS hypopg;

-- 6. Verifica ---------------------------------------------------------------
SELECT r.rolname,
       r.rolconnlimit,
       r.rolconfig,
       ARRAY(SELECT b.rolname FROM pg_auth_members m
             JOIN pg_roles b ON b.oid = m.roleid
             WHERE m.member = r.oid) AS member_of
FROM pg_roles r WHERE r.rolname = 'ai_readonly';

SELECT extname, extversion FROM pg_extension
WHERE extname IN ('pg_stat_statements', 'hypopg');

-- =====================================================================
-- pg_hba.conf (da aggiungere PRIMA delle righe generiche, poi reload):
--   hostssl  <db_test>  ai_readonly  <IP_sandbox>/32  scram-sha-256
-- e:  SELECT pg_reload_conf();
--
-- Alternativa PG < 14 (nessun pg_read_all_data): per ogni schema
--   GRANT USAGE ON SCHEMA <s> TO ai_readonly;
--   GRANT SELECT ON ALL TABLES IN SCHEMA <s> TO ai_readonly;
--
-- Rimozione completa:
--   REVOKE ALL ON DATABASE <db_test> FROM ai_readonly;
--   DROP OWNED BY ai_readonly; DROP ROLE ai_readonly;
-- =====================================================================
