import os
import sys
import time
import uuid
import json
import re
import asyncio
import httpx
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
        "token": "",
        "default_target_lang": "zh",
        "default_source_lang": "auto",
        "timeout": 30
    },
    "openai_compatibility": {
        "model_name": "mtran-translate"
    }
}

def load_config():
    """ 读取或自动生成配置文件 """
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[Warn] 无法创建默认配置文件: {e}", flush=True)
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Error] 读取配置文件失败，使用默认配置: {e}", flush=True)
        return DEFAULT_CONFIG

CONFIG = load_config()

app = FastAPI(title="MTranServer to OpenAI API Adapter")

async def call_mtran_server(text: str, target_lang: str = None, source_lang: str = None) -> str:
    """ 异步转发请求到 MTranServer 的翻译 API """
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
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    # 【Bug修复】添加 flush=True 保证控制台能够即时打印，不被 Python 缓冲区阻塞
    print(f"\n[>>> 转发至 MTranServer] URL: {url}", flush=True)
    print(f"[>>> MTran Payload] {json.dumps(payload, ensure_ascii=False)}", flush=True)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            if isinstance(data, dict):
                res_text = data.get("result") or data.get("translated_text") or data.get("text") or str(data)
            else:
                res_text = str(data)
                
            print(f"[<<< MTran Response] {res_text}", flush=True)
            return res_text
        except Exception as e:
            err_msg = f"[MTranServer 翻译错误: {str(e)}]"
            print(f"[<<< MTran Error] {err_msg}", flush=True)
            return err_msg

def extract_target_lang(prompt_text: str, default_lang: str) -> str:
    """ 从 System Prompt 或用户输入中解析目标语言 """
    # 【Bug修复】补充常用英文语言全称，防止 Prompt 输入 "English/Japanese" 时匹配失败
    lang_map = {
        "中文": "zh", "汉语": "zh", "zh": "zh", "cn": "zh", "chinese": "zh",
        "英文": "en", "英语": "en", "en": "en", "english": "en",
        "日文": "ja", "日语": "ja", "ja": "ja", "japanese": "ja",
        "韩文": "ko", "韩语": "ko", "ko": "ko", "korean": "ko",
        "法文": "fr", "法语": "fr", "fr": "fr", "french": "fr",
        "德文": "de", "德语": "de", "de": "de", "german": "de",
        "俄文": "ru", "俄语": "ru", "ru": "ru", "russian": "ru",
        "西班牙语": "es", "es": "es", "spanish": "es"
    }
    
    match = re.search(r"(?:翻译[为成]|to)\s*([a-zA-Z\u4e00-\u9fa5]+)", prompt_text, re.IGNORECASE)
    if match:
        key = match.group(1).lower()
        if key in lang_map:
            return lang_map[key]
            
    return default_lang

def parse_openai_messages(messages: list) -> tuple[str, str]:
    """ 从 OpenAI 消息中提取文本与目标语言 """
    last_user_msg = ""
    system_prompt = ""
    
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        
        if isinstance(content, list):
            text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
            content_str = "\n".join(text_parts)
        else:
            content_str = str(content)

        if role == "system":
            system_prompt += " " + content_str
        elif role == "user":
            last_user_msg = content_str

    default_target = CONFIG.get("mtran_server", {}).get("default_target_lang", "zh")
    target_lang = extract_target_lang(system_prompt + " " + last_user_msg, default_target)
    
    return last_user_msg, target_lang

@app.get("/v1/models")
async def list_models():
    configured_model = CONFIG.get("openai_compatibility", {}).get("model_name", "mtran-translate")
    print(f"\n[GET /v1/models] 客户端获取模型列表", flush=True)
    return {
        "object": "list",
        "data": [
            {
                "id": configured_model,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mtran"
            }
        ]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    print("\n" + "="*60, flush=True)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 收到新的 /v1/chat/completions 请求", flush=True)

    try:
        body = await request.json()
    except Exception:
        print("[Error] 接收到的请求体非标准 JSON 格式", flush=True)
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})

    messages = body.get("messages", [])
    stream = body.get("stream", False)
    
    default_model = CONFIG.get("openai_compatibility", {}).get("model_name", "mtran-translate")
    model = body.get("model", default_model)
    
    # 提取信息
    text_to_translate, target_lang = parse_openai_messages(messages)
    
    print(f"-> 请求模型: {model} | 流式输出(stream): {stream}", flush=True)
    print(f"-> 待翻译文本: {text_to_translate}", flush=True)
    print(f"-> 识别/设定目标语言: {target_lang}", flush=True)

    if not text_to_translate.strip():
        translated_text = "未检测到需要翻译的内容。"
    else:
        translated_text = await call_mtran_server(text_to_translate, target_lang=target_lang)

    req_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())

    print(f"<- 发送响应结果: {translated_text}", flush=True)
    print("="*60 + "\n", flush=True)

    if stream:
        async def event_generator(res_text: str, req_model: str, r_id: str, c_time: int):
            # 发送角色标识
            first_chunk = {
                "id": r_id, "object": "chat.completion.chunk", "created": c_time, "model": req_model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            # 分段推送文本
            chunk_size = 3
            for i in range(0, len(res_text), chunk_size):
                text_slice = res_text[i:i+chunk_size]
                chunk = {
                    "id": r_id, "object": "chat.completion.chunk", "created": c_time, "model": req_model,
                    "choices": [{"index": 0, "delta": {"content": text_slice}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.01)

            # 结束推送
            stop_chunk = {
                "id": r_id, "object": "chat.completion.chunk", "created": c_time, "model": req_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(translated_text, model, req_id, created_time), 
            media_type="text/event-stream"
        )
    else:
        response_data = {
            "id": req_id,
            "object": "chat.completion",
            "created": created_time,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": translated_text},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": len(text_to_translate),
                "completion_tokens": len(translated_text),
                "total_tokens": len(text_to_translate) + len(translated_text)
            }
        }
        return JSONResponse(content=response_data)

if __name__ == "__main__":
    import uvicorn
    server_cfg = CONFIG.get("server", {})
    uvicorn.run(
        app, 
        host=server_cfg.get("host", "0.0.0.0"), 
        port=server_cfg.get("port", 5000)
    )