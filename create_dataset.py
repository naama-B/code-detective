"""Builds the evaluation dataset and uploads it to LangSmith.

    python create_dataset.py              # create the dataset (skips if it exists)
    python create_dataset.py --dry-run    # only print the examples, upload nothing
    python create_dataset.py --recreate   # delete the old dataset and create it again
"""
import os
import subprocess
import sys

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

DATASET_NAME = "code-detective-errors"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Each example: the command, how to get its output, and the correct answer.
#   "script": a file in samples/ that we really run to get a real output
#   "args":   other python arguments to really run (e.g. --version)
#   "output": a fixed output, for commands we can't (or don't want to) really run
EXAMPLES = [
    # ---------- Python errors inside a file ----------
    {"script": "samples/name_error.py",
     "answer": {"has_error": True, "error_type": "NameError", "file": "name_error.py", "line": 4,
                "root_cause": "The variable 'totl' is misspelled; the variable that exists is 'total'.",
                "fix": "print(total)"}},
    {"script": "samples/type_error.py",
     "answer": {"has_error": True, "error_type": "TypeError", "file": "type_error.py", "line": 3,
                "root_cause": "A number (age) is joined to strings with +, and Python can only join strings to strings.",
                "fix": 'message = "Hello " + name + ", you are " + str(age) + " years old"'}},
    {"script": "samples/index_error.py",
     "answer": {"has_error": True, "error_type": "IndexError", "file": "index_error.py", "line": 3,
                "root_cause": "The list has 3 items (indexes 0-2), but the code asks for index 3.",
                "fix": 'print("Last grade:", grades[2])  (or grades[-1])'}},
    {"script": "samples/key_error.py",
     "answer": {"has_error": True, "error_type": "KeyError", "file": "key_error.py", "line": 3,
                "root_cause": "The dictionary has no key 'age'.",
                "fix": 'print(student.get("age"))  or add an "age" key to the dictionary'}},
    {"script": "samples/zero_division.py",
     "answer": {"has_error": True, "error_type": "ZeroDivisionError", "file": "zero_division.py", "line": 3,
                "root_cause": "The list is empty, so len(scores) is 0 and the code divides by zero.",
                "fix": "average = total / len(scores) if scores else 0"}},
    {"script": "samples/attribute_error.py",
     "answer": {"has_error": True, "error_type": "AttributeError", "file": "attribute_error.py", "line": 2,
                "root_cause": "Python lists have no 'push' method; the method for adding an item is 'append'.",
                "fix": 'words.append("python")'}},
    {"script": "samples/indentation_error.py",
     "answer": {"has_error": True, "error_type": "IndentationError", "file": "indentation_error.py", "line": 2,
                "root_cause": "The body of the function is not indented under the def line.",
                "fix": '    print("Hello", name)   (indent the line by 4 spaces)'}},
    {"script": "samples/syntax_error.py",
     "answer": {"has_error": True, "error_type": "SyntaxError", "file": "syntax_error.py", "line": 2,
                "root_cause": "The for line is missing a colon at the end.",
                "fix": "for n in numbers:"}},
    {"script": "samples/module_not_found.py",
     "answer": {"has_error": True, "error_type": "ModuleNotFoundError", "file": "module_not_found.py", "line": 1,
                "root_cause": "There is no module called 'requestz'; the name is misspelled (the module is 'requests').",
                "fix": "import requests"}},
    {"script": "samples/file_not_found.py",
     "answer": {"has_error": True, "error_type": "FileNotFoundError", "file": "file_not_found.py", "line": 1,
                "root_cause": "The file data.txt does not exist in the folder the script was run from.",
                "fix": "Create data.txt in that folder, or open the correct path to the file"}},
    {"script": "samples/value_error.py",
     "answer": {"has_error": True, "error_type": "ValueError", "file": "value_error.py", "line": 2,
                "root_cause": "int() cannot convert the text '12.5' because it has a decimal point.",
                "fix": "amount = float(price)"}},
    {"script": "samples/missing_argument.py",
     "answer": {"has_error": True, "error_type": "TypeError", "file": "missing_argument.py", "line": 6,
                "root_cause": "get_discount needs two arguments (price, percent) but is called with only one.",
                "fix": "discount = get_discount(price, 10)   (pass a percent)"}},
    {"script": "samples/missing.py",
     "answer": {"has_error": True, "error_type": "File not found (can't open file)", "file": "", "line": 0,
                "root_cause": "The script samples/missing.py does not exist, so Python cannot open it.",
                "fix": "Check the file name and run an existing file, e.g. python samples/name_error.py"}},

    # ---------- Errors in the command itself (no code file) ----------
    {"command": "pyhton app.py",
     "output": ("pyhton : The term 'pyhton' is not recognized as the name of a cmdlet, function, script file, "
                "or operable program. Check the spelling of the name, or if a path was included, verify that "
                "the path is correct and try again.\n"
                "At line:1 char:1\n+ pyhton app.py\n+ ~~~~~~\n"
                "    + CategoryInfo          : ObjectNotFound: (pyhton:String) [], CommandNotFoundException\n"
                "    + FullyQualifiedErrorId : CommandNotFoundException"),
     "answer": {"has_error": True, "error_type": "Command not recognized", "file": "", "line": 0,
                "root_cause": "'pyhton' is a misspelling of 'python', so PowerShell finds no such command.",
                "fix": "python app.py"}},
    {"command": "pip install pandaz",
     "output": ("ERROR: Could not find a version that satisfies the requirement pandaz (from versions: none)\n"
                "ERROR: No matching distribution found for pandaz"),
     "answer": {"has_error": True, "error_type": "Package not found", "file": "", "line": 0,
                "root_cause": "There is no package called 'pandaz'; the package name is misspelled.",
                "fix": "pip install pandas"}},
    {"command": "git push",
     "output": "fatal: not a git repository (or any of the parent directories): .git",
     "answer": {"has_error": True, "error_type": "Not a git repository", "file": "", "line": 0,
                "root_cause": "The command was run in a folder that is not a git repository.",
                "fix": "cd into the project folder that has the repository (or run git init first)"}},

    # ---------- No error ----------
    {"args": ["--version"], "command": "python --version",
     "answer": {"has_error": False, "error_type": "", "file": "", "line": 0, "root_cause": "", "fix": ""}},
    {"script": "samples/works.py",
     "answer": {"has_error": False, "error_type": "", "file": "", "line": 0, "root_cause": "", "fix": ""}},
]


def get_output(example: dict) -> tuple[str, str]:
    """Returns (command, output) for an example, really running it if needed."""
    if "output" in example:
        return example["command"], example["output"]

    args = [example["script"]] if "script" in example else example["args"]
    run = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=PROJECT_DIR)
    command = example.get("command") or "python " + " ".join(args)
    return command, run.stdout + run.stderr


def build_examples() -> list[dict]:
    examples = []
    for example in EXAMPLES:
        command, output = get_output(example)
        examples.append({
            "inputs": {"command": command, "output": output, "cwd": PROJECT_DIR},
            "outputs": example["answer"],
        })
    return examples


def main():
    examples = build_examples()

    if "--dry-run" in sys.argv:
        for ex in examples:
            print("=" * 70)
            print("COMMAND:", ex["inputs"]["command"])
            print(ex["inputs"]["output"].strip()[-300:])     # the end of the output
            print("ANSWER: ", ex["outputs"]["error_type"] or "(no error)",
                  ex["outputs"]["file"], ex["outputs"]["line"])
        print(f"\n{len(examples)} examples (nothing was uploaded)")
        return

    client = Client()
    if client.has_dataset(dataset_name=DATASET_NAME):
        if "--recreate" not in sys.argv:
            print(f"Dataset '{DATASET_NAME}' already exists. Use --recreate to build it again.")
            return
        client.delete_dataset(dataset_name=DATASET_NAME)
        print(f"Deleted the old dataset '{DATASET_NAME}'.")

    dataset = client.create_dataset(
        dataset_name=DATASET_NAME,
        description="Terminal commands with errors (and without), with the correct analysis.")
    client.create_examples(dataset_id=dataset.id, examples=examples)
    print(f"Created dataset '{DATASET_NAME}' with {len(examples)} examples.")


if __name__ == "__main__":
    main()
