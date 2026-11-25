"""Runs the agent on the whole LangSmith dataset and scores it.

    python run_experiment.py v1     # the "before" experiment
    python run_experiment.py v2     # the "after" experiment
"""
import os
import sys
import uuid

from dotenv import load_dotenv
from langgraph.types import Command
from langsmith import Client

from create_dataset import DATASET_NAME
from evaluators import ALL_EVALUATORS
from graph import VERSIONS, build_graph

load_dotenv()


def make_target(version: str):
    """Builds the function LangSmith calls once for every example in the dataset."""
    graph = build_graph()

    def target(inputs: dict) -> dict:
        config = {"configurable": {"thread_id": str(uuid.uuid4()), **VERSIONS[version]}}
        graph.invoke(inputs, config)

        # The graph stops at choose_mode and waits for a person.
        # In an automatic test there is no person, so we always answer "solution":
        # this way the full fix is produced and can be scored.
        if graph.get_state(config).next:
            graph.invoke(Command(resume="solution"), config)

        final = graph.get_state(config).values
        return {
            "has_error": final.get("has_error", False),
            "error_type": final.get("error_type", ""),
            "file": final.get("file_path") or final.get("fix_file", ""),
            "line": final.get("line") or final.get("fix_line", 0),
            "root_cause": final.get("root_cause", ""),
            "explanation": final.get("explanation", ""),
            "current_code": final.get("current_code", ""),
            "new_code": final.get("new_code", ""),
            "fix": final.get("fix", ""),
            "why_it_works": final.get("why_it_works", ""),
            "attempts": final.get("attempts", 0),
        }

    return target


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in VERSIONS:
        print("Usage: python run_experiment.py v1   (or v2)")
        return
    version = sys.argv[1]

    results = Client().evaluate(
        make_target(version),
        data=DATASET_NAME,
        evaluators=ALL_EVALUATORS,
        experiment_prefix=f"code-detective-{version}",
        description=f"Code Detective {version}: {VERSIONS[version]}",
        metadata={"version": version, **VERSIONS[version]},
        max_concurrency=1,      # one example at a time, so the free Gemini tier is not overloaded
    )
    print(f"\nDone: {results.experiment_name}")


if __name__ == "__main__":
    main()