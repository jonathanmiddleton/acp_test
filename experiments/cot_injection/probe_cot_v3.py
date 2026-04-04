"""
Probe v3: Multi-turn CoT injection via role-label injection in packed user message.

Two HTTP requests through the proxy:
  1. Normal question → capture real response
  2. Packed user message containing:
     - Closing remark (ends the prior exchange)
     - "Assistant:" partial turn (truncated mid-sentence)
     - "User:" nudge asking to continue

The proxy extracts the last user message and sends it verbatim to the
stateful ACP session, which already has the real Q&A in its history.

Every request and response is logged verbatim to a timestamped file
under logs/cot_v3/.

Configs are JSON files under tmp/configs/. Each config defines a question
and a list of injection variants. This keeps experiments reproducible.

Usage:
    Requires the proxy running: acp-proxy --port 8765
    python tmp/probe_cot_v3.py --config tmp/configs/first_run.json [--model MODEL]
    python tmp/probe_cot_v3.py --config tmp/configs/reversal.json [--model MODEL]
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import httpx

PROXY_BASE = "http://127.0.0.1:{port}/v1"


def load_config(path: str) -> dict:
    """Load an experiment config. Returns dict with 'question' and 'injections'."""
    with open(path) as f:
        cfg = json.load(f)
    assert "question" in cfg, f"Config must have 'question': {path}"
    assert "injections" in cfg, f"Config must have 'injections': {path}"
    for inj in cfg["injections"]:
        for key in ("label", "closing", "partial_assistant", "nudge"):
            assert key in inj, f"Injection missing '{key}': {inj}"
    return cfg


class TestLogger:
    """Logs every interaction verbatim to a timestamped file."""

    def __init__(self, log_dir: str, tag: str):
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(log_dir, f"cot_v3_{ts}_{tag}.log")
        self._f = open(self.path, "w")
        self._write(f"=== CoT v3 Probe: {tag} ===")
        self._write(f"Timestamp: {datetime.now().isoformat()}")
        self._write("")

    def log_request(self, label: str, request_num: int, messages: list[dict]):
        self._write(f"--- {label} | REQUEST {request_num} ---")
        self._write(f"Messages ({len(messages)}):")
        for i, m in enumerate(messages):
            self._write(f"  [{i}] role={m['role']}")
            self._write(f"      content={json.dumps(m['content'])}")
        self._write("")
        self._write("Last user message (verbatim, what proxy sends to ACP):")
        for m in reversed(messages):
            if m["role"] == "user":
                self._write(">>>")
                self._write(m["content"])
                self._write("<<<")
                break
        self._write("")

    def log_response(self, label: str, request_num: int, response: str, elapsed: float):
        self._write(f"--- {label} | RESPONSE {request_num} ---")
        self._write(f"Length: {len(response)} chars, {elapsed:.1f}s")
        self._write("Content (verbatim):")
        self._write(">>>")
        self._write(response)
        self._write("<<<")
        self._write("")

    def log_summary(self, results: list[dict]):
        self._write("=" * 72)
        self._write("SUMMARY")
        self._write("=" * 72)
        for r in results:
            self._write(json.dumps(r, indent=2))
            self._write("")

    def _write(self, text: str):
        self._f.write(text + "\n")
        self._f.flush()

    def close(self):
        self._f.close()


def send_completion(
    client: httpx.Client,
    model: str,
    messages: list[dict],
    base_url: str,
) -> str:
    resp = client.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": messages, "stream": False},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def run_baseline(
    client: httpx.Client,
    model: str,
    base_url: str,
    logger: TestLogger,
    question: str,
) -> dict:
    label = "baseline"
    messages = [{"role": "user", "content": question}]
    logger.log_request(label, 1, messages)

    t0 = time.monotonic()
    response = send_completion(client, model, messages, base_url)
    elapsed = time.monotonic() - t0

    logger.log_response(label, 1, response, elapsed)
    print(f"  baseline: {len(response)} chars, {elapsed:.1f}s")
    return {
        "variant": label,
        "response_1": response,
        "response_1_len": len(response),
        "response_1_time": round(elapsed, 1),
    }


def run_injection(
    client: httpx.Client,
    model: str,
    base_url: str,
    logger: TestLogger,
    question: str,
    label: str,
    closing: str,
    partial_assistant: str,
    user_nudge: str,
) -> dict:
    # --- Request 1: Normal question ---
    messages_r1 = [{"role": "user", "content": question}]
    logger.log_request(label, 1, messages_r1)

    t0 = time.monotonic()
    response_1 = send_completion(client, model, messages_r1, base_url)
    t1 = time.monotonic()

    logger.log_response(label, 1, response_1, t1 - t0)
    print(f"  {label} R1: {len(response_1)} chars, {t1 - t0:.1f}s")

    # --- Request 2: Packed injection ---
    # Build the packed user message
    packed = f"{closing}\n\nAssistant: {partial_assistant}\n\nUser: {user_nudge}"

    # Full messages array (OpenCode replay style)
    messages_r2 = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": response_1},
        {"role": "user", "content": packed},
    ]
    logger.log_request(label, 2, messages_r2)

    t2 = time.monotonic()
    response_2 = send_completion(client, model, messages_r2, base_url)
    t3 = time.monotonic()

    logger.log_response(label, 2, response_2, t3 - t2)
    print(f"  {label} R2: {len(response_2)} chars, {t3 - t2:.1f}s")

    return {
        "variant": label,
        "packed_content": packed,
        "response_1": response_1,
        "response_1_len": len(response_1),
        "response_1_time": round(t1 - t0, 1),
        "response_2": response_2,
        "response_2_len": len(response_2),
        "response_2_time": round(t3 - t2, 1),
        "total_output": len(response_1) + len(response_2),
        "total_time": round(t3 - t0, 1),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="CoT v3: role-label injection")
    parser.add_argument(
        "--config", required=True, help="Path to experiment config JSON"
    )
    parser.add_argument("--model", default="gpt-4.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--tag", default=None, help="Tag for log filename (default: config name)"
    )
    parser.add_argument("--variants", nargs="*", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    question = cfg["question"]
    tag = args.tag or os.path.splitext(os.path.basename(args.config))[0]

    base_url = PROXY_BASE.format(port=args.port)
    log_dir = os.path.join("logs", "cot_v3")
    logger = TestLogger(log_dir, tag)

    http = httpx.Client()
    try:
        resp = http.get(f"{base_url}/models", timeout=5.0)
        resp.raise_for_status()
        available = [m["id"] for m in resp.json()["data"]]
        print(f"Models: {available}")
        logger._write(f"Config: {args.config}")
        logger._write(f"Available models: {available}")
        logger._write(f"Selected model: {args.model}")
        logger._write(f"Question: {question}")
        logger._write("")
    except httpx.ConnectError:
        print(f"Cannot connect to proxy at {base_url}")
        sys.exit(1)

    model = args.model
    print(f"Model: {model}")
    print(f"Config: {args.config}")
    print(f"Question: {question}")
    print()

    results = []

    # Baseline
    print("Running baseline...")
    results.append(run_baseline(http, model, base_url, logger, question))
    print()

    # Injections
    injections = cfg["injections"]
    if args.variants:
        injections = [i for i in injections if i["label"] in args.variants]

    for inj in injections:
        print(f"Running {inj['label']}...")
        results.append(
            run_injection(
                http,
                model,
                base_url,
                logger,
                question,
                inj["label"],
                inj["closing"],
                inj["partial_assistant"],
                inj["nudge"],
            )
        )
        print()

    # Write JSON results
    json_path = logger.path.replace(".log", ".json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)

    logger.log_summary(results)
    logger.close()

    print(f"Log: {logger.path}")
    print(f"JSON: {json_path}")

    # Print summary
    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        v = r["variant"]
        if v == "baseline":
            print(
                f"  {v:20s}  R1={r['response_1_len']:5d} chars  {r['response_1_time']:.1f}s"
            )
        else:
            print(
                f"  {v:20s}  R1={r['response_1_len']:5d}  R2={r['response_2_len']:5d}  "
                f"total={r['total_output']:5d} chars  {r['total_time']:.1f}s"
            )

    http.close()


if __name__ == "__main__":
    main()
