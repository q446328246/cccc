import os
import sys
import time
import uuid
import json
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 确保读取 exe 同级目录下的配置文件
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 5000
    },
    "mtran_server": {
        "url": "http://127.0.0.1:8000/translate",
        "token": "",  # 如果 MTranServer 启用了 Token 校验，请在此填写
        "default_target_lang": "zh",
        "default_source_lang": "auto",
        "timeout": 30
    }
}

def load_config():
    """ 读取或自动生成配置文件 """
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CONFIG

CONFIG = load_config()

app = FastAPI(title="MTranServer to OpenAI API Adapter")

def call_mtran_server(text: str, target_lang: str = None, source_lang: str = None) -> str:
    """ 转发请求到 MTranServer 的翻译 API """
    mtran_cfg = CONFIG.get("mtran_server", {})
    url = mtran_cfg.get("url", "http://127.0.0.1:8000/translate")
    token = mtran_cfg.get("token", "").strip()
    timeout = mtran_cfg.get("timeout", 30)
    target_lang = target_lang or mtran_cfg.get("default_target_lang", "zh")
    source_lang = source_lang or mtran_cfg.get("default_source_lang", "auto")

    payload = {
        "text": text,
        "source_lang": source_lang,
        "target_lang": target_lang
    }
    
    headers = {"Content-Type": "application/json"}
    # 如果配置了 Token，自动加入 Authorization Header
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        
        if isinstance(data, dict):
            return data.get("result") or data.get("translated_text") or data.get("text") or str(data)
        return str(data)
    except Exception as e:
        return f"[MTranServer 翻译错误: {str(e)}]"

def parse_openai_messages(messages: list) -> tuple[str, str]:
    """ 提取 OpenAI 请求中的待翻译文本 """
    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_user_msg = content
            elif isinstance(content, list):
                text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
                last_user_msg = "\n".join(text_parts)
            break
            
    target_lang = CONFIG.get("mtran_server", {}).get("default_target_lang", "zh")
    return last_user_msg, target_lang

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": "mtran-translate", "object": "model", "created": int(time.time()), "owned_by": "mtran"}]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    model = body.get("model", "mtran-translate")
    
    text_to_translate, target_lang = parse_openai_messages(messages)
    translated_text = "未检测到需要翻译的内容。" if not text_to_translate else call_mtran_server(text_to_translate, target_lang=target_lang)

    req_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())

    if stream:
        async def event_generator():
            chunk = {
                "id": req_id, "object": "chat.completion.chunk", "created": created_time, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": translated_text}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            
            stop_chunk = {
                "id": req_id, "object": "chat.completion.chunk", "created": created_time, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        response_data = {
            "id": req_id, "object": "chat.completion", "created": created_time, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": translated_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(text_to_translate), "completion_tokens": len(translated_text), "total_tokens": len(text_to_translate) + len(translated_text)}
        }
        return JSONResponse(content=response_data)

if __name__ == "__main__":
    import uvicorn
    server_cfg = CONFIG.get("server", {})
    uvicorn.run(app, host=server_cfg.get("host", "0.0.0.0"), port=server_cfg.get("port", 5000))