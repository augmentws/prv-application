"""Compile and inspect a staged assessment-synthesis manifest without model calls."""

import argparse
import json
from pathlib import Path
from typing import Any

from app.assessment_synthesis import compile_synthesis_manifest, manifest_summary


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _analyses(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("document_analyses"), list):
        return value["document_analyses"]
    raise ValueError("Analyses JSON must be an array or contain a document_analyses array")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile the deterministic synthesis manifest used by assessment workflow v12."
    )
    parser.add_argument("--analyses", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--retrieval-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    manifest = compile_synthesis_manifest(
        _analyses(_json(arguments.analyses)),
        _json(arguments.coverage),
        _json(arguments.retrieval_plan),
    )
    if arguments.output is not None:
        arguments.output.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(manifest_summary(manifest), indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
