"""A throwaway support-ticket triage bot: the kind of app semula is for.

Three LLM calls per ticket: classify (text), look up the order (tool call, executed here),
draft a reply (streamed). Every run injects a timestamp and a temp path into the system prompt
and shuffles the tool list, the noise that defeats naive caches.
"""
import json, os, random, sys, tempfile
from datetime import datetime, timezone
from openai import OpenAI

BASE = os.environ.get("SEMULA_BASE", "http://127.0.0.1:18080/v1/projects/triage")
client = OpenAI(base_url=BASE, api_key=os.environ.get("SEMULA_API_KEY", "unused"), timeout=120)

TICKET = os.environ.get("TICKET") or (
    "Order #A-1042 arrived with a cracked screen. I already paid with the key "
    "sk-" + "ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE in your portal; please refund me."
)
TOOLS = [
    {"type": "function", "function": {"name": "lookup_order", "description": "Fetch an order",
     "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"]}}},
    {"type": "function", "function": {"name": "escalate", "description": "Hand to a human",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"]}}},
]

def system() -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scratch = tempfile.mkdtemp(prefix="triage-")
    return (f"You are a support triage bot. Current time: {now}. "
            f"Scratch directory: {scratch}. Reply tersely.")

def main() -> None:
    sysmsg = system()
    msgs = [{"role": "system", "content": sysmsg},
            {"role": "user", "content": f"Classify this ticket as JSON with a 'category' field:\n{TICKET}"}]
    first = client.chat.completions.create(model="gpt-4o-mini", messages=msgs, temperature=random.random())
    category = first.choices[0].message.content
    print("1. classify  ->", category)

    msgs.append({"role": "assistant", "content": category})
    msgs.append({"role": "user", "content": "Find the order this ticket is about."})
    tools = TOOLS[:] ; random.shuffle(tools)
    second = client.chat.completions.create(model="gpt-4o-mini", messages=msgs, tools=tools)
    call = second.choices[0].message.tool_calls[0]
    args = json.loads(call.function.arguments)
    print("2. tool call ->", call.function.name, args)
    order = {"order_id": args["order_id"], "status": "delivered", "total": "129.00"}
    msgs.append(second.choices[0].message.model_dump(exclude_none=True))
    msgs.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(order)})

    print("3. reply     -> ", end="", flush=True)
    stream = client.chat.completions.create(model="gpt-4o-mini", messages=msgs, tools=tools, stream=True)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()

if __name__ == "__main__":
    main()
