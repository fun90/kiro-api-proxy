#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx


async def measure(client: httpx.AsyncClient, url: str, payload: dict) -> tuple[float, float]:
    started = time.perf_counter()
    first = None
    async with client.stream("POST", url, json=payload) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if chunk and first is None:
                first = time.perf_counter()
    finished = time.perf_counter()
    return (first or finished) - started, finished - started


async def main() -> None:
    parser = argparse.ArgumentParser(description="Kiro 代理性能基准")
    parser.add_argument("--base-url", default="http://127.0.0.1:3458")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--requests", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    payload = {
        "model": args.model,
        "stream": True,
        "messages": [{"role": "user", "content": "只回复 OK"}],
    }
    limits = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(headers=headers, timeout=900) as client:
        async def one() -> tuple[float, float]:
            async with limits:
                return await measure(
                    client, f"{args.base_url}/v1/chat/completions", payload
                )

        values = await asyncio.gather(*(one() for _ in range(args.requests)))
    ttft = [item[0] for item in values]
    total = [item[1] for item in values]
    print(
        json.dumps(
            {
                "requests": args.requests,
                "concurrency": args.concurrency,
                "ttft_seconds": {
                    "min": min(ttft),
                    "median": statistics.median(ttft),
                    "max": max(ttft),
                },
                "total_seconds": {
                    "min": min(total),
                    "median": statistics.median(total),
                    "max": max(total),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
