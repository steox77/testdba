#!/usr/bin/env bash
# =====================================================================
# 02_prepara_bundle.sh - prepara il pacchetto per l'installazione OFFLINE
#
# Da eseguire su una macchina Linux x86-64 CON accesso Internet
# (es. WSL OracleLinux, una VM di appoggio) che abbia:
#   curl, python3.12 (+ pip), e Ollama installato solo per scaricare i modelli.
# Produce bundle-ai-locale-<data>.tar da copiare sul server RHEL 8 (scp).
#
#   MODELLI="qwen3:14b qwen3:8b" OLLAMA_VERSION=0.12.6 bash 02_prepara_bundle.sh
# =====================================================================
set -euo pipefail

MODELLI="${MODELLI:-qwen3:14b}"
OLLAMA_VERSION="${OLLAMA_VERSION:?Impostare OLLAMA_VERSION, es. la stessa della soluzione esistente}"
PY="${PY:-python3.12}"
CON_IMMAGINE="${CON_IMMAGINE:-0}"   # 1 = salva anche l'immagine container di pg-airman-mcp
DEST="bundle-ai-locale-$(date +%Y%m%d)"

mkdir -p "$DEST"/{ollama,modelli,wheels,config}
cd "$DEST"

echo ">> Ollama $OLLAMA_VERSION"
base="https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}"
# Le release piu' recenti usano .tar.zst, le precedenti .tgz: prova entrambe.
curl -fL -o ollama/ollama-linux-amd64.tar.zst "$base/ollama-linux-amd64.tar.zst" \
  || { rm -f ollama/ollama-linux-amd64.tar.zst; curl -fL -o ollama/ollama-linux-amd64.tgz "$base/ollama-linux-amd64.tgz"; }
# GPU NVIDIA: le librerie CUDA sono gia' nel pacchetto amd64. Per AMD scaricare anche ollama-linux-amd64-rocm.

echo ">> Modelli: $MODELLI"
# Store temporaneo, cosi' il bundle contiene solo i modelli richiesti
export OLLAMA_MODELS="$PWD/modelli"
OLLAMA_HOST=127.0.0.1:11499 ollama serve >/tmp/ollama-bundle.log 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 3
for m in $MODELLI; do OLLAMA_HOST=127.0.0.1:11499 ollama pull "$m"; done
OLLAMA_HOST=127.0.0.1:11499 ollama list | tee config/modelli.txt
kill $SRV; trap - EXIT

echo ">> Pacchetti Python (pg-airman-mcp, ollmcp) per Python 3.12"
cat > config/constraints.txt <<'EOF'
# pg-airman-mcp 1.1.1 non e' compatibile con mcp 2.x (FastMCP rimosso): bloccare a 1.x
mcp<2
EOF
# pg-airman-mcp 1.1.1 dichiara pglast==7.2.0, che non e' piu' su PyPI: lo si installa
# senza dipendenze e le sue dipendenze si elencano qui, con pglast 7.21 (verificato).
# python-dotenv e' importato da pg-airman-mcp ma non dichiarato tra le dipendenze.
cat > config/requirements.txt <<'EOF'
attrs>=25.3.0
humanize>=4.8.0
instructor>=1.7.9
mcp>=1.9.2
pglast==7.21
psycopg-pool>=3.2.6
psycopg[c]>=3.2.8
pyjwt[crypto]>=2.12.1
python-dotenv
ollmcp
EOF
"$PY" -m pip download -d wheels -r config/requirements.txt -c config/constraints.txt pip setuptools wheel
"$PY" -m pip download -d wheels --no-deps pg-airman-mcp==1.1.1
# psycopg-c arriva come sorgente: verra' compilato sul server (gcc, libpq-devel, python3.12-devel).
ls wheels | tee config/wheels.txt

if [ "$CON_IMMAGINE" = 1 ]; then
  echo ">> Immagine container pg-airman-mcp (alternativa a pip)"
  podman pull docker.io/enterprisedb/pg-airman-mcp:latest
  podman save -o pg-airman-mcp-image.tar docker.io/enterprisedb/pg-airman-mcp:latest
fi

cp ../03_installa_sandbox.sh ../04_utente_readonly.sql ../collaudo.py ../domande_collaudo.json . 2>/dev/null || true
cd ..
tar cf "$DEST.tar" "$DEST"
sha256sum "$DEST.tar" | tee "$DEST.tar.sha256"
echo "Pronto: $DEST.tar ($(du -h "$DEST.tar" | cut -f1)). Copiare sul server con scp e verificare lo sha256."
