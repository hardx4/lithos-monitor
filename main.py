#!/usr/bin/env python3
"""
Monitor Lithos NISP + novos blocos + block candidates
Checa superSharesInWindow diretamente (em vez de held).
Contagem diária de blocos do pool (reseta às 00:00 UTC) com persistência.
"""

import os
import time
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests

# ==================== CONFIG ====================
LITHOS_BASE = os.getenv("LITHOS_BASE")
LITHOS_API_KEY = os.getenv("LITHOS_API_KEY")

if not LITHOS_BASE:
    raise SystemExit("Defina a variável de ambiente LITHOS_BASE")
if not LITHOS_API_KEY:
    raise SystemExit("Defina a variável de ambiente LITHOS_API_KEY")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
BLOCKS_CRITICAL = int(os.getenv("BLOCKS_CRITICAL", "15"))
NISP_REQUIRED = int(os.getenv("NISP_REQUIRED", "10"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = Path(os.getenv("STATE_FILE", "lithos_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lithos-monitor")

last_state = {
    "nisp_count": None,
    "critical": None,
    "max_block_height": None,
    "block_candidates": None,
    "daily_blocks": 0,
    "current_utc_date": None,
    "initialized": False,
}

# ==================== PERSISTÊNCIA ====================
def load_state():
    global last_state
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        today = get_utc_date()
        if data.get("current_utc_date") == today:
            last_state["daily_blocks"] = data.get("daily_blocks", 0)
            last_state["current_utc_date"] = today
            last_state["max_block_height"] = data.get("max_block_height")
            log.info(f"Estado carregado: {last_state['daily_blocks']} blocos hoje (UTC)")
        else:
            log.info("Arquivo de estado é de outro dia. Contador zerado.")
    except Exception as e:
        log.warning(f"Não foi possível carregar estado: {e}")

def save_state():
    try:
        data = {
            "current_utc_date": last_state["current_utc_date"],
            "daily_blocks": last_state["daily_blocks"],
            "max_block_height": last_state["max_block_height"],
        }
        STATE_FILE.write_text(json.dumps(data))
    except Exception as e:
        log.warning(f"Não foi possível salvar estado: {e}")

# ==================== TELEGRAM ====================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram não configurado, mensagem não enviada")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram erro: {resp.text}")
    except Exception as e:
        log.error(f"Falha ao enviar Telegram: {e}")

# ==================== LITHOS API ====================
def _headers() -> Dict[str, str]:
    headers = {}
    if LITHOS_API_KEY:
        headers["X-Api-Key"] = LITHOS_API_KEY
    return headers

def get_nisp_status() -> Optional[Dict[str, Any]]:
    url = f"{LITHOS_BASE}/stats/mining/workers"
    try:
        resp = requests.get(url, headers=_headers(), timeout=10, verify=False)
        resp.raise_for_status()
        data = resp.json()
        nisp = data.get("nisp")
        if not nisp:
            return None
        return {
            "nisp_count": nisp.get("superSharesInWindow", 0),
            "required": nisp.get("required", NISP_REQUIRED),
            "blocks_remaining": nisp.get("blocksRemaining"),
            "held": nisp.get("held", False),
            "block_candidates": data.get("blockCandidates", 0),
        }
    except Exception as e:
        log.error(f"Erro ao consultar /stats/mining/workers: {e}")
        return None

def get_pool_blocks() -> Optional[List[Dict[str, Any]]]:
    url = f"{LITHOS_BASE}/blocks/all"
    try:
        resp = requests.get(url, headers=_headers(), timeout=10, verify=False)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Erro ao consultar /blocks/all: {e}")
        return None

def get_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ==================== LOOP ====================
def main():
    global last_state

    load_state()

    log.info("Iniciando monitor Lithos NISP + blocos (por quantidade)")
    send_telegram("🟢 <b>Monitor Lithos iniciado</b>\nChecando quantidade de NISP, blocos do pool (diário UTC) e block candidates a cada 60s")

    while True:
        try:
            today_utc = get_utc_date()
            if last_state["current_utc_date"] != today_utc:
                if last_state["current_utc_date"] is not None:
                    log.info(f"Novo dia UTC ({today_utc}). Contador de blocos zerado.")
                    send_telegram("📅 <b>Novo dia UTC</b>\nContador de blocos do pool zerado.")
                last_state["current_utc_date"] = today_utc
                last_state["daily_blocks"] = 0
                save_state()

            nisp = get_nisp_status()
            if not nisp or nisp["blocks_remaining"] is None:
                log.warning("Não foi possível obter status NISP")
                time.sleep(POLL_INTERVAL)
                continue

            nisp_count = nisp["nisp_count"]
            required = nisp["required"]
            blocks = nisp["blocks_remaining"]
            block_candidates = nisp["block_candidates"]
            is_ready = nisp_count >= required
            is_critical = blocks < BLOCKS_CRITICAL

            log.info(
                f"NISP | count={nisp_count}/{required} | ready={is_ready} | "
                f"blocks={blocks} | candidates={block_candidates} | daily={last_state['daily_blocks']}"
            )

            alerts = []

            # Primeira checagem
            if not last_state["initialized"]:
                if not is_ready:
                    alerts.append(
                        f"🔴 <b>NISP ABAIXO DE {required} (início)</b>\n"
                        f"Super shares: {nisp_count}/{required}\n"
                        f"Blocos restantes: {blocks}"
                    )
                if is_critical:
                    alerts.append(
                        f"⚠️ <b>BLOCOS CRÍTICOS (início)</b>\n"
                        f"Blocos restantes: <b>{blocks}</b>\n"
                        f"Super shares: {nisp_count}/{required}"
                    )
                last_state["initialized"] = True
                last_state["nisp_count"] = nisp_count
                last_state["critical"] = is_critical
                last_state["block_candidates"] = block_candidates

            else:
                prev_count = last_state["nisp_count"]
                prev_ready = prev_count is not None and prev_count >= required

                # Caiu abaixo de 10
                if prev_ready and not is_ready:
                    alerts.append(
                        f"🔴 <b>NISP CAIU ABAIXO DE {required}</b>\n"
                        f"Super shares: {nisp_count}/{required}\n"
                        f"Blocos restantes: {blocks}"
                    )

                # Voltou a 10 ou mais
                if not prev_ready and is_ready:
                    alerts.append(
                        f"🟢 <b>NISP RECUPERADO ({nisp_count}/{required})</b>\n"
                        f"Blocos restantes: {blocks}"
                    )

                # Blocos críticos
                if last_state["critical"] is False and is_critical:
                    alerts.append(
                        f"⚠️ <b>BLOCOS CRÍTICOS</b>\n"
                        f"Blocos restantes: <b>{blocks}</b> (threshold: {BLOCKS_CRITICAL})\n"
                        f"Super shares: {nisp_count}/{required}"
                    )

                # Blocos normalizados
                if last_state["critical"] is True and not is_critical:
                    alerts.append(
                        f"✅ <b>Blocos normalizados</b>\n"
                        f"Blocos restantes: {blocks}\n"
                        f"Super shares: {nisp_count}/{required}"
                    )

                # Block candidates
                if last_state["block_candidates"] is not None and block_candidates > last_state["block_candidates"]:
                    delta = block_candidates - last_state["block_candidates"]
                    alerts.append(
                        f"🎯 <b>BLOCK CANDIDATE ENCONTRADO</b>\n"
                        f"Novos candidatos: <b>{delta}</b>\n"
                        f"Total nesta sessão: {block_candidates}"
                    )

            # --- Novos blocos no pool ---
            pool_blocks = get_pool_blocks()
            if pool_blocks:
                heights = [b.get("blockHeight", 0) for b in pool_blocks if b.get("blockHeight")]
                if heights:
                    current_max = max(heights)
                    if last_state["max_block_height"] is not None and current_max > last_state["max_block_height"]:
                        new_count = sum(1 for h in heights if h > last_state["max_block_height"])
                        last_state["daily_blocks"] += new_count
                        save_state()
                        alerts.append(
                            f"⛏️ <b>NOVOS BLOCOS NO POOL</b>\n"
                            f"Nesta checagem: <b>{new_count}</b>\n"
                            f"Total hoje (UTC): <b>{last_state['daily_blocks']}</b>\n"
                            f"Maior height: {current_max}"
                        )
                    last_state["max_block_height"] = current_max
                    save_state()

            # Envia alertas
            for msg in alerts:
                log.info(f"Enviando alerta: {msg[:80]}...")
                send_telegram(msg)

            # Atualiza estado
            last_state["nisp_count"] = nisp_count
            last_state["critical"] = is_critical
            last_state["block_candidates"] = block_candidates

        except Exception as e:
            log.exception(f"Erro no ciclo: {e}")
            send_telegram(f"🚨 <b>Erro no monitor</b>\n<code>{str(e)[:400]}</code>")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
