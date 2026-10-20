#!/usr/bin/env python3
"""
Monitor Lithos NISP + NiceHash marketplace
- Alertas de blocos <15, <10 e <5
- Total protocolo + participação por heights
- Block candidates (aceito/rejeitado)
- Resumo diário com rollupScore
- Monitor de preço NiceHash (subir/baixar)
"""

import os
import time
import logging
import json
import statistics
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

# NiceHash
NICEHASH_ORDER_ID = os.getenv("NICEHASH_ORDER_ID", "").strip()
NICEHASH_ALGORITHM = os.getenv("NICEHASH_ALGORITHM", "AUTOLYKOS")
NICEHASH_MARKET = os.getenv("NICEHASH_MARKET", "BTC")
PRICE_DECREASE_COOLDOWN = int(os.getenv("PRICE_DECREASE_COOLDOWN", "600"))
PRICE_DROP_THRESHOLD = float(os.getenv("PRICE_DROP_THRESHOLD", "0.0005"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = Path(os.getenv("STATE_FILE", "lithos_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lithos-manager")

last_state = {
    "nisp_count": None,
    "critical": None,
    "critical_10": None,
    "critical_5": None,
    "max_block_height": None,
    "seen_heights": set(),
    "block_candidates": None,
    "solutions_accepted": None,
    "solutions_rejected": None,
    "current_utc_date": None,
    "initialized": False,
    "last_summary_date": None,
    "last_official_daily": 0,
    "last_price_decrease_alert": 0,
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
            last_state["last_price_decrease_alert"] = data.get("last_price_decrease_alert", 0)
            log.info(f"Estado carregado. Heights vistas hoje: {len(last_state['seen_heights'])}")
        else:
            log.info("Arquivo de estado é de outro dia. Set de heights zerado.")
            last_state["last_summary_date"] = data.get("last_summary_date")
            last_state["last_price_decrease_alert"] = data.get("last_price_decrease_alert", 0)
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
            "last_price_decrease_alert": last_state["last_price_decrease_alert"],
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
            "solutions_accepted": data.get("solutionsAccepted", 0),
            "solutions_rejected": data.get("solutionsRejected", 0),
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

def get_payments() -> Optional[Dict]:
    url = f"{LITHOS_BASE}/stats/mining/payments"
    try:
        resp = requests.get(url, headers=_headers(), timeout=15, verify=False)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Erro ao consultar /stats/mining/payments: {e}")
        return None

def get_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_utc_midnight(days_ago: int = 0) -> datetime:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days_ago)

def get_official_daily_blocks() -> int:
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

def get_rollup_score_stats() -> Dict[str, Any]:
    data = get_payments()
    if not data:
        return {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    day_ago_ms = now_ms - 86400000
    scores = []

    for p in data.get("payments", []):
        ts = p.get("timestamp") or p.get("minedTimestamp") or 0
        if ts >= day_ago_ms and p.get("rollupScore"):
            try:
                scores.append(int(p["rollupScore"]))
            except (ValueError, TypeError):
                pass

    for c in data.get("claims", []):
        if c.get("phase") != "evaluation":
            continue
        ts = c.get("phaseTimestamp") or c.get("submittedTimestamp") or 0
        if ts >= day_ago_ms and c.get("rollupScore"):
            try:
                scores.append(int(c["rollupScore"]))
            except (ValueError, TypeError):
                pass

    if not scores:
        return {"count": 0}
    return {
        "count": len(scores),
        "mean": int(statistics.mean(scores)),
        "median": int(statistics.median(scores)),
        "min": min(scores),
        "max": max(scores),
    }

def send_daily_summary():
    yesterday_midnight = get_utc_midnight(1)
    today_midnight = get_utc_midnight(0)
    from_ms = int(yesterday_midnight.timestamp() * 1000)
    until_ms = int(today_midnight.timestamp() * 1000)
    date_str = yesterday_midnight.strftime("%Y-%m-%d")

    log.info(f"Gerando resumo diário para {date_str}")

    data = get_mining_buckets(from_ms, until_ms, "day")
    protocol_blocks = 0
    local_participation = 0
    if data and data.get("buckets"):
        totals = data["buckets"][0].get("totals", {})
        protocol_blocks = int(totals.get("lithos.blocks", 0) or 0)
        local_participation = int(totals.get("rollup.submission.localCount", 0) or 0)

    missed = max(0, protocol_blocks - local_participation)
    score_stats = get_rollup_score_stats()

    msg = (
        f"📊 <b>Resumo diário — {date_str} (UTC)</b>\n\n"
        f"⛏️ Blocos minerados pelo protocolo: <b>{protocol_blocks}</b>\n"
        f"🎯 Blocos em que o pool participou: <b>{local_participation}</b>\n"
        f"❌ Blocos em que ficou de fora: <b>{missed}</b>\n"
    )
    if score_stats.get("count", 0) > 0:
        msg += (
            f"\n📈 <b>RollupScore (últimas 24h, {score_stats['count']} blocos)</b>\n"
            f"(pagamentos + evaluation)\n"
            f"Média: <b>{score_stats['mean']:,}</b>\n"
            f"Mediana: <b>{score_stats['median']:,}</b>\n"
            f"Min / Max: {score_stats['min']:,} / {score_stats['max']:,}"
        )
    else:
        msg += "\n📈 Sem dados de rollupScore nas últimas 24h."

    send_telegram(msg)
    last_state["last_summary_date"] = date_str
    save_state()

# ==================== NICEHASH MARKETPLACE ====================
def get_nicehash_orderbook() -> Optional[Dict]:
    url = "https://api2.nicehash.com/main/api/v2/hashpower/orderBook"
    params = {"algorithm": NICEHASH_ALGORITHM, "size": 50}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        stats = data.get("stats", {}).get(NICEHASH_MARKET)
        return stats
    except Exception as e:
        log.error(f"Erro ao consultar NiceHash orderBook: {e}")
        return None

def monitor_nicehash_price():
    if not NICEHASH_ORDER_ID:
        return

    book = get_nicehash_orderbook()
    if not book or not book.get("orders"):
        return

    orders = book["orders"]
    my_order = None
    for o in orders:
        if o.get("id") == NICEHASH_ORDER_ID:
            my_order = o
            break

    if not my_order:
        log.warning(f"Ordem {NICEHASH_ORDER_ID} não encontrada no book")
        return

    my_price = float(my_order.get("price", 0))
    my_rigs = int(my_order.get("rigsCount", 0))
    my_speed = float(my_order.get("acceptedSpeed", 0))

    higher_active = [
        o for o in orders
        if o.get("id") != NICEHASH_ORDER_ID
        and float(o.get("price", 0)) > my_price
        and int(o.get("rigsCount", 0)) > 0
        and float(o.get("acceptedSpeed", 0)) > 0.0001
    ]

    other_active_prices = [
        float(o["price"]) for o in orders
        if o.get("id") != NICEHASH_ORDER_ID and int(o.get("rigsCount", 0)) > 0
    ]
    top_other = max(other_active_prices, default=0)

    alerts = []

    if my_rigs == 0 and my_speed < 0.0001 and higher_active:
        top_higher = max(float(o["price"]) for o in higher_active)
        alerts.append(
            f"📈 <b>SUBIR PREÇO NICEHASH</b>\n"
            f"Sua ordem está sem mineradores\n"
            f"Preço atual: {my_price:.4f}\n"
            f"Ordens mais altas ativas: até {top_higher:.4f}\n"
            f"Rigs suas: {my_rigs} | Speed: {my_speed:.6f}"
        )

    now = time.time()
    if (now - last_state["last_price_decrease_alert"]) >= PRICE_DECREASE_COOLDOWN:
        if top_other > 0 and my_price > top_other + PRICE_DROP_THRESHOLD and my_rigs > 0:
            alerts.append(
                f"📉 <b>PODE BAIXAR PREÇO NICEHASH</b>\n"
                f"Seu preço: {my_price:.4f}\n"
                f"Próxima ordem ativa: {top_other:.4f}\n"
                f"Diferença: {my_price - top_other:.4f}\n"
                f"Cooldown de baixa: ok\n"
                f"Rigs: {my_rigs} | Speed: {my_speed:.6f}"
            )
            last_state["last_price_decrease_alert"] = now
            save_state()

    for msg in alerts:
        log.info(f"NiceHash alert: {msg[:80]}")
        send_telegram(msg)

# ==================== LOOP ====================
def main():
    global last_state

    load_state()

    log.info("Iniciando monitor Lithos + NiceHash")
    send_telegram("🟢 <b>Monitor Lithos + NiceHash iniciado</b>")

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
            sol_accepted = nisp["solutions_accepted"]
            sol_rejected = nisp["solutions_rejected"]
            is_ready = nisp_count >= required
            is_critical = blocks < BLOCKS_CRITICAL

            log.info(
                f"NISP | count={nisp_count}/{required} | ready={is_ready} | "
                f"blocks={blocks} | candidates={block_candidates} | "
                f"accepted={sol_accepted} rejected={sol_rejected}"
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
                last_state["critical_10"] = blocks < 10
                last_state["critical_5"] = blocks < 5
                last_state["initialized"] = True
                last_state["nisp_count"] = nisp_count
                last_state["critical"] = is_critical
                last_state["block_candidates"] = block_candidates
                last_state["solutions_accepted"] = sol_accepted
                last_state["solutions_rejected"] = sol_rejected

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

                # Alertas específicos <10 e <5
                if last_state["critical_10"] is not True and blocks < 10:
                    alerts.append(
                        f"⚠️ <b>BLOCOS ABAIXO DE 10</b>\n"
                        f"Blocos restantes: <b>{blocks}</b>\n"
                        f"Super shares: {nisp_count}/{required}\n"
                        f"NISP: {'✅ READY' if is_ready else '❌ NÃO READY'}"
                    )
                    last_state["critical_10"] = True

                if last_state["critical_10"] is True and blocks >= 10:
                    last_state["critical_10"] = False

                if last_state["critical_5"] is not True and blocks < 5:
                    alerts.append(
                        f"🚨 <b>BLOCOS ABAIXO DE 5</b>\n"
                        f"Blocos restantes: <b>{blocks}</b>\n"
                        f"Super shares: {nisp_count}/{required}\n"
                        f"NISP: {'✅ READY' if is_ready else '❌ NÃO READY'}"
                    )
                    last_state["critical_5"] = True

                if last_state["critical_5"] is True and blocks >= 5:
                    last_state["critical_5"] = False

                if last_state["block_candidates"] is not None:
                    if block_candidates > last_state["block_candidates"]:
                        delta_cand = block_candidates - last_state["block_candidates"]
                        accepted_delta = sol_accepted - (last_state["solutions_accepted"] or 0)
                        rejected_delta = sol_rejected - (last_state["solutions_rejected"] or 0)

                        if accepted_delta > 0:
                            alerts.append(
                                f"🎯 <b>BLOCK CANDIDATE ACEITO</b>\n"
                                f"Novos candidatos: <b>{delta_cand}</b>\n"
                                f"Soluções aceitas: +{accepted_delta}\n"
                                f"Total aceitas nesta sessão: {sol_accepted}"
                            )
                        if rejected_delta > 0:
                            alerts.append(
                                f"❌ <b>BLOCK CANDIDATE REJEITADO</b>\n"
                                f"Candidato minerado mas rejeitado: <b>{rejected_delta}</b>\n"
                                f"Total rejeitadas nesta sessão: {sol_rejected}"
                            )
                        if accepted_delta == 0 and rejected_delta == 0:
                            alerts.append(
                                f"🎯 <b>BLOCK CANDIDATE ENCONTRADO</b>\n"
                                f"Novos candidatos: <b>{delta_cand}</b>\n"
                                f"Aguardando confirmação de aceitação"
                            )

                last_state["solutions_accepted"] = sol_accepted
                last_state["solutions_rejected"] = sol_rejected

            # Participação via heights
            pool_blocks = get_pool_blocks()
            if pool_blocks:
                current_heights = {b.get("blockHeight") for b in pool_blocks if b.get("blockHeight")}
                prev_max = last_state["max_block_height"] or 0
                new_heights = {h for h in current_heights if h > prev_max and h not in last_state["seen_heights"]}

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
                    save_state()

            for msg in alerts:
                log.info(f"Enviando alerta: {msg[:80]}...")
                send_telegram(msg)

            last_state["nisp_count"] = nisp_count
            last_state["critical"] = is_critical
            last_state["block_candidates"] = block_candidates

            # NiceHash price monitor
            monitor_nicehash_price()

        except Exception as e:
            log.exception(f"Erro no ciclo: {e}")
            send_telegram(f"🚨 <b>Erro no monitor</b>\n<code>{str(e)[:400]}</code>")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
