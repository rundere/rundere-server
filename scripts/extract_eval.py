#!/usr/bin/env python3
"""Run the extractor chain over stored ingest payloads and report results.

Local evaluation harness for the real captured corpus (OQ-08 / D-10
measurement). Reads *.json files each containing one ingest_batches.payload
value. NOT a test — real PII flows through here; output stays local.

    python scripts/extract_eval.py --dir /corpus \
        --ollama http://host.docker.internal:11434 --model qwen3:14b
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extract.llm import OllamaExtractor
from extract.pipeline import extract_ingest_payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--ollama", default=None, help="Ollama base URL; omit to skip LLM")
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    llm = OllamaExtractor(args.ollama, args.model, args.timeout) if args.ollama else None
    files = sorted(f for f in Path(args.dir).glob("*.json")
                   if not f.name.startswith("eval-"))
    if args.limit:
        files = files[: args.limit]

    stats = {"ok": 0, "needs_review": 0}
    by_extractor: dict[str, int] = {}
    rows = []
    for f in files:
        payload = json.loads(f.read_text())
        subject = (payload.get("subject") or f.name)[:60]
        t0 = time.time()
        r = extract_ingest_payload(payload, llm=llm)
        dt = time.time() - t0
        if r.ok:
            stats["ok"] += 1
            by_extractor[r.extractor] = by_extractor.get(r.extractor, 0) + 1
            items = r.batch["items"]
            kinds = ",".join(i["item_type"] for i in items)
            retried = len(r.llm_attempts) > 1
            print(f"[ok]     {subject}")
            print(f"         extractor={r.extractor} items={len(items)} ({kinds}) "
                  f"{dt:.1f}s{' RETRIED' if retried else ''}")
            rows.append({"file": f.name, "subject": subject, "ok": True,
                         "extractor": r.extractor, "seconds": round(dt, 1),
                         "retried": retried, "items": items})
        else:
            stats["needs_review"] += 1
            print(f"[REVIEW] {subject}")
            for reason in r.reasons:
                print(f"         {reason[:200]}")
            for a in r.llm_attempts:
                if a.get("errors"):
                    print(f"         attempt {a['attempt']}: {a['errors'][:3]}")
            rows.append({"file": f.name, "subject": subject, "ok": False,
                         "reasons": r.reasons, "seconds": round(dt, 1)})

    print(f"\n=== {stats['ok']}/{len(files)} extracted ok, "
          f"{stats['needs_review']} needs_review; by extractor: {by_extractor}")
    out = Path(args.dir) / f"eval-{(args.model if llm else 'nollm').replace(':','_').replace('/','_')}.json"
    out.write_text(json.dumps({"stats": stats, "by_extractor": by_extractor,
                               "rows": rows}, indent=1, default=str))
    print(f"detail -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
