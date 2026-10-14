#!/usr/bin/env python3
"""Bounded async HTTP load test for a local/test Instagram Clone instance."""

import argparse
import asyncio
from dataclasses import dataclass
import statistics
import time

import httpx


@dataclass
class Result:
    status: int | None
    latency_ms: float


async def request_once(client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore) -> Result:
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.get(url)
            return Result(response.status_code, (time.perf_counter() - started) * 1000)
        except httpx.HTTPError:
            return Result(None, (time.perf_counter() - started) * 1000)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, round((len(values) - 1) * fraction))
    return values[index]


async def run(args: argparse.Namespace) -> list[Result]:
    cookies = {}
    if args.cookie:
        name, separator, value = args.cookie.partition("=")
        if not separator or not name or not value:
            raise ValueError("--cookie must be NAME=VALUE")
        cookies[name] = value
    timeout = httpx.Timeout(args.timeout)
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, cookies=cookies) as client:
        semaphore = asyncio.Semaphore(args.concurrency)
        return await asyncio.gather(
            *(request_once(client, args.url, semaphore) for _ in range(args.requests))
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--cookie", help="optional local/test cookie NAME=VALUE")
    args = parser.parse_args()
    if args.requests < 1 or args.requests > 10_000:
        parser.error("--requests must be between 1 and 10000")
    if args.concurrency < 1 or args.concurrency > args.requests:
        parser.error("--concurrency must be between 1 and --requests")

    results = asyncio.run(run(args))
    latencies = [result.latency_ms for result in results]
    successful = sum(result.status is not None and result.status < 500 for result in results)
    print(f"url={args.url} requests={len(results)} concurrency={args.concurrency}")
    print(f"successful={successful} errors={len(results) - successful}")
    print(
        "latency_ms="
        f"mean:{statistics.fmean(latencies):.2f} "
        f"p50:{percentile(latencies, 0.50):.2f} "
        f"p95:{percentile(latencies, 0.95):.2f} "
        f"p99:{percentile(latencies, 0.99):.2f}"
    )


if __name__ == "__main__":
    main()

