import os
import sys
import json
import time
import uuid
import httpx
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

# -------------------------------------------------------------------
# 1. 配置文件读取逻辑
# -------------------------------------------------------------------
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 5000,
        "ssl_keyfile": "",
        "ssl_certfile": ""
    },
    "mtran_server": {
        "url": "http://127.0.0.1:8989/translate",
        "batch_size": 32,
        "batch_timeout": 120,
        "batch_retries": 2,
        "cache_checkpoint": 256,
        "token": "sk-whj",
        "default_target_lang": "zh",
        "default_source_lang": "auto",
        "timeout": 30
    },
    "openai_compatibility": {
        "model_name": "DeepSeek"
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] 读取 {CONFIG_FILE} 失败，使用默认配置: {e}")
    return DEFAULT_CONFIG

config = load_config()

SERVER_HOST = config.get("server", {}).get("host", "0.0.0.0")
SERVER_PORT = config.get("server", {}).get("port", 5000)
SSL_KEYFILE = config.get("server", {}).get("ssl_keyfile", "")
SSL_CERTFILE = config.get("server", {}).get("ssl_certfile", "")

MTRAN_CFG = config.get("mtran_server", {})
MTRAN_URL = MTRAN_CFG.get("url", "http://127.0.0.1:8989/translate")
MTRAN_TOKEN = MTRAN_CFG.get("token", "")
DEFAULT_TARGET_LANG = MTRAN_CFG.get("default_target_lang", "zh")
DEFAULT_SOURCE_LANG = MTRAN_CFG.get("default_source_lang", "auto")
TIMEOUT = MTRAN_CFG.get("timeout", 30)

DEFAULT_MODEL_NAME = config.get("openai_compatibility", {}).get("model_name", "DeepSeek")

def get_timestamp():
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def extract_user_text(req_data):
    """提取最后一条用户文本，兼容 OpenAI 文本块格式。"""
    for message in reversed(req_data.get("messages", [])):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
    return str(req_data.get("prompt", ""))


def error_response(status_code, message):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "mtran_server_error"}},
    )


async def stream_completion(request_id, created, model, translated_text):
    chunks = (
        {"role": "assistant"},
        {"content": translated_text},
        {},
    )
    for index, delta in enumerate(chunks):
        payload = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": "stop" if index == 2 else None,
            }],
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# -------------------------------------------------------------------
# 2. 查询模型列表接口 /v1/models (标准 OpenAI 格式)
# -------------------------------------------------------------------
@app.get("/v1/models")
async def list_models():
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL_NAME,
                "object": "model",
                "created": 1700000000,
                "owned_by": "adapter"
            },
            {
                "id": "DeepLX",
                "object": "model",
                "created": 1700000000,
                "owned_by": "adapter"
            }
        ]
    })


@app.get("/health")
async def health():
    """同时检查适配器和上游 MTranServer。"""
    base_url = MTRAN_URL.rsplit("/", 1)[0]
    try:
        async with httpx.AsyncClient(verify=False, timeout=float(TIMEOUT)) as client:
            health_resp, version_resp = await client.get(f"{base_url}/health"), await client.get(f"{base_url}/version")
        health_resp.raise_for_status()
        version_resp.raise_for_status()
        return {"status": "ok", "mtran": health_resp.json(), **version_resp.json()}
    except Exception as exc:
        return error_response(503, f"MTranServer 不可用: {exc}")


# -------------------------------------------------------------------
# 3. 核心接口 /v1/chat/completions (输入输出均为 OpenAI 格式)
# -------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    now_str = get_timestamp()
    
    try:
        # [步骤 1: 接收标准 OpenAI 格式输入]
        req_data = await request.json()
        model_name = req_data.get("model", DEFAULT_MODEL_NAME)
        is_stream = req_data.get("stream", False)

        text_to_translate = extract_user_text(req_data)
        if not text_to_translate.strip():
            return error_response(400, "未检测到需要翻译的文本")

        target_lang = req_data.get("target_lang", DEFAULT_TARGET_LANG).lower()
        source_lang = req_data.get("source_lang", DEFAULT_SOURCE_LANG).lower()

        # 输出控制台日志
        print("=" * 60)
        print(f"{now_str} 收到新的 /v1/chat/completions 请求")
        print(f"-> 请求模型: {model_name} | 流式输出(stream): {is_stream}")
        print(f"-> 待翻译文本: {text_to_translate}")
        print(f"-> 识别/设定目标语言: {target_lang}")

        is_native_api = MTRAN_URL.rstrip("/").endswith("/translate")
        mtran_payload = ({
            "from": source_lang,
            "to": target_lang,
            "text": text_to_translate,
            "html": False,
        } if is_native_api else {
            "text": text_to_translate,
            "source_lang": source_lang.upper(),
            "target_lang": target_lang.upper(),
        })

        headers = {}
        if MTRAN_TOKEN:
            headers["Authorization"] = f"Bearer {MTRAN_TOKEN}"

        print(f"[>>> 转发至 MTranServer] URL: {MTRAN_URL}")
        print(f"[>>> MTran Payload] {mtran_payload}")

        # 发送请求至后端的 deeplx 接口（verify=False 兼容 http 和 https/自签名证书）
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                MTRAN_URL,
                json=mtran_payload,
                headers=headers,
                timeout=float(TIMEOUT)
            )

            if resp.status_code != 200:
                err_msg = f"[MTranServer 响应异常 {resp.status_code}: {resp.text}]"
                print(f"[<<< MTran Error Detail] {err_msg}")
                print(f"<- 发送响应结果: {err_msg}")
                print("=" * 60)
                return error_response(502, err_msg)

            res_json = resp.json()

        # 提取翻译内容
        translated_text = res_json.get("data") or res_json.get("result") or res_json.get("translated_text") or res_json.get("text")
        if translated_text is None:
            return error_response(502, f"MTranServer 响应缺少翻译结果: {res_json}")
        translated_text = str(translated_text)

        # 计算 Token 估算量
        prompt_tokens = len(text_to_translate)
        completion_tokens = len(translated_text)
        total_tokens = prompt_tokens + completion_tokens

        print(f"<- 发送响应结果: {translated_text}")
        print("=" * 60 + "\n")

        print(f"[KGT] {datetime.now().strftime('%H:%M:%S')} [WebSocket] |翻译文本|：{text_to_translate} -> {translated_text} (消耗 {total_tokens} tokens)")

        # [步骤 3: 包装为标准 OpenAI chat.completion 响应输出]
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        if is_stream:
            return StreamingResponse(
                stream_completion(request_id, created, model_name, translated_text),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        openai_response = {
            "id": request_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": translated_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens
            }
        }

        return JSONResponse(content=openai_response)

    except json.JSONDecodeError:
        return error_response(400, "请求体不是有效的 JSON")
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        return error_response(502, f"无法连接 MTranServer: {e}")
    except Exception as e:
        err_detail = f"[MTranServer 响应异常 500: {{\"error\":\"Internal Server Error\",\"message\":\"{str(e)}\"}}]"
        print(f"[<<< MTran Error Detail] {err_detail}")
        print(f"<- 发送响应结果: {err_detail}")
        print("=" * 60)
        return error_response(500, err_detail)

if __name__ == "__main__":
    # 同一个程序支持两种模式：拖入 JSON/传入 --batch 时批量翻译，
    # 无参数（或显式传入 --serve）时启动 OpenAI 兼容服务。
    cli_args = sys.argv[1:]
    if cli_args and cli_args[0] != "--serve":
        explicit_batch = cli_args[0] == "--batch"
        if explicit_batch:
            cli_args = cli_args[1:]
        if not cli_args:
            print("用法: MTranServer_Adapter.exe --batch <JSON文件> [JA|EN|AUTO]")
            raise SystemExit(2)
        from translate_mtranserver import main as batch_translate_main
        source_lang = cli_args[1] if len(cli_args) > 1 else None
        raise SystemExit(batch_translate_main(cli_args[0], source_lang, pause=not explicit_batch))

    uvicorn_kwargs = {
        "app": app,
        "host": SERVER_HOST,
        "port": int(SERVER_PORT)
    }

    # 如果配置了 ssl 路径且文件存在，则开启 HTTPS 支持
    if SSL_KEYFILE and SSL_CERTFILE:
        if os.path.exists(SSL_KEYFILE) and os.path.exists(SSL_CERTFILE):
            uvicorn_kwargs["ssl_keyfile"] = SSL_KEYFILE
            uvicorn_kwargs["ssl_certfile"] = SSL_CERTFILE
            print(f"[*] 部署模式: HTTPS (SSL Enabled)")
        else:
            print(f"[!] 警告: 未找到指定的 SSL 证书文件，降级为 HTTP 模式启动")
    else:
        print(f"[*] 部署模式: HTTP")

    import uvicorn
    uvicorn.run(**uvicorn_kwargs)
