#!/usr/bin/env python3
"""Create the public, static Polymarket snapshot used by the panel.

This script is run by GitHub Actions. It never runs in the visitor's browser.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


EVENT_SLUG = "brazil-presidential-election"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUTPUT = Path(__file__).resolve().parents[1] / "polymarket.json"
BRT = ZoneInfo("America/Sao_Paulo")
UTC = timezone.utc
ALIASES = {
    "lula": ("lula", "luiz inacio lula da silva"),
    "flavio": ("flavio bolsonaro", "flavio"),
}


def get_json(url: str):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "apuracao-eleicoes-2026-painel/1.0"})
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=25) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} em {url.split('?')[0]}")
                return json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"falha ao consultar endpoint público: {last_error}") from last_error


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def candidate_in(value: str) -> str | None:
    text = f" {normalized(value)} "
    for candidate, aliases in ALIASES.items():
        if any(f" {normalized(alias)} " in text for alias in aliases):
            return candidate
    return None


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def outcome_records(market: dict) -> list[dict]:
    outcomes = decode(market.get("outcomes"))
    prices = decode(market.get("outcomePrices"))
    if not isinstance(prices, list):
        prices = decode(market.get("prices"))
    if not isinstance(prices, list):
        prices = []
    tokens = decode(market.get("clobTokenIds"))
    if not isinstance(tokens, list):
        tokens = decode(market.get("tokenIds"))
    if not isinstance(tokens, list):
        tokens = []

    records = []
    if isinstance(outcomes, list):
        for index, outcome in enumerate(outcomes):
            if isinstance(outcome, dict):
                label = str(outcome.get("name") or outcome.get("outcome") or outcome.get("label") or "")
                token = outcome.get("tokenId") or outcome.get("token_id")
                price = outcome.get("price")
                if price is None:
                    price = outcome.get("probability")
                if price is None:
                    price = outcome.get("outcomePrice")
            else:
                label = str(outcome)
                token = None
                price = None
            if token is None and index < len(tokens):
                token = tokens[index]
            if price is None and index < len(prices):
                price = prices[index]
            records.append({"label": label, "token": token, "price": number(price)})
    elif isinstance(outcomes, dict):
        for index, (key, outcome) in enumerate(outcomes.items()):
            if isinstance(outcome, dict):
                label = str(outcome.get("name") or outcome.get("outcome") or outcome.get("label") or key)
                token = outcome.get("tokenId") or outcome.get("token_id")
                price = outcome.get("price")
                if price is None:
                    price = outcome.get("probability")
                if price is None:
                    price = outcome.get("outcomePrice")
            else:
                label, token, price = str(key), outcome, None
            if token is None and index < len(tokens):
                token = tokens[index]
            if price is None:
                if isinstance(prices, dict):
                    price = prices.get(key)
                elif index < len(prices):
                    price = prices[index]
            records.append({"label": label, "token": token, "price": number(price)})

    # Some Gamma market versions place token objects in a separate field.
    token_objects = decode(market.get("tokens"))
    if isinstance(token_objects, list):
        for index, token_object in enumerate(token_objects):
            if not isinstance(token_object, dict):
                continue
            if index >= len(records):
                records.append({"label": "", "token": None, "price": None})
            if not records[index].get("token"):
                records[index]["token"] = token_object.get("token_id") or token_object.get("tokenId")
            if records[index].get("price") is None:
                records[index]["price"] = number(token_object.get("price"))
    return records


def market_text(market: dict) -> str:
    return " ".join(str(market.get(key) or "") for key in ("question", "title", "slug", "groupItemTitle"))


def choose_candidate_markets(markets: list[dict]) -> dict[str, dict]:
    matches = {"lula": [], "flavio": []}
    for market in markets:
        if not isinstance(market, dict):
            continue
        records = outcome_records(market)
        direct = {}
        for record in records:
            candidate = candidate_in(record["label"])
            if candidate:
                direct.setdefault(candidate, []).append(record)
        if all(len(direct.get(candidate, [])) == 1 for candidate in matches):
            for candidate in matches:
                record = direct[candidate][0]
                matches[candidate].append({"market": market, "record": record})
            continue

        labels = {normalized(record["label"]): record for record in records}
        yes = labels.get("yes")
        no = labels.get("no")
        candidate = candidate_in(market_text(market))
        if candidate and yes and no:
            matches[candidate].append({"market": market, "record": yes})

    result = {}
    for candidate, found in matches.items():
        unique = {}
        for item in found:
            token = item["record"].get("token")
            if token:
                unique[str(token)] = item
        if len(unique) != 1:
            titles = [market_text(item["market"])[:120] for item in found]
            raise RuntimeError(f"não foi possível identificar um único resultado de {candidate}; correspondências: {titles}")
        item = next(iter(unique.values()))
        record = item["record"]
        price = record.get("price")
        if price is None or not 0 <= price <= 1:
            raise RuntimeError(f"Gamma não trouxe preço válido para {candidate}")
        result[candidate] = {"token": str(record["token"]), "price": price}
    if result["lula"]["token"] == result["flavio"]["token"]:
        raise RuntimeError("Gamma associou o mesmo token a Lula e Flávio")
    return result


def fetch_event_markets() -> list[dict]:
    payload = get_json(f"{GAMMA}/events/slug/{EVENT_SLUG}")
    if isinstance(payload, list):
        events = [item for item in payload if isinstance(item, dict)]
        event = next((item for item in events if item.get("slug") == EVENT_SLUG), None)
    else:
        event = payload if isinstance(payload, dict) else None
    if not event or event.get("slug") not in (None, EVENT_SLUG):
        raise RuntimeError(f"evento Gamma {EVENT_SLUG} não encontrado ou slug inesperado")
    markets = event.get("markets")
    if not isinstance(markets, list) or not markets:
        raise RuntimeError("evento Gamma não trouxe mercados associados")
    return markets


def fetch_history(token: str, start_ts: int, end_ts: int) -> list[dict]:
    query = urlencode({"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": 60})
    payload = get_json(f"{CLOB}/prices-history?{query}")
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        raise RuntimeError("CLOB não trouxe a série histórica esperada")
    points = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = int(item.get("t"))
        except (TypeError, ValueError):
            continue
        price = number(item.get("p"))
        if start_ts <= timestamp <= end_ts and price is not None and 0 <= price <= 1:
            points[timestamp] = round(price * 100, 4)
    return [{"t": timestamp, "probability_pct": points[timestamp]} for timestamp in sorted(points)]


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def main() -> None:
    markets = fetch_event_markets()
    selected = choose_candidate_markets(markets)
    end_ts = int(datetime.now(UTC).timestamp())
    start_ts = end_ts - 24 * 60 * 60
    series = {}
    for candidate, label in (("lula", "Lula"), ("flavio", "Flávio Bolsonaro")):
        history = fetch_history(selected[candidate]["token"], start_ts, end_ts)
        series[candidate] = {
            "label": label,
            "price_pct": round(selected[candidate]["price"] * 100, 4),
            "points": history,
        }
    updated = datetime.now(UTC)
    payload = {
        "schema": 1,
        "source": "Polymarket public Gamma API and CLOB prices-history",
        "event_slug": EVENT_SLUG,
        "updated_at_utc": updated.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "updated_at_brt": updated.astimezone(BRT).isoformat(timespec="seconds"),
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
        "series": series,
    }
    atomic_write(OUTPUT, payload)
    print(f"Snapshot gravado: Lula {series['lula']['price_pct']:.2f}% / Flávio {series['flavio']['price_pct']:.2f}%")
    print(f"Histórico de 24 h: Lula {len(series['lula']['points'])} pontos; Flávio {len(series['flavio']['points'])} pontos")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Falha ao atualizar o snapshot Polymarket: {exc}")
        raise SystemExit(1)
