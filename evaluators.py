"""Automatic evaluators. Each one gets the example (inputs), what the agent returned (outputs)
and the correct answer (reference_outputs), and returns a score: 1 = good, 0 = bad."""
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

load_dotenv()

JUDGE_MODEL = "gemini-3.8-flash"
judge_llm = init_chat_model(JUDGE_MODEL, model_provider="google_genai")


class Grade(BaseModel):
    reasoning: str = Field(description="One or two sentences explaining the grade")
    correct: bool


def ask_judge(prompt: str) -> Grade:
    # Same automatic retries as the agent, so a busy Gemini does not leave "No feedback"
    judge = judge_llm.with_structured_output(Grade)
    return judge.with_retry(stop_after_attempt=8, wait_exponential_jitter=True).invoke(prompt)


def agent_crashed(outputs: dict, key: str) -> dict | None:
    """If the agent itself failed on this example (for example a 503 from Gemini),
    there are no outputs to check. Give 0 and say why, instead of crashing."""
    if not outputs or "has_error" not in outputs:
        return {"key": key, "score": 0, "comment": "the agent run failed (see the trace)"}
    return None


# ---------- Evaluators that are plain code (no model) ----------
def error_detected(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """Did the agent correctly decide whether there is an error?"""
    crashed = agent_crashed(outputs, "error_detected")
    if crashed:
        return crashed
    correct = outputs["has_error"] == reference_outputs["has_error"]
    return {"key": "error_detected", "score": int(correct)}


def location(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """Did the agent point to the right file and line?"""
    crashed = agent_crashed(outputs, "location")
    if crashed:
        return crashed
    same_file = os.path.basename(outputs["file"]) == reference_outputs["file"]
    same_line = outputs["line"] == reference_outputs["line"]
    return {"key": "location", "score": int(same_file and same_line),
            "comment": f"agent: {outputs['file']} line {outputs['line']}"}


# ---------- Evaluators that use a model as a judge (LLM-as-judge) ----------
ROOT_CAUSE_PROMPT = """You are grading an assistant that analyzes terminal errors.
Does the assistant's root cause describe the same cause as the correct root cause?
Different wording is fine. It is wrong if it names a different cause or is too vague to be useful.

Terminal output:
{output}

Correct root cause: {reference}
Assistant's root cause: {answer}"""

FIX_PROMPT = """You are grading an assistant that suggests fixes for terminal errors.
Would the assistant's fix really solve the error? Compare it to the correct fix.
A different fix that also solves the problem correctly is fine.

Terminal output:
{output}

Correct fix: {reference}

Assistant's fix:
- change this: {current_code}
- to this: {new_code}
- in words: {fix}"""

EXPLANATION_PROMPT = """You are grading an explanation written for a beginner programmer.
Is everything in the explanation factually correct? Mark it wrong if it contains even one false claim
(for example about how Python, the terminal or Windows works).

Terminal output:
{output}

Correct root cause: {reference}

Explanation:
{explanation}

Why the fix works:
{why}"""


def no_error_case(outputs: dict, reference_outputs: dict, key: str) -> dict | None:
    """If there was no error, there is nothing to judge: correct only if the agent also found none."""
    if not reference_outputs["has_error"]:
        return {"key": key, "score": int(not outputs["has_error"])}
    if not outputs["has_error"]:
        return {"key": key, "score": 0, "comment": "the agent missed the error"}
    return None


def root_cause(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    crashed = agent_crashed(outputs, "root_cause")
    if crashed:
        return crashed
    special = no_error_case(outputs, reference_outputs, "root_cause")
    if special:
        return special
    grade = ask_judge(ROOT_CAUSE_PROMPT.format(
        output=inputs["output"], reference=reference_outputs["root_cause"],
        answer=outputs["root_cause"]))
    return {"key": "root_cause", "score": int(grade.correct), "comment": grade.reasoning}


def fix_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    crashed = agent_crashed(outputs, "fix_correct")
    if crashed:
        return crashed
    special = no_error_case(outputs, reference_outputs, "fix_correct")
    if special:
        return special
    grade = ask_judge(FIX_PROMPT.format(
        output=inputs["output"], reference=reference_outputs["fix"],
        current_code=outputs["current_code"], new_code=outputs["new_code"], fix=outputs["fix"]))
    return {"key": "fix_correct", "score": int(grade.correct), "comment": grade.reasoning}


def explanation_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    crashed = agent_crashed(outputs, "explanation_correct")
    if crashed:
        return crashed
    special = no_error_case(outputs, reference_outputs, "explanation_correct")
    if special:
        return special
    grade = ask_judge(EXPLANATION_PROMPT.format(
        output=inputs["output"], reference=reference_outputs["root_cause"],
        explanation=outputs["explanation"], why=outputs["why_it_works"]))
    return {"key": "explanation_correct", "score": int(grade.correct), "comment": grade.reasoning}


ALL_EVALUATORS = [error_detected, location, root_cause, fix_correct, explanation_correct]