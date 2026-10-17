#!/usr/bin/env python3
"""
Monitor Lithos NISP - apenas alertas no Telegram
Checa a cada 60 segundos e avisa mudanças de estado.
"""

import os
import time
import logging
from typing import Optional, Dict, Any

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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lithos-monitor")

# Estado anterior para detectar mudanças
last_state = {
    "held": None,
    "critical": None,
}

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

# ==================== LITHOS ====================
def get_nisp_status() -> Optional[Dict[str, Any]]:
    url = f"{LITHOS_BASE}/stats/mining/workers"
    headers = {}
    if LITHOS_API_KEY:
        headers["X-Api-Key"] = LITHOS_API_KEY

    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
        resp.raise_for_status()
        data = resp.json()
        nisp = data.get("nisp")
        if not nisp:
            log.warning("Resposta sem objeto 'nisp'")
            return None

        return {
            "held": nisp.get("held", False),
            "blocks_remaining": nisp.get("blocksRemaining"),
            "super_shares": nisp.get("superSharesInWindow", 0),
            "required": nisp.get("required", 10),
            "valid_through": nisp.get("validThroughHeight"),
        }
    except Exception as e:
        log.error(f"Erro ao consultar Lithos: {e}")
        return None

# ==================== LOOP ====================
def main():
    global last_state

    log.info("Iniciando monitor Lithos NISP")
    send_telegram("🟢 <b>Monitor Lithos iniciado</b>\nChecando NISP a cada 60s")

    while True:
        try:
            nisp = get_nisp_status()
            if not nisp or nisp["blocks_remaining"] is None:
                log.warning("Não foi possível obter status do Lithos")
                time.sleep(POLL_INTERVAL)
                continue

            held = nisp["held"]
            blocks = nisp["blocks_remaining"]
            shares = nisp["super_shares"]
            required = nisp["required"]
            is_critical = blocks < BLOCKS_CRITICAL

            log.info(
                f"NISP | held={held} | shares={shares}/{required} | "
                f"blocks={blocks} | critical={is_critical}"
            )

            # Detecta mudanças de estado
            alerts = []

            # NISP caiu abaixo de 10
            if last_state["held"] is True and not held:
                alerts.append(
                    f"🔴 <b>NISP PERDIDO</b>\n"
                    f"Super shares: {shares}/{required}\n"
                    f"Blocos restantes: {blocks}"
                )

            # NISP voltou para 10
            if last_state["held"] is False and held:
                alerts.append(
                    f"🟢 <b>NISP RECUPERADO</b>\n"
                    f"Super shares: {shares}/{required}\n"
                    f"Blocos restantes: {blocks}"
                )

            # Entrou em nível crítico de blocos
            if last_state["critical"] is False and is_critical:
                alerts.append(
                    f"⚠️ <b>BLOCOS CRÍTICOS</b>\n"
                    f"Blocos restantes: <b>{blocks}</b> (threshold: {BLOCKS_CRITICAL})\n"
                    f"Super shares: {shares}/{required}\n"
                    f"NISP: {'✅ READY' if held else '❌ NÃO READY'}"
                )

            # Saiu do nível crítico
            if last_state["critical"] is True and not is_critical:
                alerts.append(
                    f"✅ <b>Blocos normalizados</b>\n"
                    f"Blocos restantes: {blocks}\n"
                    f"NISP: {'✅ READY' if held else '❌ NÃO READY'}"
                )

            # Envia alertas
            for msg in alerts:
                log.info(f"Enviando alerta: {msg[:80]}...")
                send_telegram(msg)

            # Atualiza estado
            last_state["held"] = held
            last_state["critical"] = is_critical

        except Exception as e:
            log.exception(f"Erro no ciclo: {e}")
            send_telegram(f"🚨 <b>Erro no monitor</b>\n<code>{str(e)[:400]}</code>")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
