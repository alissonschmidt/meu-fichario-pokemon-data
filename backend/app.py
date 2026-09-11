from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

GITHUB_REPO = os.getenv("GITHUB_REPO", "alissonschmidt/meu-fichario-pokemon-data")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_WORKFLOW = os.getenv("GITHUB_WORKFLOW", "search-cardtrader-on-demand.yml")
GITHUB_DISPATCH_TOKEN = os.getenv("GITHUB_DISPATCH_TOKEN", "").strip()
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "604800"))
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}"

app = FastAPI(
    title="Pokémon Card Ranking API",
    version="1.0.0",
    description="Busca sob demanda do Top 3 de cartas via CardTrader, com GitHub Actions como worker.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class CreateSearchRequest(BaseModel):
    speciesNumber: int = Field(ge=1, le=1025)
    apiIdentifier: str | None = None
    pokemonName: str = Field(min_length=1, max_length=100)
    forceRefresh: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def safe_key(value: str | None) -> str:
    if not value:
        return "normal"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "form"


def result_key(species: int, api_identifier: str | None) -> str:
    return f"{species:04d}-{safe_key(api_identifier)}"


def job_id_for(species: int, api_identifier: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"cs_{species:04d}_{safe_key(api_identifier)}_{stamp}_{secrets.token_hex(3)}"


async def raw_json(path: str) -> dict[str, Any] | None:
    url = f"{RAW_BASE}/{path}"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url, headers={"Cache-Control": "no-cache"})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def error(code: str, message: str, retryable: bool, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "retryable": retryable}},
    )


async def dispatch_search(job_id: str, body: CreateSearchRequest) -> None:
    if not GITHUB_DISPATCH_TOKEN:
        raise error(
            "BACKEND_NOT_CONFIGURED",
            "GITHUB_DISPATCH_TOKEN não configurado no backend.",
            False,
            503,
        )
    url = f"{API_BASE}/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
    payload = {
        "ref": GITHUB_BRANCH,
        "inputs": {
            "job_id": job_id,
            "species_number": str(body.speciesNumber),
            "api_identifier": body.apiIdentifier or "",
            "pokemon_name": body.pokemonName,
            "force_refresh": str(body.forceRefresh).lower(),
        },
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_DISPATCH_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PokeBinder-Backend/1.0",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code != 204:
        raise error(
            "WORKFLOW_DISPATCH_FAILED",
            f"GitHub Actions recusou o disparo: HTTP {response.status_code}",
            response.status_code >= 500,
            503,
        )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "pokemon-card-ranking-api",
        "githubRepo": GITHUB_REPO,
        "workflow": GITHUB_WORKFLOW,
        "dispatchConfigured": bool(GITHUB_DISPATCH_TOKEN),
        "time": now_iso(),
    }


@app.post("/api/v1/card-searches")
async def create_search(body: CreateSearchRequest, response: Response) -> dict[str, Any]:
    key = result_key(body.speciesNumber, body.apiIdentifier)
    cached = await raw_json(f"results/{key}.json")
    if cached and cached.get("rankingComplete") is True and not body.forceRefresh:
        searched = parse_iso(cached.get("searchedAt"))
        if searched is not None:
            age = (datetime.now(timezone.utc) - searched).total_seconds()
            if age <= CACHE_TTL_SECONDS:
                return {
                    "jobId": None,
                    "status": "completed",
                    "speciesNumber": body.speciesNumber,
                    "apiIdentifier": body.apiIdentifier,
                    "pokemonName": cached.get("pokemonName", body.pokemonName),
                    "rankingComplete": True,
                    "cacheHit": True,
                    "cachedAt": cached.get("searchedAt"),
                    "resultUrl": f"/api/v1/pokemon/{body.speciesNumber}/cards/top" + (
                        f"?apiIdentifier={body.apiIdentifier}" if body.apiIdentifier else ""
                    ),
                }

    job_id = job_id_for(body.speciesNumber, body.apiIdentifier)
    await dispatch_search(job_id, body)
    response.status_code = 201
    response.headers["Location"] = f"/api/v1/card-searches/{job_id}"
    return {
        "jobId": job_id,
        "status": "queued",
        "speciesNumber": body.speciesNumber,
        "apiIdentifier": body.apiIdentifier,
        "pokemonName": body.pokemonName,
        "rankingComplete": False,
        "cacheHit": False,
        "createdAt": now_iso(),
        "statusUrl": f"/api/v1/card-searches/{job_id}",
        "resultUrl": f"/api/v1/card-searches/{job_id}/result",
    }


@app.get("/api/v1/card-searches/{job_id}")
async def search_status(job_id: str = Path(min_length=1, max_length=160)) -> dict[str, Any]:
    payload = await raw_json(f"jobs/{job_id}.json")
    if payload is None:
        return {
            "jobId": job_id,
            "status": "queued",
            "rankingComplete": False,
            "rankingStatus": "provisional",
            "phase": "resolving_pokemon",
            "progress": {
                "percent": 0.0,
                "processedBlueprints": 0,
                "candidateBlueprints": 0,
                "validPriceReferences": 0,
                "remainingBlueprints": 0,
            },
            "currentTop3": [],
            "updatedAt": now_iso(),
        }
    return payload


@app.get("/api/v1/card-searches/{job_id}/result")
async def search_result(job_id: str, response: Response) -> dict[str, Any]:
    job = await raw_json(f"jobs/{job_id}.json")
    if job is None:
        response.status_code = 202
        response.headers["Retry-After"] = "5"
        return {
            "jobId": job_id,
            "status": "queued",
            "rankingComplete": False,
            "message": "A busca ainda não iniciou no worker.",
        }
    if job.get("status") == "failed":
        err = job.get("error") or {}
        raise error(
            str(err.get("code") or "SEARCH_FAILED"),
            str(err.get("message") or "A busca terminou com falha."),
            bool(err.get("retryable", True)),
            409,
        )
    if job.get("status") != "completed":
        response.status_code = 202
        response.headers["Retry-After"] = "5"
        return {
            "jobId": job_id,
            "status": job.get("status", "running"),
            "rankingComplete": False,
            "message": "A busca ainda está em andamento.",
        }
    key = job.get("resultKey")
    if not key:
        raise error("SEARCH_FAILED", "Job concluído sem chave de resultado.", True, 409)
    result = await raw_json(f"results/{key}.json")
    if result is None:
        raise error("SEARCH_FAILED", "Resultado ainda não foi publicado.", True, 409)
    return result


@app.get("/api/v1/pokemon/{species_number}/cards/top")
async def pokemon_top_cards(
    species_number: int = Path(ge=1, le=1025),
    api_identifier: str | None = Query(default=None, alias="apiIdentifier"),
) -> dict[str, Any]:
    key = result_key(species_number, api_identifier)
    result = await raw_json(f"results/{key}.json")
    if result is None:
        return {
            "speciesNumber": species_number,
            "apiIdentifier": api_identifier,
            "pokemonName": f"Pokémon #{species_number:04d}",
            "hasResult": False,
            "rankingComplete": False,
            "searchedAt": None,
            "expiresAt": None,
            "ageSeconds": None,
            "stale": True,
            "top3": [],
        }
    searched = parse_iso(result.get("searchedAt"))
    age = int((datetime.now(timezone.utc) - searched).total_seconds()) if searched else None
    stale = age is None or age > CACHE_TTL_SECONDS
    return {
        "speciesNumber": species_number,
        "apiIdentifier": api_identifier,
        "pokemonName": result.get("pokemonName", f"Pokémon #{species_number:04d}"),
        "hasResult": True,
        "rankingComplete": result.get("rankingComplete") is True,
        "searchedAt": result.get("searchedAt"),
        "expiresAt": result.get("cachedUntil"),
        "ageSeconds": age,
        "stale": stale,
        "top3": result.get("top3", []),
    }
