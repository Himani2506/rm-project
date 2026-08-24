"""Benchmark the pipeline at 1k / 10k / 100k rows.

Generates synthetic data with the same defect profile as the supplied dataset
(quoted and apostrophised names, ALL CAPS, ' marks' suffixes, mixed gender
encodings, both grade formats) plus injected duplicates, missing values and
wrong Totals.

    python scripts/benchmark.py
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.cleaning import clean_dataframe  # noqa: E402

NAMES = ["Aarav", "Aditi", "Advika", "Ananya", "Anika", "Arjun", "Aryan", "Diya",
         "Ishaan", "Kabir", "Krishna", "Myra", "Navya", "Reyansh", "Rohan",
         "Saanvi", "Shaurya", "Vihaan", "Zara"]
GENDERS = ["M", "m", "Male", "male", "F", "f", "Female", "female", "0", "1"]


def messy_name(name: str, rng: random.Random) -> str:
    style = rng.random()
    if style < 0.27:
        return f'"{name}"'
    if style < 0.48:
        return f"{name}'"
    if style < 0.72:
        return name.upper()
    return name


def messy_mark(value: int, rng: random.Random) -> str:
    return f"{value} marks" if rng.random() < 0.32 else str(value)


def synthesise(n: int, seed: int = 7) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        name = rng.choice(NAMES)
        marks = [rng.randint(1, 100) for _ in range(3)]
        total = sum(marks)
        if rng.random() < 0.05:          # wrong stated Total
            total += rng.randint(1, 40)
        cells = [messy_mark(m, rng) for m in marks]
        if rng.random() < 0.03:          # missing mark
            cells[rng.randrange(3)] = ""
        grade = rng.randint(1, 12)
        rows.append({
            "Name": messy_name(name, rng),
            "Gender": rng.choice(GENDERS),
            "Grade": f"Grade {grade}" if rng.random() < 0.38 else str(grade),
            "Math": cells[0], "Science": cells[1], "English": cells[2],
            "Total": str(total),
        })
    frame = pd.DataFrame(rows)
    # inject 2% exact duplicates
    duplicates = frame.sample(frac=0.02, random_state=seed)
    return pd.concat([frame, duplicates], ignore_index=True).sample(frac=1, random_state=seed).reset_index(drop=True)


def main() -> None:
    print(f"{'rows':>8} | {'clean ms':>9} | {'insert ms':>10} | {'filter ms':>10} | {'stats ms':>9} | {'out':>7}")
    print("-" * 70)

    for n in (1_000, 10_000, 100_000):
        raw = synthesise(n)

        started = time.perf_counter()
        cleaned, report = clean_dataframe(raw, log_details=False)
        clean_ms = (time.perf_counter() - started) * 1000

        with tempfile.TemporaryDirectory() as tmp:
            import os
            os.environ["RM_DB_PATH"] = str(Path(tmp) / "bench.db")
            import importlib
            importlib.reload(db)
            db.reset()

            started = time.perf_counter()
            db.replace_students(cleaned, report, "benchmark.csv")
            insert_ms = (time.perf_counter() - started) * 1000

            _, filter_ms = db.list_students(min_total=150, shortlist_only=True)
            stats_ms = db.stats(150)["query_ms"]

        print(f"{n:>8,} | {clean_ms:>9.1f} | {insert_ms:>10.1f} | {filter_ms:>10.2f} | {stats_ms:>9.2f} | {report.rows_out:>7,}")

    print("\nMeasured on the machine running this script. Reproduce with: python scripts/benchmark.py")


if __name__ == "__main__":
    main()
