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
    parser.add_argument("--p1s-json", type=Path, help="Previously downloaded official 01P response")
    args = parser.parse_args()

    def fetch(model):
        url = f"https://raw.githubusercontent.com/bambulab/BambuStudio/{args.revision}/resources/hms/hms_en_{model}.json"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        return model, url, payload

    messages = {}
    sources = []
    variants = 0
    model_messages = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        for model, url, payload in pool.map(fetch, FILES):
            data = json.loads(payload)
            count = 0
            model_messages[model] = {}
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
                    model_messages[model][key] = text.strip()
                    count += 1
            sources.append(
                {
                    "model_file": model,
                    "url": url,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "entries": count,
                }
            )
    # P1S uses vendor device prefix 01P. BambuStudio DevHMSQuery.cpp selects
    # the diagnostic catalog using the first three characters of the device ID.
    url = "https://e.bambulab.com/query.php?lang=en&d=01P"
    if args.p1s_json:
        payload = args.p1s_json.read_bytes()
    else:
        request = urllib.request.Request(
            url, headers={"User-Agent": "BambuBridge-catalog-refresh/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    vendor = json.loads(payload)
    if vendor.get("result") != 0:
        raise ValueError("P1S diagnostic catalog unavailable")
    model_messages["01P"] = {}
    for section in ("device_hms", "device_error"):
        for entry in vendor.get("data", {}).get(section, {}).get("en", []):
            key, text = str(entry.get("ecode", "")).lower(), entry.get("intro")
            if (
                len(key) in (8, 16)
                and all(c in "0123456789abcdef" for c in key)
                and isinstance(text, str)
                and text.strip()
            ):
                model_messages["01P"][key] = text.strip()
                messages.setdefault(key, text.strip())
    sources.append(
        {
            "model_file": "01P",
            "url": url,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "vendor_version": vendor.get("ver"),
            "entries": len(model_messages["01P"]),
        }
    )
    model_messages = {
        model: {key: text for key, text in rows.items() if text != messages[key]}
        for model, rows in model_messages.items()
    }
    target = Path(__file__).resolve().parents[1] / "src/bambu_bridge/hms_catalog.json"
    target.write_text(
        json.dumps(
            {
                "revision": args.revision,
                "sources": sources,
                "model_text_variants": variants,
                "messages": messages,
                "model_messages": model_messages,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(len(messages), "English diagnostics frozen")


if __name__ == "__main__":
    main()
