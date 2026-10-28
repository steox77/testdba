#!/usr/bin/env bash
# =====================================================================
# 01_raccolta_info.sh - Fase 1: fotografa la soluzione AI esistente
#
# Da eseguire (come utente con sudo, o root) sul server dove gira oggi
# Ollama + Qwen + pg-airman-mcp. Non modifica nulla: legge e basta.
#   bash 01_raccolta_info.sh > info_soluzione_$(hostname)_$(date +%F).txt 2>&1
# Rivedere il file prima di inviarlo: le password nei DSN vengono mascherate,
# ma conviene controllare comunque.
# =====================================================================
set -u
sez() { printf '\n===== %s =====\n' "$*"; }
run() { printf '$ %s\n' "$*"; eval "$@" 2>&1 | sed -E 's#(postgres(ql)?://[^:/@]+:)[^@]+@#\1***@#g; s#(PASSWORD|password|PGPASSWORD|API_KEY|TOKEN|SECRET)([=:" ]+)[^ "]+#\1\2***#g'; echo; }

sez "Sistema"
run "cat /etc/redhat-release /etc/os-release 2>/dev/null | head -5"
run "uname -r"
run "lscpu | grep -E 'Model name|^CPU\\(s\\)|Thread|Socket'"
run "grep -owE 'avx2|avx512f' /proc/cpuinfo | sort -u"
run "free -g"
run "df -h / /var /opt /usr/share/ollama /var/lib/ollama 2>/dev/null"
run "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv 2>/dev/null || echo 'nessuna GPU NVIDIA'"

sez "Ollama"
run "command -v ollama && ollama --version"
run "systemctl cat ollama 2>/dev/null"
run "systemctl show ollama -p Environment 2>/dev/null"
run "ollama list"
run "ollama ps"
for m in $(ollama list 2>/dev/null | awk 'NR>1{print $1}'); do
  run "ollama show $m"
  run "ollama show --modelfile $m | grep -vE '^(TEMPLATE|\"\"\"|\\{\\{)' | head -40"
done
run "ss -ltnp | grep -E '11434|8000|8001|8080|3000'"

sez "pg-airman-mcp"
run "command -v pg-airman-mcp; pipx list 2>/dev/null | grep -A3 -i airman; uv tool list 2>/dev/null | grep -A3 -i airman"
run "pip3 show pg-airman-mcp mcp pglast psycopg 2>/dev/null | grep -E '^(Name|Version|Location)'"
run "podman images 2>/dev/null | grep -i airman; docker images 2>/dev/null | grep -i airman"
run "ps -eo pid,user,etime,args | grep -iE 'airman|ollmcp|mcphost|mcpo|open-webui|oterm' | grep -v grep"
run "env | grep -E '^(AIRMAN_|ALLOW_|OLLAMA_)'"
run "systemctl list-units --all --no-pager | grep -iE 'airman|mcp|ollama|webui'"

sez "Client MCP (componente che collega Ollama e pg-airman-mcp)"
for f in ~/.config/ollmcp/* ~/.config/mcphost/* ~/.mcphost.* ~/.mcp.json ~/mcp*.json ~/servers*.json \
         /etc/ollmcp/* /opt/*/servers*.json /opt/*/mcp*.json; do
  [ -f "$f" ] && run "echo '--- $f'; cat '$f'"
done
run "command -v ollmcp mcphost mcpo open-webui oterm 2>/dev/null"

sez "Python e strumenti"
run "python3 --version; ls /usr/bin/python3.* 2>/dev/null"
run "rpm -q python3.12 python3.12-devel libpq libpq-devel gcc podman zstd 2>/dev/null"

sez "Database collegati (dai DSN trovati)"
echo "Indicare a mano: versione PostgreSQL/EDB, estensioni pg_stat_statements e hypopg,"
echo "utenza usata e privilegi (\\du <utente>), regole pg_hba.conf per l'host AI."
