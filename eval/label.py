"""Gold-label collection CLI (BLUEPRINT.md §15 — the one Day-0 task that
only the user can do; Claude Code must not fabricate these labels).

Walks through claim-passage pairs from one or more completed runs and
asks the user to label each one (supported / partial / unsupported /
contradicted), appending to gold_labels.csv. Run this after executing a
handful of real questions via `cli.py run` (needs a real MESH_API_KEY,
and ideally FIRECRAWL_API_KEY so there's real web evidence to label
against, not just NullSearchProvider placeholders).

Usage:
    python eval/label.py <run_id> [<run_id> ...] [--target 100]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from core.config import get_settings
from store.repo import Repo

GOLD_LABELS_PATH = Path(__file__).resolve().parent / "gold_labels.csv"
VALID_LABELS = {"supported", "partial", "unsupported", "contradicted"}


def _already_labeled_claim_ids() -> set[str]:
    if not GOLD_LABELS_PATH.exists():
        return set()
    with open(GOLD_LABELS_PATH, newline="", encoding="utf-8") as fh:
        return {row["claim_id"] for row in csv.DictReader(fh)}


def _append_row(claim_id: str, claim_text: str, passage_id: str, passage_text: str, label: str, notes: str) -> None:
    file_exists = GOLD_LABELS_PATH.exists()
    with open(GOLD_LABELS_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not file_exists:
            writer.writerow(["claim_id", "claim_text", "passage_id", "passage_text", "human_label", "notes"])
        writer.writerow([claim_id, claim_text, passage_id, passage_text, label, notes])


def label_run(repo: Repo, run_id: str, target: int, already: set[str]) -> int:
    data = repo.export_run(run_id)
    passages_by_id = {p["id"]: p for p in data["passages"]}

    labeled_count = 0
    for claim in data["claims"]:
        if labeled_count >= target:
            break
        if claim["id"] in already:
            continue
        passage = passages_by_id.get(claim["passage_id"])
        if passage is None:
            continue

        print("\n" + "=" * 70)
        print(f"CLAIM: {claim['text']}")
        print("-" * 70)
        print(f"PASSAGE: {passage['text'][:800]}")
        print("=" * 70)
        label = input(f"Label ({'/'.join(sorted(VALID_LABELS))}, 's' to skip, 'q' to quit): ").strip().lower()
        if label == "q":
            return labeled_count
        if label == "s" or label not in VALID_LABELS:
            continue
        notes = input("Notes (optional): ").strip()
        _append_row(claim["id"], claim["text"], passage["id"], passage["text"], label, notes)
        already.add(claim["id"])
        labeled_count += 1

    return labeled_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactively label claim-passage pairs for eval/gold_labels.csv")
    parser.add_argument("run_ids", nargs="+", help="One or more run IDs (from `cli.py run`) to pull claims from")
    parser.add_argument("--target", type=int, default=100, help="Stop once this many NEW labels have been collected")
    args = parser.parse_args()

    settings = get_settings()
    repo = Repo(settings.database_url)

    already = _already_labeled_claim_ids()
    total_new = 0
    for run_id in args.run_ids:
        if total_new >= args.target:
            break
        total_new += label_run(repo, run_id, args.target - total_new, already)

    print(f"\nCollected {total_new} new labels. Total in {GOLD_LABELS_PATH.name}: {len(_already_labeled_claim_ids())}.")


if __name__ == "__main__":
    main()
