#!/usr/bin/env bash
# =====================================================================
# 03_installa_sandbox.sh - Fase 3: installa lo stack sul server RHEL 8
#
# Da eseguire come root dentro la cartella del bundle estratto:
#   tar xf bundle-ai-locale-AAAAMMGG.tar && cd bundle-ai-locale-AAAAMMGG
#   sudo bash 03_installa_sandbox.sh
#
# Prerequisiti dai repository RHEL aziendali (Satellite/mirror):
#   dnf install python3.12 python3.12-devel gcc libpq-devel zstd
#   (podman solo se si usa l'immagine container)
# Cosa fa:
#   * installa Ollama come servizio systemd in ascolto solo su 127.0.0.1
#   * copia i modelli in /var/lib/ollama/models
#   * crea /opt/ai-locale/venv con pg-airman-mcp e ollmcp (senza Internet)
#   * prepara /etc/ai-locale/ (DSN del DB) e la configurazione di ollmcp
# =====================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Eseguire come root"; exit 1; }
BUNDLE="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/ai-locale
CONTEXT="${OLLAMA_CONTEXT_LENGTH:-16384}"

echo ">> Controlli preliminari"
grep -qw avx2 /proc/cpuinfo || echo "ATTENZIONE: CPU senza AVX2, prestazioni molto ridotte"
command -v python3.12 >/dev/null || { echo "Manca python3.12: dnf install python3.12 python3.12-devel"; exit 1; }
rpm -q gcc libpq-devel python3.12-devel >/dev/null || { echo "Mancano gcc/libpq-devel/python3.12-devel"; exit 1; }

echo ">> Ollama"
if [ -f "$BUNDLE/ollama/ollama-linux-amd64.tar.zst" ]; then
  zstd -dc "$BUNDLE/ollama/ollama-linux-amd64.tar.zst" | tar x -C /usr
else
  tar xzf "$BUNDLE/ollama/ollama-linux-amd64.tgz" -C /usr
fi
/usr/bin/ollama --version || true
id ollama >/dev/null 2>&1 || useradd -r -s /sbin/nologin -U -m -d /var/lib/ollama ollama
mkdir -p /var/lib/ollama/models
cp -a "$BUNDLE/modelli/." /var/lib/ollama/models/
chown -R ollama:ollama /var/lib/ollama
# Accesso alla GPU NVIDIA (se presente)
getent group video >/dev/null && usermod -aG video ollama || true

cat > /etc/systemd/system/ollama.service <<EOF
[Unit]
Description=Ollama (modelli AI locali)
After=network-online.target

[Service]
User=ollama
Group=ollama
ExecStart=/usr/bin/ollama serve
Restart=always
RestartSec=3
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MODELS=/var/lib/ollama/models"
Environment="OLLAMA_CONTEXT_LENGTH=${CONTEXT}"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
# Non cancellare i blob dei modelli all'avvio (copiati a mano dal bundle)
Environment="OLLAMA_NOPRUNE=1"
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ollama
sleep 3
ollama list

echo ">> pg-airman-mcp e ollmcp (venv offline)"
python3.12 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --no-index --find-links "$BUNDLE/wheels" --upgrade pip setuptools wheel
"$PREFIX/venv/bin/pip" install --no-index --find-links "$BUNDLE/wheels" \
  -r "$BUNDLE/config/requirements.txt" -c "$BUNDLE/config/constraints.txt"
"$PREFIX/venv/bin/pg-airman-mcp" --help >/dev/null && echo "pg-airman-mcp OK"
ln -sf "$PREFIX/venv/bin/ollmcp" /usr/local/bin/ollmcp
cp "$BUNDLE/collaudo.py" "$BUNDLE/domande_collaudo.json" "$PREFIX/" 2>/dev/null || true

echo ">> Wrapper e configurazione"
mkdir -p /etc/ai-locale
if [ ! -f /etc/ai-locale/db.env ]; then
  cat > /etc/ai-locale/db.env <<'EOF'
# DSN dell'utenza read-only (vedi 04_utente_readonly.sql). Leggibile solo dal gruppo ai-locale.
AIRMAN_MCP_DATABASE_URL=postgresql://ai_readonly:CAMBIAMI@dbtest.example:5432/testdb?sslmode=require
AIRMAN_MCP_ACCESS_MODE=restricted
ALLOW_COMMENT_IN_RESTRICTED=false
ALLOW_ALIAS_WRITE_IN_RESTRICTED=false
EOF
fi
getent group ai-locale >/dev/null || groupadd ai-locale
chown root:ai-locale /etc/ai-locale/db.env && chmod 640 /etc/ai-locale/db.env

# Wrapper: carica il DSN, forza la modalita' restricted, impedisce chiamate cloud
cat > "$PREFIX/pg-airman-mcp-ro" <<EOF
#!/usr/bin/env bash
set -a; . /etc/ai-locale/db.env; set +a
unset OPENAI_API_KEY
PURPOSE="ai-\$(printf %s "\${USER:-dba}" | tr A-Z a-z | tr -c a-z0-9- -)"
exec $PREFIX/venv/bin/pg-airman-mcp --access-mode=restricted --purpose="\$PURPOSE" "\$@"
EOF
chmod 750 "$PREFIX/pg-airman-mcp-ro" && chgrp ai-locale "$PREFIX/pg-airman-mcp-ro"

cat > /etc/ai-locale/servers.json <<EOF
{
  "mcpServers": {
    "postgres": {
      "command": "$PREFIX/pg-airman-mcp-ro",
      "args": [],
      "disabled": false
    }
  }
}
EOF
chmod 644 /etc/ai-locale/servers.json

cat > /usr/local/bin/ai-dba <<'EOF'
#!/usr/bin/env bash
# Avvia la chat con il modello locale e i tool di pg-airman-mcp
exec ollmcp --servers-json /etc/ai-locale/servers.json --model "${AI_MODEL:-qwen3:14b}" "$@"
EOF
chmod 755 /usr/local/bin/ai-dba

cat <<EOF

Installazione completata.
 1. Modificare /etc/ai-locale/db.env con host e password dell'utenza ai_readonly.
 2. Aggiungere gli utenti abilitati al gruppo: usermod -aG ai-locale <utente>
 3. Da SSH:   ai-dba            (AI_MODEL=qwen3:8b ai-dba per un altro modello)
 4. Collaudo: cd $PREFIX && set -a && . /etc/ai-locale/db.env && set +a && \\
              venv/bin/python collaudo.py --model qwen3:14b --out risultati/
EOF
