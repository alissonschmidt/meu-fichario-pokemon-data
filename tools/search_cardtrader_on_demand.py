#!/usr/bin/env python3
"""Busca sob demanda do Top 3 de cartas de um Pokémon/forma na CardTrader.

Este worker é executado pelo GitHub Actions. Ele mantém progresso em jobs/<jobId>.json,
resultado definitivo em results/<pokemon-key>.json e nunca expõe CARDTRADER_TOKEN ao app.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import update_catalog_cardtrader_v3 as base

JOB_ID = os.environ.get("JOB_ID", "").strip()
SPECIES_NUMBER = int(os.environ.get("SPECIES_NUMBER", "0") or 0)
API_IDENTIFIER_RAW = os.environ.get("API_IDENTIFIER", "").strip()
API_IDENTIFIER = API_IDENTIFIER_RAW or None
POKEMON_NAME = os.environ.get("POKEMON_NAME", "").strip()
FORCE_REFRESH = os.environ.get("FORCE_REFRESH", "false").lower() == "true"
PROGRESS_COMMIT_EVERY = max(5, int(os.environ.get("PROGRESS_COMMIT_EVERY", "25")))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60)))

JOBS_DIR = Path("jobs")
RESULTS_DIR = Path("results")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_key(value: str | None) -> str:
    if not value:
        return "normal"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "form"


def result_path() -> Path:
    return RESULTS_DIR / f"{SPECIES_NUMBER:04d}-{safe_key(API_IDENTIFIER)}.json"


def job_path() -> Path:
    return JOBS_DIR / f"{JOB_ID}.json"


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git_publish(paths: list[Path], message: str) -> None:
    existing = [str(p) for p in paths if p.exists()]
    if not existing:
        return
    subprocess.run(["git", "add", *existing], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    subprocess.run(["git", "push"], check=True)


def parse_iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def fresh_cached_result(path: Path) -> dict[str, Any] | None:
    if FORCE_REFRESH or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("rankingComplete") is not True:
        return None
    searched_epoch = parse_iso_epoch(payload.get("searchedAt"))
    if searched_epoch is None:
        return None
    if time.time() - searched_epoch > CACHE_TTL_SECONDS:
        return None
    return payload


def pt_br_value(cents: int, currency: str) -> str:
    number = cents / 100
    raw = f"{number:,.2f}"
    localized = raw.replace(",", "X").replace(".", ",").replace("X", ".")
    symbol = {"BRL": "R$", "USD": "US$", "EUR": "€", "GBP": "£"}.get(currency, currency)
    return f"{symbol} {localized}"


def direct_card_url(card: dict[str, Any]) -> str:
    blueprint_id = int(card.get("blueprintId", 0) or 0)
    raw = f"{card.get('name', '')} {card.get('collection', '')}".strip().lower()
    slug = base.normalize(raw).replace(" ", "-") or "card"
    return f"https://www.cardtrader.com/en/cards/{blueprint_id}-{slug}"


def api_card(card: dict[str, Any], rank: int) -> dict[str, Any]:
    cents = int(card.get("referenceCents", 0) or 0)
    currency = str(card.get("currency", "")).upper()
    return {
        "rank": rank,
        "blueprintId": int(card["blueprintId"]),
        "name": card.get("name") or "",
        "collection": card.get("collection"),
        "code": card.get("code"),
        "rarity": card.get("rarity"),
        "value": pt_br_value(cents, currency),
        "referenceCents": cents,
        "currency": currency,
        "url": direct_card_url(card),
        "priceMethod": card.get("priceMethod") or "Mediana de até 15 ofertas Near Mint mais baratas",
    }


def status_payload(
    *,
    status: str,
    phase: str,
    target_name: str,
    processed: int,
    total: int,
    valid: int,
    cards: list[dict[str, Any]],
    created_at: str,
    started_at: str | None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    percent = 100.0 if total == 0 and status == "completed" else (processed * 100.0 / total if total else 0.0)
    elapsed = 0.0
    if started_at:
        start_epoch = parse_iso_epoch(started_at)
        if start_epoch:
            elapsed = max(0.0, time.time() - start_epoch)
    eta = None
    if processed > 0 and total > processed and elapsed > 0:
        eta = int(round((elapsed / processed) * (total - processed)))
    return {
        "jobId": JOB_ID,
        "status": status,
        "speciesNumber": SPECIES_NUMBER,
        "apiIdentifier": API_IDENTIFIER,
        "pokemonName": target_name,
        "phase": phase,
        "rankingStatus": "definitive" if status == "completed" else "provisional",
        "rankingComplete": status == "completed",
        "progress": {
            "percent": round(percent, 1),
            "processedBlueprints": processed,
            "candidateBlueprints": total,
            "validPriceReferences": valid,
            "remainingBlueprints": max(0, total - processed),
        },
        "currentTop3": [api_card(c, i + 1) for i, c in enumerate(base.rank(cards))],
        "createdAt": created_at,
        "startedAt": started_at,
        "updatedAt": now_iso(),
        "completedAt": now_iso() if status == "completed" else None,
        "estimatedSecondsRemaining": eta,
        "error": error,
    }


def select_target() -> dict[str, Any]:
    targets = base.species_targets(SPECIES_NUMBER)
    for target in targets:
        identifier = target.get("apiIdentifier") or None
        if identifier == API_IDENTIFIER:
            return target
    raise ValueError("Pokémon ou forma não localizada para speciesNumber/apiIdentifier informados")


def main() -> int:
    if not JOB_ID:
        raise SystemExit("JOB_ID não informado")
    if not 1 <= SPECIES_NUMBER <= base.MAX_POKEDEX:
        raise SystemExit("SPECIES_NUMBER inválido")
    if not base.TOKEN:
        raise SystemExit("CARDTRADER_TOKEN não configurado")

    created_at = now_iso()
    started_at = now_iso()
    target_name = POKEMON_NAME or f"Pokémon #{SPECIES_NUMBER:04d}"

    cached = fresh_cached_result(result_path())
    if cached:
        job = {
            "jobId": JOB_ID,
            "status": "completed",
            "speciesNumber": SPECIES_NUMBER,
            "apiIdentifier": API_IDENTIFIER,
            "pokemonName": cached.get("pokemonName", target_name),
            "phase": "completed",
            "rankingStatus": "definitive",
            "rankingComplete": True,
            "progress": {
                "percent": 100.0,
                "processedBlueprints": cached.get("search", {}).get("processedBlueprints", 0),
                "candidateBlueprints": cached.get("search", {}).get("candidateBlueprints", 0),
                "validPriceReferences": cached.get("search", {}).get("validPriceReferences", 0),
                "remainingBlueprints": 0,
            },
            "currentTop3": cached.get("top3", []),
            "createdAt": created_at,
            "startedAt": started_at,
            "updatedAt": now_iso(),
            "completedAt": now_iso(),
            "estimatedSecondsRemaining": 0,
            "cacheHit": True,
            "resultKey": result_path().stem,
            "error": None,
        }
        save_json(job_path(), job)
        git_publish([job_path()], f"Finaliza job {JOB_ID} via cache")
        return 0

    try:
        base.api("/info")
        index = base.get_index()
        target = select_target()
        target_name = target.get("name") or target_name
        pool = base.candidates(target, index)
        total = len(pool)
        collected: list[dict[str, Any]] = []
        processed = 0
        valid = 0

        job = status_payload(
            status="running", phase="marketplace_scan", target_name=target_name,
            processed=0, total=total, valid=0, cards=[], created_at=created_at,
            started_at=started_at,
        )
        save_json(job_path(), job)
        git_publish([job_path()], f"Inicia busca {JOB_ID}")

        for bp in pool:
            try:
                card = base.reference_for(bp)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise RuntimeError("CARDTRADER_RATE_LIMIT") from exc
                raise RuntimeError(f"CARDTRADER_HTTP_ERROR:{exc.code}") from exc
            processed += 1
            if card:
                collected.append(card)
                valid += 1
            if processed % PROGRESS_COMMIT_EVERY == 0 or processed == total:
                job = status_payload(
                    status="running", phase="marketplace_scan", target_name=target_name,
                    processed=processed, total=total, valid=valid, cards=collected,
                    created_at=created_at, started_at=started_at,
                )
                save_json(job_path(), job)
                git_publish([job_path()], f"Atualiza progresso {JOB_ID} {processed}/{total}")
            time.sleep(base.MARKETPLACE_DELAY)

        top_cards = [api_card(c, i + 1) for i, c in enumerate(base.rank(collected))]
        completed_at = now_iso()
        started_epoch = parse_iso_epoch(started_at) or time.time()
        duration = max(0, int(round(time.time() - started_epoch)))
        result = {
            "jobId": JOB_ID,
            "status": "completed",
            "speciesNumber": SPECIES_NUMBER,
            "apiIdentifier": API_IDENTIFIER,
            "pokemonName": target_name,
            "rankingComplete": True,
            "rankingStatus": "definitive",
            "search": {
                "candidateBlueprints": total,
                "processedBlueprints": processed,
                "validPriceReferences": valid,
                "startedAt": started_at,
                "completedAt": completed_at,
                "durationSeconds": duration,
            },
            "pricing": {
                "source": "CardTrader",
                "condition": "Near Mint",
                "method": "Mediana de até 15 ofertas Near Mint mais baratas por blueprint",
                "currency": top_cards[0]["currency"] if top_cards else "BRL",
            },
            "top3": top_cards,
            "searchedAt": completed_at,
            "cachedUntilEpoch": int(time.time()) + CACHE_TTL_SECONDS,
            "cachedUntil": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + CACHE_TTL_SECONDS)),
        }
        save_json(result_path(), result)

        final_job = status_payload(
            status="completed", phase="completed", target_name=target_name,
            processed=processed, total=total, valid=valid, cards=collected,
            created_at=created_at, started_at=started_at,
        )
        final_job["resultKey"] = result_path().stem
        final_job["cacheHit"] = False
        save_json(job_path(), final_job)
        git_publish([job_path(), result_path()], f"Conclui busca {JOB_ID}")
        return 0

    except Exception as exc:
        message = str(exc)
        code = "INTERNAL_ERROR"
        retryable = True
        if "RATE_LIMIT" in message:
            code = "CARDTRADER_RATE_LIMIT"
        elif "CARDTRADER_HTTP_ERROR" in message:
            code = "CARDTRADER_HTTP_ERROR"
        elif isinstance(exc, ValueError):
            code = "INVALID_POKEMON"
            retryable = False
        failed = status_payload(
            status="failed", phase="marketplace_scan", target_name=target_name,
            processed=0, total=0, valid=0, cards=[], created_at=created_at,
            started_at=started_at,
            error={"code": code, "message": message, "retryable": retryable},
        )
        save_json(job_path(), failed)
        try:
            git_publish([job_path()], f"Registra falha {JOB_ID}")
        except Exception as git_exc:
            print(f"Falha ao publicar status de erro: {git_exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
