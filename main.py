#!/usr/bin/env python3
"""
Monitor Lithos NISP + novos blocos + block candidates
Contagem diária + resumo às 00:01 UTC.
"""

import os
import time
import logging
import json
from datetime import datetime, timezone, timedelta
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
    "last_summary_date": None,
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
            last_state["last_summary_date"] = data.get("last_summary_date")
            log.info(f"Estado carregado: {last_state['daily_blocks']} blocos hoje (UTC)")
        else:
            log.info("Arquivo de estado é de outro dia. Contador zerado.")
            last_state["last_summary_date"] = data.get("last_summary_date")
    except Exception as e:
        log.warning(f"Não foi possível carregar estado: {e}")

def save_state():
    try:
        data = {
            "current_utc_date": last_state["current_utc_date"],
            "daily_blocks": last_state["daily_blocks"],
            "max_block_height": last_state["max_block_height"],
            "last_summary_date": last_state["last_summary_date"],
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

def get_mining_buckets(from_ms: int, until_ms: int, interval: str = "day") -> Optional[Dict]:
    """GET /stats/mining/buckets"""
    url = f"{LITHOS_BASE}/stats/mining/buckets"
    params = {"from": from_ms, "until": until_ms, "interval": interval}
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=15, verify=False)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Erro ao consultar /stats/mining/buckets: {e}")
        return None

def get_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_utc_midnight(days_ago: int = 0) -> datetime:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days_ago)

def should_send_daily_summary() -> bool:
    """Retorna True se for >= 00:01 UTC e ainda não enviou o resumo do dia anterior."""
    now = datetime.now(timezone.utc)
    if now.hour == 0 and now.minute >= 1:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if last_state["last_summary_date"] != yesterday:
            return True
    return False

def send_daily_summary():
    """Envia resumo do dia anterior usando os buckets."""
    yesterday_midnight = get_utc_midnight(days_ago=1)
    today_midnight = get_utc_midnight(days_ago=0)
    from_ms = int(yesterday_midnight.timestamp() * 1000)
    until_ms = int(today_midnight.timestamp() * 1000)
    date_str = yesterday_midnight.strftime("%Y-%m-%d")

    log.info(f"Gerando resumo diário para {date_str}")

    data = get_mining_buckets(from_ms, until_ms, "day")
    if not data or not data.get("buckets"):
        send_telegram(f"📅 <b>Resumo {date_str}</b>\nNão foi possível obter dados dos buckets.")
        last_state["last_summary_date"] = date_str
        save_state()
        return

    # Soma os totais do dia
    protocol_blocks = 0
    local_participation = 0
    for bucket in data["buckets"]:
        totals = bucket.get("totals", {})
        protocol_blocks += int(totals.get("lithos.blocks", 0) or 0)
        # Participação local aproximada via rollup submissions locais
        local_participation += int(totals.get("rollup.submission.localCount", 0) or 0)

    missed = max(0, protocol_blocks - local_participation)

    msg = (
        f"📊 <b>Resumo diário — {date_str} (UTC)</b>\n\n"
        f"⛏️ Blocos minerados pelo protocolo: <b>{protocol_blocks}</b>\n"
        f"🎯 Blocos em que o pool participou: <b>{local_participation}</b>\n"
        f"❌ Blocos em que ficou de fora: <b>{missed}</b>\n\n"
        f"Contador interno do script (blocos detectados): {last_state['daily_blocks']}"
    )
    send_telegram(msg)
    log.info(f"Resumo enviado: protocolo={protocol_blocks}, local={local_participation}")

    last_state["last_summary_date"] = date_str
    save_state()

# ==================== LOOP ====================
def main():
    global last_state

    load_state()

    log.info("Iniciando monitor Lithos NISP + blocos + resumo diário")
    send_telegram("🟢 <b>Monitor Lithos iniciado</b>\nChecando NISP, blocos e resumo diário às 00:01 UTC")

    while True:
        try:
            # Resumo diário às 00:01 UTC
            if should_send_daily_summary():
                send_daily_summary()

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

                if prev_ready and not is_ready:
                    alerts.append(
                        f"🔴 <b>NISP CAIU ABAIXO DE {required}</b>\n"
                        f"Super shares: {nisp_count}/{required}\n"
                        f"Blocos restantes: {blocks}"
                    )

                if not prev_ready and is_ready:
                    alerts.append(
                        f"🟢 <b>NISP RECUPERADO ({nisp_count}/{required})</b>\n"
                        f"Blocos restantes: {blocks}"
                    )

                if last_state["critical"] is False and is_critical:
                    alerts.append(
                        f"⚠️ <b>BLOCOS CRÍTICOS</b>\n"
                        f"Blocos restantes: <b>{blocks}</b> (threshold: {BLOCKS_CRITICAL})\n"
                        f"Super shares: {nisp_count}/{required}"
                    )

                if last_state["critical"] is True and not is_critical:
                    alerts.append(
                        f"✅ <b>Blocos normalizados</b>\n"
                        f"Blocos restantes: {blocks}\n"
                        f"Super shares: {nisp_count}/{required}"
                    )

                if last_state["block_candidates"] is not None and block_candidates > last_state["block_candidates"]:
                    delta = block_candidates - last_state["block_candidates"]
                    alerts.append(
                        f"🎯 <b>BLOCK CANDIDATE ENCONTRADO</b>\n"
                        f"Novos candidatos: <b>{delta}</b>\n"
                        f"Total nesta sessão: {block_candidates}"
                    )

            # Novos blocos no pool
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

            for msg in alerts:
                log.info(f"Enviando alerta: {msg[:80]}...")
                send_telegram(msg)

            last_state["nisp_count"] = nisp_count
            last_state["critical"] = is_critical
            last_state["block_candidates"] = block_candidates

        except Exception as e:
            log.exception(f"Erro no ciclo: {e}")
            send_telegram(f"🚨 <b>Erro no monitor</b>\n<code>{str(e)[:400]}</code>")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
