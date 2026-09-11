#!/usr/bin/env python3
"""Coletor CardTrader v4: ranking definitivo somente após varrer todos os blueprints candidatos.

Reutiliza os parsers/normalizadores estáveis do v3, mas remove a parada antecipada por entrada.
O top 3 é mantido incrementalmente; `rankingComplete=true` só quando todos os candidatos
foram consultados com sucesso (ou retornaram sem ofertas válidas).
"""
import time
import urllib.error

import update_catalog_cardtrader_v3 as base

CATALOG_VERSION = 10
PROGRESS_VERSION = 6
PARSER_VERSION = "10.0-exhaustive-ranking"


def ensure_entry(entries, by_key, number, target):
    k = (number, target["apiIdentifier"])
    entry = by_key.get(k)
    if entry is None:
        entry = {
            "speciesNumber": number,
            "name": target["name"],
            "formKind": target["formKind"],
            "cards": [],
            "scanCursor": 0,
            "scanComplete": False,
            "rankingComplete": False,
            "rankingStatus": "provisional",
            "lookupStatus": "pending",
            "manualSearchUrl": base.MANUAL_URL,
        }
        if target["apiIdentifier"] is not None:
            entry["apiIdentifier"] = target["apiIdentifier"]
        entries.append(entry)
        by_key[k] = entry
    else:
        entry["name"] = target["name"]
        entry["formKind"] = target["formKind"]
        entry["manualSearchUrl"] = base.MANUAL_URL
        entry["cards"] = [
            c for c in entry.get("cards", [])
            if str(c.get("source", "")).lower() == "cardtrader"
        ]
    entry.pop("lastLookupHttpStatus", None)
    return entry


def reset_rankings_from_start(entries, progress):
    """Zera os cursores de TODAS as entradas para uma nova varredura exaustiva desde #0001.

    As cartas já conhecidas são preservadas apenas como ranking provisório. Como o cursor volta
    a zero, todos os blueprints serão revisitados e o `rank()` deduplicará pelo blueprintId.
    """
    if not progress.get("rankingResetRequested"):
        return False

    for entry in entries:
        entry["scanCursor"] = 0
        entry["scanComplete"] = False
        entry["rankingComplete"] = False
        entry["rankingStatus"] = "provisional"
        entry["lookupStatus"] = "found" if entry.get("cards") else "pending"
        entry.pop("lastLookupHttpStatus", None)

    progress["nextSpeciesNumber"] = 1
    progress["mode"] = "first_pass"
    progress["rankingsDefinitive"] = 0
    progress["rankingResetAppliedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    progress["rankingResetApplied"] = True
    progress.pop("rankingResetRequested", None)
    print("RESET aplicado: todos os cursores zerados; ranking reiniciado em #0001")
    return True


def main():
    if not base.TOKEN:
        raise SystemExit("CARDTRADER_TOKEN não configurado")

    info = base.api("/info")
    print(f"Autenticação OK (/info={type(info).__name__})")
    index = base.get_index()

    catalog = base.load(base.CATALOG, {"version": CATALOG_VERSION, "pokemon": []})
    progress = base.load(
        base.PROGRESS,
        {"version": PROGRESS_VERSION, "mode": "first_pass", "nextSpeciesNumber": 1, "totalRuns": 0},
    )
    entries = catalog.setdefault("pokemon", [])

    # Limpa fontes antigas e aplica reset explícito antes de calcular o ponto de retomada.
    for entry in entries:
        entry["cards"] = [
            c for c in entry.get("cards", [])
            if str(c.get("source", "")).lower() == "cardtrader"
        ]
        entry.pop("lastLookupHttpStatus", None)

    reset_applied = reset_rankings_from_start(entries, progress)

    if not reset_applied:
        # Migração segura do estado já coletado: preservamos o top 3 e o cursor, mas nunca
        # tratamos um ranking como definitivo sem o cursor ter chegado ao fim do pool.
        for entry in entries:
            entry["rankingComplete"] = bool(entry.get("scanComplete", False))
            entry["rankingStatus"] = "definitive" if entry["rankingComplete"] else "provisional"

    by_key = {base.entry_key(e): e for e in entries}
    start = max(1, min(base.MAX_POKEDEX, int(progress.get("nextSpeciesNumber", 1) or 1)))

    calls = 0
    observed = 0
    species_touched = 0
    species_completed = 0
    http_errors = 0
    stopped_by_rate_limit = False
    stopped_by_http_error = False
    current_species = start

    upper = min(base.MAX_POKEDEX + 1, start + base.BATCH_SIZE)
    for number in range(start, upper):
        current_species = number
        species_touched += 1
        print(f"Processando #{number:04d} (ranking exaustivo)")
        targets = base.species_targets(number)
        species_is_complete = True

        for target in targets:
            entry = ensure_entry(entries, by_key, number, target)
            pool = base.candidates(target, index)
            entry["candidateBlueprints"] = len(pool)

            if not pool:
                entry["scanCursor"] = 0
                entry["scanComplete"] = True
                entry["rankingComplete"] = True
                entry["rankingStatus"] = "definitive"
                entry["lookupStatus"] = "not_found"
                continue

            cursor = max(0, min(len(pool), int(entry.get("scanCursor", 0) or 0)))
            collected = list(entry.get("cards", []))
            hard_stop = False

            while cursor < len(pool) and calls < base.MAX_MARKETPLACE_CALLS:
                bp = pool[cursor]
                try:
                    card = base.reference_for(bp)
                except urllib.error.HTTPError as exc:
                    http_errors += 1
                    print(f"Blueprint {bp['id']} HTTP {exc.code}; cursor preservado em {cursor}")
                    if exc.code == 429:
                        stopped_by_rate_limit = True
                    else:
                        stopped_by_http_error = True
                    hard_stop = True
                    break
                except Exception as exc:
                    http_errors += 1
                    stopped_by_http_error = True
                    print(f"Blueprint {bp['id']} falhou: {exc}; cursor preservado em {cursor}")
                    hard_stop = True
                    break

                cursor += 1
                calls += 1
                if card:
                    collected.append(card)
                    observed += 1
                time.sleep(base.MARKETPLACE_DELAY)

            entry["scanCursor"] = cursor
            entry["scanComplete"] = cursor >= len(pool)
            entry["rankingComplete"] = entry["scanComplete"]
            entry["rankingStatus"] = "definitive" if entry["scanComplete"] else "provisional"
            entry["lastLookupAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["cards"] = base.rank(collected)
            entry["lookupStatus"] = (
                "found" if entry["cards"] else
                ("not_found" if entry["scanComplete"] else "pending")
            )

            if not entry["scanComplete"]:
                species_is_complete = False
            if hard_stop or calls >= base.MAX_MARKETPLACE_CALLS:
                species_is_complete = False
                break

        if species_is_complete:
            species_entries = [e for e in entries if int(e.get("speciesNumber", -1)) == number]
            species_is_complete = bool(species_entries) and all(bool(e.get("rankingComplete")) for e in species_entries)

        if species_is_complete:
            species_completed += 1
            progress["nextSpeciesNumber"] = number + 1 if number < base.MAX_POKEDEX else 1
        else:
            progress["nextSpeciesNumber"] = number
            break

        if stopped_by_rate_limit or stopped_by_http_error or calls >= base.MAX_MARKETPLACE_CALLS:
            break

    entries.sort(key=lambda e: (int(e.get("speciesNumber", 9999)), e.get("apiIdentifier") or ""))
    filled = sum(1 for e in entries if e.get("cards"))
    total_cards = sum(len(e.get("cards", [])) for e in entries)
    definitive = sum(1 for e in entries if e.get("rankingComplete"))
    provisional = sum(1 for e in entries if not e.get("rankingComplete"))

    if progress.get("nextSpeciesNumber") == 1 and current_species >= base.MAX_POKEDEX and species_completed:
        progress["mode"] = "complete"
        progress["completedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    else:
        progress["mode"] = "first_pass"

    for obsolete in (
        "stoppedByLigaBlock", "blockedHttpStatus", "blockedEntryName", "resetReason",
        "missingEntriesRemaining", "requestDelaySeconds", "collectorMode", "lastBatchEntriesWithNewCards",
        "lastBatchCardsAdded", "earlyStops", "lastBatchEarlyStops", "maxCallsPerEntry", "earlyValidPerEntry",
    ):
        progress.pop(obsolete, None)

    catalog.update({
        "version": CATALOG_VERSION,
        "updatedAt": time.strftime("%Y-%m-%d"),
        "priceSource": base.SOURCE,
        "sources": [base.SOURCE],
        "currencyPolicy": "Moeda retornada pela CardTrader",
        "priceMethod": "Mediana de até 15 ofertas Near Mint mais baratas por blueprint",
        "catalogScope": "Ranking exaustivo por espécie e forma usando todos os blueprints candidatos da CardTrader",
        "rankingPolicy": "Top 3 só é definitivo após consultar todos os blueprints candidatos da entrada",
        "note": "Entradas com rankingComplete=false são provisórias. Formas Shiny não recebem cartas normais automaticamente.",
        "stats": {
            "entries": len(entries),
            "entriesWithCards": filled,
            "cards": total_cards,
            "rankingsDefinitive": definitive,
            "rankingsProvisional": provisional,
        },
    })

    progress.update({
        "version": PROGRESS_VERSION,
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastBatchSpeciesTouched": species_touched,
        "lastBatchSpeciesCompleted": species_completed,
        "lastBatchMarketplaceCalls": calls,
        "lastBatchCardsObserved": observed,
        "lastBatchHttpErrors": http_errors,
        "stoppedByRateLimit": stopped_by_rate_limit,
        "stoppedByHttpError": stopped_by_http_error,
        "rankingsDefinitive": definitive,
        "rankingsProvisional": provisional,
        "totalRuns": int(progress.get("totalRuns", 0)) + 1,
        "sourcePolicy": "CardTrader-only",
        "parserVersion": PARSER_VERSION,
        "rankingPolicy": "exhaustive-all-candidates",
    })

    base.save(base.CATALOG, catalog)
    base.save(base.PROGRESS, progress)
    print(
        f"CardTrader v4: calls={calls}; espécies tocadas={species_touched}; "
        f"espécies concluídas={species_completed}; rankings definitivos={definitive}; "
        f"provisórios={provisional}; erros HTTP={http_errors}"
    )


if __name__ == "__main__":
    main()
