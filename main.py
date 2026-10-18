#!/usr/bin/env python3
"""
Monitor Lithos NISP + novos blocos + block candidates
- Total protocolo: lithos.blocks (buckets)
- Participação do pool: heights novas em /blocks/all (set persistido)
- Lista as heights na notificação
Resumo diário às 00:01 UTC.
"""

import os
import time
import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Set

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
    "seen_heights": set(),
    "block_candidates": None,
    "current_utc_date": None,
    "initialized": False,
    "last_summary_date": None,
    "last_official_daily": 0,
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
            last_state["current_utc_date"] = today
            last_state["max_block_height"] = data.get("max_block_height")
            last_state["seen_heights"] = set(data.get("seen_heights", []))
            last_state["last_summary_date"] = data.get("last_summary_date")
            last_state["last_official_daily"] = data.get("last_official_daily", 0)
            log.info(f"Estado carregado. Heights vistas hoje: {len(last_state['seen_heights'])}")
        else:
            log.info("Arquivo de estado é de outro dia. Set de heights zerado.")
            last_state["last_summary_date"] = data.get("last_summary_date")
    except Exception as e:
        log.warning(f"Não foi possível carregar estado: {e}")

def save_state():
    try:
        data = {
            "current_utc_date": last_state["current_utc_date"],
            "max_block_height": last_state["max_block_height"],
            "seen_heights": list(last_state["seen_heights"]),
            "last_summary_date": last_state["last_summary_date"],
            "last_official_daily": last_state["last_official_daily"],
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

def get_official_daily_blocks() -> int:
    """lithos.blocks do dia UTC atual."""
    today_midnight = get_utc_midnight(0)
    next_midnight = today_midnight + timedelta(days=1)
    from_ms = int(today_midnight.timestamp() * 1000)
    until_ms = int(next_midnight.timestamp() * 1000)
    data = get_mining_buckets(from_ms, until_ms, "day")
    if not data or not data.get("buckets"):
        return last_state["last_official_daily"]
    totals = data["buckets"][0].get("totals", {})
    count = int(totals.get("lithos.blocks", 0) or 0)
    last_state["last_official_daily"] = count
    return count

def should_send_daily_summary() -> bool:
    now = datetime.now(timezone.utc)
    if now.hour == 0 and now.minute >= 1:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if last_state["last_summary_date"] != yesterday:
            return True
    return False

def send_daily_summary():
    yesterday_midnight = get_utc_midnight(1)
    today_midnight = get_utc_midnight(0)
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

    totals = data["buckets"][0].get("totals", {})
    protocol_blocks = int(totals.get("lithos.blocks", 0) or 0)
    local_participation = int(totals.get("rollup.submission.localCount", 0) or 0)
    missed = max(0, protocol_blocks - local_participation)

    msg = (
        f"📊 <b>Resumo diário — {date_str} (UTC)</b>\n\n"
        f"⛏️ Blocos minerados pelo protocolo: <b>{protocol_blocks}</b>\n"
        f"🎯 Blocos em que o pool participou: <b>{local_participation}</b>\n"
        f"❌ Blocos em que ficou de fora: <b>{missed}</b>"
    )
    send_telegram(msg)
    log.info(f"Resumo enviado: protocolo={protocol_blocks}, local={local_participation}")

    last_state["last_summary_date"] = date_str
    save_state()

# ==================== LOOP ====================
def main():
    global last_state

    load_state()

    log.info("Iniciando monitor Lithos (total protocolo + participação por heights)")
    send_telegram("🟢 <b>Monitor Lithos iniciado</b>\nTotal protocolo (buckets) + participação (heights)")

    while True:
        try:
            if should_send_daily_summary():
                send_daily_summary()

            today_utc = get_utc_date()
            if last_state["current_utc_date"] != today_utc:
                if last_state["current_utc_date"] is not None:
                    log.info(f"Novo dia UTC ({today_utc}). Set de heights zerado.")
                    send_telegram("📅 <b>Novo dia UTC</b>\nContadores de participação zerados.")
                last_state["current_utc_date"] = today_utc
                last_state["seen_heights"] = set()
                last_state["last_official_daily"] = 0
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
                f"blocks={blocks} | candidates={block_candidates} | "
                f"participated={len(last_state['seen_heights'])}"
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

            # --- Participação via heights novas em /blocks/all ---
            pool_blocks = get_pool_blocks()
            if pool_blocks:
                current_heights = {b.get("blockHeight") for b in pool_blocks if b.get("blockHeight")}
                new_heights = current_heights - last_state["seen_heights"]

                if new_heights:
                    official_daily = get_official_daily_blocks()
                    last_state["seen_heights"].update(new_heights)
                    participated = len(last_state["seen_heights"])
                    save_state()

                    heights_list = ", ".join(str(h) for h in sorted(new_heights))
                    alerts.append(
                        f"⛏️ <b>NOVOS BLOCOS NO POOL</b>\n"
                        f"Novos nesta checagem: <b>{len(new_heights)}</b>\n"
                        f"Heights: <code>{heights_list}</code>\n"
                        f"Protocolo hoje: <b>{official_daily}</b>\n"
                        f"Pool participou: <b>{participated}</b>\n"
                        f"Ficou de fora: <b>{max(0, official_daily - participated)}</b>"
                    )

                if current_heights:
                    last_state["max_block_height"] = max(current_heights)

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
