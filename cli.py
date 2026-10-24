"""Entrypoints: `init-db`, `run`, `resume`, `followup`, `export`,
`replay`."""

from __future__ import annotations

import argparse
import asyncio
import json

from agents.followup import answer_followup
from app.runner import build_deps, resume_run, start_run
from core.config import get_settings
from core.logging import configure_logging
from store.repo import Repo


def init_db() -> None:
    settings = get_settings()
    repo = Repo(settings.database_url)
    repo.create_all()
    print(f"Initialized schema at {settings.database_url}")


def _print_result(repo: Repo, run_id: str) -> None:
    trail = repo.get_trail(run_id)
    report = repo.get_report(run_id)
    print(
        f"\nRun {run_id} — status={trail['status']} stop_reason={trail['stop_reason']} "
        f"iterations={trail['iterations']} cost=${trail['llm_calls']['total_cost_usd']}\n"
    )
    for sentence in report:
        sources = ", ".join(c["url"] for c in sentence["citations"])
        print(f"[{sentence['section']}] {sentence['text']}\n    sources: {sources}\n")


async def _run(question: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    repo = Repo(settings.database_url)
    repo.create_all()

    run_id = await start_run(settings, repo, question)
    _print_result(repo, run_id)


async def _resume(run_id: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    repo = Repo(settings.database_url)
    repo.create_all()

    await resume_run(settings, repo, run_id)
    _print_result(repo, run_id)


async def _followup(run_id: str, question: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    repo = Repo(settings.database_url)
    repo.create_all()
    result = await answer_followup(build_deps(settings, repo), run_id, question)
    for item in result["answer"]:
        print(f"- {item['text']}\n    source: {item['citation']['url']}")
    if result["note"]:
        print(result["note"])
    print(f"({result['dropped']} sentence(s) dropped for failing verification)")


def export_run(run_id: str, out_path: str) -> None:
    settings = get_settings()
    repo = Repo(settings.database_url)
    repo.create_all()
    data = repo.export_run(run_id)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"Exported run {run_id} to {out_path}")


def replay_run(in_path: str) -> None:
    """Loads an exported run (BLUEPRINT.md §13 "Replay mode") into the
    local DB and prints its report/trail — no MESH_API_KEY needed."""
    settings = get_settings()
    repo = Repo(settings.database_url)
    repo.create_all()
    with open(in_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    run_id = repo.import_run(data)
    _print_result(repo, run_id)


def main() -> None:
    parser = argparse.ArgumentParser(prog="deepdive")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

    run_parser = sub.add_parser("run")
    run_parser.add_argument("question")

    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("run_id")

    followup_parser = sub.add_parser("followup")
    followup_parser.add_argument("run_id")
    followup_parser.add_argument("question")

    export_parser = sub.add_parser("export")
    export_parser.add_argument("run_id")
    export_parser.add_argument("out_path")

    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("in_path")

    args = parser.parse_args()
    if args.command == "init-db":
        init_db()
    elif args.command == "run":
        asyncio.run(_run(args.question))
    elif args.command == "resume":
        asyncio.run(_resume(args.run_id))
    elif args.command == "followup":
        asyncio.run(_followup(args.run_id, args.question))
    elif args.command == "export":
        export_run(args.run_id, args.out_path)
    elif args.command == "replay":
        replay_run(args.in_path)


if __name__ == "__main__":
    main()
