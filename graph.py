import os
import re
import subprocess
import sys
import time
import uuid
from typing import TypedDict

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

import prompts

load_dotenv()

MODEL_NAME = "gemini-3.8-flash"
llm = init_chat_model(MODEL_NAME, model_provider="google_genai")
# The same model, told to "think" less before answering: faster and fewer tokens (used by v3)
fast_llm = init_chat_model(MODEL_NAME, model_provider="google_genai", thinking_level="low")

# Errors that mean "the server is busy, try again later". Any other error is a real
# problem (wrong key, wrong model name...) and is shown at once instead of retried.
BUSY_SIGNS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")
MAX_TRIES = 8


def ask_model(prompt: str, config: RunnableConfig, schema=None):
    """Sends a prompt to the model.
    - With a schema, the answer comes back in that shape (structured output).
    - If the server is busy, waits 1s, 2s, 4s, 8s ... and tries again (up to 8 times)."""
    model = fast_llm if settings(config)["fast_thinking"] else llm
    if schema:
        model = model.with_structured_output(schema)

    for attempt in range(MAX_TRIES):
        try:
            return model.invoke(prompt)
        except Exception as error:
            busy = any(sign in str(error) for sign in BUSY_SIGNS)
            if not busy or attempt == MAX_TRIES - 1:
                raise                      # a real error, or we tried enough: show it
            time.sleep(2 ** attempt)       # wait 1, 2, 4, 8 ... seconds

# ---------- Versions: the same graph, with different settings ----------
# v1 = the "before" version: simple word search for errors, does not read the code, no reviewer.
# v2 = improved after looking at the v1 traces:
#      precise error patterns, reads the code, reviewer + loop, stricter prompts.
# v3 = improved after looking at the v2 traces:
#      keeps the student's intent, checks commands really work, and is faster and cheaper
#      (the model thinks less, and the loop runs at most one extra time).
VERSIONS = {
    "v1": {"read_code": False, "use_critique": False, "max_fix_attempts": 1,
           "smart_detect": False, "better_prompts": False, "intent_rules": False,
           "fast_thinking": False},
    "v2": {"read_code": True, "use_critique": True, "max_fix_attempts": 3,
           "smart_detect": True, "better_prompts": True, "intent_rules": False,
           "fast_thinking": False},
    "v3": {"read_code": True, "use_critique": True, "max_fix_attempts": 2,
           "smart_detect": True, "better_prompts": True, "intent_rules": True,
           "fast_thinking": True},
}


def settings(config: RunnableConfig) -> dict:
    """The settings of this run. If no version was given, use the newest (v3)."""
    return {**VERSIONS["v3"], **(config or {}).get("configurable", {})}


def language(config: RunnableConfig) -> str:
    """The language for explanations. The app asks for Hebrew; experiments stay in English."""
    return settings(config).get("language", prompts.LANGUAGE)


# ---------- State: the data that moves between the nodes ----------
class State(TypedDict, total=False):
    command: str          # the command that was run
    output: str           # what the command printed
    cwd: str              # the folder the command ran in
    has_error: bool       # did an error happen?
    file_path: str        # the file where the error happened (if found)
    line: int             # the line number (if found)
    code_snippet: str     # the code around that line
    error_type: str       # e.g. NameError
    root_cause: str       # one sentence: what caused it
    explanation: str      # simple explanation for the student
    mode: str             # the student's choice: "self" or "solution"
    hint: str             # a hint, if she chose to fix it herself
    fix_file: str         # file to change
    fix_line: int         # line to change
    current_code: str     # what is written there now
    new_code: str         # what to write instead
    fix: str              # what to change, in words
    why_it_works: str     # why the fix solves the problem
    attempts: int         # how many times suggest_fix ran
    critique_passed: bool # did the reviewer approve the fix?
    feedback: str         # the reviewer's comments


# ---------- The shape of the answers we want from the model ----------
class Analysis(BaseModel):
    error_type: str = Field(description="Name of the error, e.g. NameError")
    root_cause: str = Field(description="One sentence: what exactly caused the failure")
    explanation: str = Field(description="Simple explanation for a beginner, 2-4 sentences")


class FixSuggestion(BaseModel):
    file: str = Field(description="File to change, empty if the fix is a command")
    line: int = Field(description="Line number to change, 0 if the fix is a command")
    current_code: str = Field(description="Exactly what is written there now")
    new_code: str = Field(description="Exactly what to write instead")
    fix: str = Field(description="What needs to change, 1-2 sentences")
    why_it_works: str = Field(description="Why this fix solves the root cause")


class Critique(BaseModel):
    passed: bool = Field(description="True only if the fix passes all checks")
    feedback: str = Field(description="Short, specific feedback if it did not pass")


# ---------- Helper ----------
def code_section(state: State) -> str:
    """The code part of a prompt: the code snippet if we have one, otherwise nothing."""
    if state.get("code_snippet"):
        return prompts.CODE_SECTION.format(code_snippet=state["code_snippet"])
    return ""


# ---------- Nodes ----------
# v1: search for words anywhere in the output (simple, but "0 errors found" looks like an error)
ERROR_SIGNS = ["traceback", "error", "exception", "is not recognized", "fatal:", "failed"]

# v2: search for the real shapes that error messages have
ERROR_PATTERNS = [
    r"Traceback \(most recent call last\)",      # a Python traceback
    r"^\s*\w*(Error|Exception)\b.*:",               # a line like "NameError: ..."
    r"is not recognized as",                        # PowerShell: unknown command
    r"^fatal:",                                     # git
    r"^ERROR:",                                     # pip
    r"can't open file",                             # python: script not found
    r"No such file or directory",
    r"^\s*\+ CategoryInfo\s*:",                      # PowerShell's own errors (cd, Get-ChildItem...)
    r"is not a git command",                        # git with a typo, e.g. "git comit"
    r"Unable to connect to the server",             # kubectl: cannot reach the cluster
    r"^error( |:)",                                 # kubectl / docker / npm: "error: ..."
    r"Cannot connect to the Docker daemon|error during connect|Error response from daemon",  # docker
    r"npm ERR!",                                    # npm (older versions)
]


def detect_error(state: State, config: RunnableConfig) -> dict:
    output = state["output"]
    if settings(config)["smart_detect"]:
        has_error = any(re.search(p, output, re.MULTILINE | re.IGNORECASE)
                        for p in ERROR_PATTERNS)
    else:
        has_error = any(sign in output.lower() for sign in ERROR_SIGNS)
    return {"has_error": has_error}


def parse_error(state: State) -> dict:
    # Python tracebacks contain lines like:  File "C:\...\app.py", line 12
    matches = re.findall(r'File "(.+?)", line (\d+)', state["output"])

    # Keep only the student's own files (not Python's or a library's internal files)
    own_files = [(path, line) for path, line in matches
                 if "site-packages" not in path and not path.startswith("<")]
    if not own_files:
        return {"file_path": "", "line": 0}

    path, line = own_files[-1]          # the last one is closest to the error
    if not os.path.isabs(path):         # a relative path -> join it with the folder
        path = os.path.join(state.get("cwd", ""), path)
    return {"file_path": path, "line": int(line)}


def read_source(state: State) -> dict:
    with open(state["file_path"], encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    error_line = state["line"]
    start = max(1, error_line - 5)
    end = min(len(lines), error_line + 5)

    snippet = []
    for number in range(start, end + 1):
        mark = ">>" if number == error_line else "  "
        snippet.append(f"{mark} {number:4} | {lines[number - 1].rstrip()}")
    return {"code_snippet": "\n".join(snippet)}


def analyze(state: State, config: RunnableConfig) -> dict:
    s = settings(config)
    extra_rules = ""
    if s["better_prompts"]:
        extra_rules += prompts.ANALYZE_RULES_V2
    if s["intent_rules"]:
        extra_rules += prompts.ANALYZE_RULES_V3
    prompt = prompts.ANALYZE_PROMPT.format(
        command=state["command"], output=state["output"],
        code_section=code_section(state), language=language(config),
        extra_rules=extra_rules)
    result = ask_model(prompt, config, Analysis)
    return {"error_type": result.error_type,
            "root_cause": result.root_cause,
            "explanation": result.explanation}


def choose_mode(state: State) -> dict:
    # The graph STOPS here and waits for the student's answer.
    # When it continues, interrupt() returns that answer.
    answer = interrupt({
        "error_type": state["error_type"],
        "explanation": state["explanation"],
        "file": state.get("file_path", ""),
        "line": state.get("line", 0),
        "question": "Do you want to fix it yourself (self) or get the full solution (solution)?",
    })
    mode = answer if answer in ("self", "solution") else "solution"
    return {"mode": mode}


def give_hint(state: State, config: RunnableConfig) -> dict:
    prompt = prompts.HINT_PROMPT.format(
        command=state["command"], output=state["output"],
        code_section=code_section(state), root_cause=state["root_cause"],
        language=language(config))
    return {"hint": ask_model(prompt, config).text}


def suggest_fix(state: State, config: RunnableConfig) -> dict:
    s = settings(config)
    extra_rules = ""
    if s["better_prompts"]:
        extra_rules += prompts.FIX_RULES_V2
    if s["intent_rules"]:
        extra_rules += prompts.FIX_RULES_V3
    feedback_section = ""
    if state.get("feedback"):
        feedback_section = prompts.FEEDBACK_SECTION.format(feedback=state["feedback"])

    prompt = prompts.FIX_PROMPT.format(
        command=state["command"], output=state["output"],
        code_section=code_section(state), root_cause=state["root_cause"],
        language=language(config), feedback_section=feedback_section,
        extra_rules=extra_rules)
    result = ask_model(prompt, config, FixSuggestion)
    return {"fix_file": result.file,
            "fix_line": result.line,
            "current_code": result.current_code,
            "new_code": result.new_code,
            "fix": result.fix,
            "why_it_works": result.why_it_works,
            "attempts": state.get("attempts", 0) + 1}


def critique(state: State, config: RunnableConfig) -> dict:
    extra_checks = prompts.CRITIQUE_CHECKS_V3 if settings(config)["intent_rules"] else ""
    prompt = prompts.CRITIQUE_PROMPT.format(
        output=state["output"], code_section=code_section(state),
        root_cause=state["root_cause"], file=state["fix_file"], line=state["fix_line"],
        current_code=state["current_code"], new_code=state["new_code"],
        extra_checks=extra_checks)
    result = ask_model(prompt, config, Critique)
    return {"critique_passed": result.passed,
            "feedback": "" if result.passed else result.feedback}


# ---------- Routing functions: decide where to go next ----------
def route_after_detect(state: State) -> str:
    return "parse_error" if state["has_error"] else END


def route_after_parse(state: State, config: RunnableConfig) -> str:
    if not settings(config)["read_code"]:
        return "analyze"
    if state["file_path"] and os.path.exists(state["file_path"]):
        return "read_source"
    return "analyze"


def route_after_choice(state: State) -> str:
    return "give_hint" if state["mode"] == "self" else "suggest_fix"


def route_after_fix(state: State, config: RunnableConfig) -> str:
    return "critique" if settings(config)["use_critique"] else END


def route_after_critique(state: State, config: RunnableConfig) -> str:
    max_attempts = settings(config)["max_fix_attempts"]
    if not state["critique_passed"] and state["attempts"] < max_attempts:
        return "suggest_fix"    # the loop: try again with the reviewer's feedback
    return END


# ---------- Building the graph ----------
def build_graph(checkpointer=None):
    builder = StateGraph(State)

    builder.add_node("detect_error", detect_error)
    builder.add_node("parse_error", parse_error)
    builder.add_node("read_source", read_source)
    builder.add_node("analyze", analyze)
    builder.add_node("choose_mode", choose_mode)
    builder.add_node("give_hint", give_hint)
    builder.add_node("suggest_fix", suggest_fix)
    builder.add_node("critique", critique)

    builder.add_edge(START, "detect_error")
    builder.add_conditional_edges("detect_error", route_after_detect, ["parse_error", END])
    builder.add_conditional_edges("parse_error", route_after_parse, ["read_source", "analyze"])
    builder.add_edge("read_source", "analyze")
    builder.add_edge("analyze", "choose_mode")
    builder.add_conditional_edges("choose_mode", route_after_choice, ["give_hint", "suggest_fix"])
    builder.add_edge("give_hint", END)
    builder.add_conditional_edges("suggest_fix", route_after_fix, ["critique", END])
    builder.add_conditional_edges("critique", route_after_critique, ["suggest_fix", END])

    # A checkpointer saves the State between steps. Without it, interrupt() cannot work.
    return builder.compile(checkpointer=checkpointer or MemorySaver())


# ---------- Quick test: runs only when you run this file directly ----------
def ask_student(question: dict) -> str:
    print("\n" + "-" * 70)
    print(f"PAUSED. Error: {question['error_type']}")
    if question["file"]:
        print(f"Where:  {question['file']}  line {question['line']}")
    print(f"\n{question['explanation']}\n")
    while True:
        choice = input("[1] I'll fix it myself   [2] Give me the solution   > ").strip()
        if choice in ("1", "2"):
            return "self" if choice == "1" else "solution"


def run_and_show(graph, inputs: dict, title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)

    # Each run gets its own thread_id, so the checkpointer knows which run to continue
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    next_input = inputs

    while True:
        for update in graph.stream(next_input, config):
            for node in update:
                if node != "__interrupt__":
                    print(f"  -> {node}")

        snapshot = graph.get_state(config)
        if not snapshot.next:                 # nothing left to run: the graph finished
            final = snapshot.values
            break
        # Otherwise the graph is paused on interrupt(): ask the student and continue
        question = snapshot.tasks[0].interrupts[0].value
        answer = ask_student(question)
        next_input = Command(resume=answer)

    if not final.get("has_error"):
        print("\nNo error found.")
    elif final["mode"] == "self":
        print(f"\nHINT:\n{final['hint']}")
    else:
        print(f"\nSOLUTION ({final['attempts']} attempt(s), approved: {final['critique_passed']}):")
        if final["fix_file"]:
            print(f"  File:       {final['fix_file']}  line {final['fix_line']}")
        print(f"  Now:        {final['current_code']}")
        print(f"  Change to:  {final['new_code']}")
        print(f"  Why:        {final['why_it_works']}")


if __name__ == "__main__":
    graph = build_graph()
    project_dir = os.path.dirname(os.path.abspath(__file__))

    # Test 1: a real broken script
    run = subprocess.run([sys.executable, "samples/name_error.py"],
                         capture_output=True, text=True, cwd=project_dir)
    run_and_show(graph, {"command": "python samples/name_error.py",
                         "output": run.stdout + run.stderr,
                         "cwd": project_dir},
                 "TEST 1: error inside a file")

    # Test 2: a typo in the command
    run_and_show(graph, {"command": "pyhton app.py",
                         "output": "pyhton : The term 'pyhton' is not recognized as the name of a "
                                   "cmdlet, function, script file, or operable program.",
                         "cwd": project_dir},
                 "TEST 2: wrong command, no file")

    # Test 3: a command that worked
    run_and_show(graph, {"command": "python --version",
                         "output": "Python 3.12.0",
                         "cwd": project_dir},
                 "TEST 3: no error")