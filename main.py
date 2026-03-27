#!/usr/bin/env python3
"""
Ratecon Extractor
Usage:
  python main.py path/to/ratecon.pdf
  python main.py path/to/folder/         # batch mode
"""

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from extractor.llm_extractor import extract

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}


def process_file(file_path: str) -> dict:
    print(f"Processing: {file_path}")
    try:
        ratecon = extract(file_path)
        result = ratecon.model_dump()
        print(f"  OK — Load #{result.get('load_number')} | {result.get('total_rate_usd')}")
        return {"status": "ok", "file": file_path, "data": result}
    except ValidationError as e:
        print(f"  VALIDATION ERROR: {e.error_count()} field(s) failed")
        for err in e.errors():
            print(f"    - {' > '.join(str(x) for x in err['loc'])}: {err['msg']}")
        return {"status": "validation_error", "file": file_path, "errors": e.errors()}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"status": "error", "file": file_path, "error": str(e)}


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <file_or_folder>")
        sys.exit(1)

    target = Path(sys.argv[1])
    results = []

    if target.is_file():
        results.append(process_file(str(target)))

    elif target.is_dir():
        files = [
            f for f in sorted(target.iterdir())
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not files:
            print(f"No supported files found in {target}")
            sys.exit(1)

        print(f"Found {len(files)} file(s) in {target}\n")
        for f in files:
            results.append(process_file(str(f)))

    else:
        print(f"Error: '{target}' is not a valid file or folder.")
        sys.exit(1)

    output_file = "results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\nDone: {ok}/{len(results)} successful → {output_file}")


if __name__ == "__main__":
    main()
