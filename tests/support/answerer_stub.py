"""A stand-in for a real answerer command: reads the request JSON on stdin and does what the
last user message asks, so a test can make it succeed, fail, hang or misbehave. Never a model."""

import json
import os
import sys
import time

raw = sys.stdin.read()
data = json.loads(raw)
messages = data["request"]["messages"]
text = str(messages[-1]["content"])
shape = data.get("shape") or {}

log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(f"{os.getpid()} attempt={data.get('attempt')} {text[:20]}\n")


def reply(payload: object) -> None:
    sys.stdout.write(payload if isinstance(payload, str) else json.dumps(payload))
    sys.exit(0)


def bad_tool() -> None:
    reply({"message": {"tool_calls": [{"function": {"name": "no_such_tool", "arguments": {}}}]}})


if text.startswith("FAIL"):
    sys.exit(3)
if text.startswith("SLEEP"):
    pidfile = os.environ.get("STUB_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as handle:
            handle.write(str(os.getpid()))
    time.sleep(60)
if text.startswith("FORK"):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["STUB_PIDFILE"], "w") as handle:
        handle.write(f"{os.getpid()} {child.pid}")
    time.sleep(60)
if text.startswith("BIG"):
    sys.stdout.write("x" * 2_000_000)
    sys.exit(0)
if text.startswith("NOTHING"):
    sys.exit(0)
if text.startswith("BADJSON"):
    reply("{not json")
if text.startswith("ENV"):
    reply({"content": json.dumps({"env": sorted(os.environ), "cwd_empty": not os.listdir(".")})})
if text.startswith("ARGV"):
    reply(
        {
            "content": json.dumps(
                {"argv": sys.argv[1:], "prompt_in_argv": text in " ".join(sys.argv)}
            )
        }
    )
if text.startswith("STUBBORN"):
    bad_tool()
if text.startswith("TOOL"):
    if "rejection" in data:
        calls = shape["tools"][0]["skeleton"]["message"]["tool_calls"]
        reply({"message": {"content": None, "tool_calls": calls}})
    bad_tool()
if text.startswith("SKELETON"):
    reply(shape["tools"][0]["skeleton"])
if text.startswith("FENCED"):
    reply('```json\n{"content": "from a fence"}\n```')
if text.startswith("PLAIN"):
    reply("just words")
reply({"content": f"pid={os.getpid()} cwd={os.getcwd()}"})
