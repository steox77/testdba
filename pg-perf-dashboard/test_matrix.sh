#!/usr/bin/env bash
# Avvia PostgreSQL 11-18 in Docker, genera carico con pgbench e lancia il check su ciascuna versione.
# Uso: ./test_matrix.sh [versioni...]   (default: 11 12 13 14 15 16 17 18)
set -euo pipefail
cd "$(dirname "$0")"
VERSIONS=${*:-"11 12 13 14 15 16 17 18"}
export PGPASSWORD=pw PGUSER=postgres PGHOST=127.0.0.1
mkdir -p reports
for v in $VERSIONS; do
  docker rm -f "pgcheck$v" >/dev/null 2>&1 || true
  docker run -d --name "pgcheck$v" -e POSTGRES_PASSWORD=pw -p "54$v:5432" "postgres:$v-alpine" \
    -c shared_preload_libraries=pg_stat_statements >/dev/null
done
for v in $VERSIONS; do
  until pg_isready -q -p "54$v"; do sleep 1; done
  psql -qp "54$v" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" postgres
  pgbench -p "54$v" -i -s 5 -q postgres >/dev/null 2>&1
  pgbench -p "54$v" -c 4 -T 5 postgres >/dev/null 2>&1
  rc=0; python3 pg_perf_check.py -p "54$v" -d postgres -o "reports/pg$v.html" --json "reports/pg$v.json" || rc=$?
  # exit 0/1/2 = OK/attenzione/critico; altri codici = errore dello script
  [ "$rc" -le 2 ] || { echo "pg$v: errore (exit $rc)"; exit 1; }
done
for v in $VERSIONS; do docker rm -f "pgcheck$v" >/dev/null; done
