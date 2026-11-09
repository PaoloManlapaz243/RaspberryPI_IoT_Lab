import json

from langchain_ollama import ChatOllama
from iot_lab import detection_assistant as da

# OLLAMA_URL   = "http://localhost:11434/api/chat"
# MODEL        = "gemma2:2b"      # try "qwen2.5:3b" if routing is flaky

MODEL = da.MODEL

def _ollama(messages, schema=None, temperature=0.0):
    llm = ChatOllama(model=MODEL, temperature=temperature, format=schema)
    return llm.invoke(messages).content   # LangChain accepts {"role", "content"} dicts


if __name__ == "__main__":
    q = "how many people came by in the last hour?"

    # 1. Raw router call: is the schema respected? (no guards yet)
    raw = _ollama([{"role": "system", "content": da.ROUTER_SYSTEM},
                   {"role": "user", "content": q}], schema=da.ROUTER_SCHEMA)
    print("raw:", raw)
    print("validated:", da.validate_call(json.loads(raw)))   # needs `import json`

    # 2. The whole pipeline, with LangChain as the transport
    da._ollama = _ollama
    print(da.route(q))      # guard -> model -> validate/retry -> guard
    print(da.answer(q))     # + SQL + phrasing
    print(da.answer("how many cats and dogs?"))   # guard declines, no model call