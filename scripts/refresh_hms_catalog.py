"""Freeze BambuStudio's complete English HMS/device-error descriptions.
Run explicitly with --revision; no runtime network dependency.
"""

import argparse
import concurrent.futures
import hashlib
import json
import urllib.request
from pathlib import Path

FILES = ["094", "093", "20P", "22E", "239", "26A", "31B"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    def fetch(model):
        url = f"https://raw.githubusercontent.com/bambulab/BambuStudio/{args.revision}/resources/hms/hms_en_{model}.json"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        return model, url, payload

    messages = {}
    sources = []
    variants = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        for model, url, payload in pool.map(fetch, FILES):
            data = json.loads(payload)
            count = 0
            for section in ("device_hms", "device_error"):
                for entry in data.get("data", {}).get(section, {}).get("en", []):
                    key = str(entry.get("ecode", "")).lower()
                    text = entry.get("intro")
                    if (
                        len(key) not in (8, 16)
                        or not all(c in "0123456789abcdef" for c in key)
                        or not isinstance(text, str)
                        or not text.strip()
                    ):
                        continue
                    if key in messages and messages[key] != text:
                        variants += 1
                    messages.setdefault(key, text.strip())
                    count += 1
            sources.append(
                {
                    "model_file": model,
                    "url": url,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "entries": count,
                }
            )
    target = Path(__file__).resolve().parents[1] / "src/bambu_bridge/hms_catalog.json"
    target.write_text(
        json.dumps(
            {
                "revision": args.revision,
                "sources": sources,
                "model_text_variants": variants,
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(len(messages), "English diagnostics frozen")


if __name__ == "__main__":
    main()
