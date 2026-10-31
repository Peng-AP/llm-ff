"""Shared web research for every team, backed by the Exa API. Same engine and same output
format for every model, whatever harness drives it."""

import logging
import os

import httpx
from dotenv import load_dotenv

from aiffl.config import ROOT

EXA = "https://api.exa.ai"
logging.getLogger("httpx").setLevel(logging.WARNING)


def _headers() -> dict:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("EXA_API_KEY")
    if not key:
        raise RuntimeError("EXA_API_KEY is not set in .env")
    return {"x-api-key": key}


def search(query: str, num_results: int = 6, published_after: str | None = None,
           include_domains: list[str] | None = None) -> list[dict]:
    body = {
        "query": query,
        "numResults": max(1, min(num_results, 10)),
        "type": "auto",
        "contents": {"text": {"maxCharacters": 1500}},
    }
    if published_after:
        body["startPublishedDate"] = published_after
    if include_domains:
        body["includeDomains"] = include_domains
    r = httpx.post(f"{EXA}/search", headers=_headers(), json=body, timeout=60)
    r.raise_for_status()
    return [{
        "title": x.get("title"),
        "url": x.get("url"),
        "published": x.get("publishedDate") or "unknown — treat as unverified",
        "author": x.get("author"),
        "excerpt": (x.get("text") or "").strip(),
    } for x in r.json().get("results", [])]


def fetch(url: str, max_chars: int = 10000) -> dict:
    r = httpx.post(f"{EXA}/contents", headers=_headers(),
                   json={"urls": [url], "text": {"maxCharacters": max(500, min(max_chars, 20000))}}, timeout=90)
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return {"url": url, "error": "Could not retrieve this page."}
    x = results[0]
    return {"url": x.get("url", url), "title": x.get("title"),
            "published": x.get("publishedDate") or "unknown — treat as unverified",
            "author": x.get("author"), "text": x.get("text") or ""}
