import os
import sys
import json
import urllib.request
import urllib.error
import tempfile
import time
from collections import Counter, deque
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# 打包后配置与缓存应位于 exe 同级目录，源码运行时位于脚本目录。
SCRIPT_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
CACHE_PATH = os.path.join(SCRIPT_DIR, "translation_cache.json")

# 全局线程锁，保证多线程修改/保存缓存时的线程安全
cache_lock = Lock()
counter_lock = Lock()

# =========================================================================
# Ren'Py 常用系统 UI 官方/标准翻译词库 (内置极速匹配)
# =========================================================================
RENPY_SYSTEM_DICT = {
    # 基础菜单
    "Start": "开始游戏",
    "Load": "读取存档",
    "Preferences": "偏好设置",
    "Prefs": "设置",
    "About": "关于",
    "Help": "帮助",
    "Quit": "退出游戏",
    "Return": "返回",
    "Back": "返回",
    "Save": "保存存档",
    "Q.Save": "快捷保存",
    "Q.Load": "快捷读取",
    "Quick Save": "快捷保存",
    "Quick Load": "快捷读取",
    "History": "历史履历",
    "Skip": "跳过文本",
    "Auto": "自动播放",
    "Main Menu": "主菜单",
    "End Replay": "结束重播",
    "Scene Replay": "场景重播",
    "Chapter Select": "章节选择",
    "Replay Gallery": "重放画廊",
    "empty slot": "空存档位",
    
    # 偏好设置 / UI
    "Display": "显示模式",
    "Window": "窗口化",
    "Fullscreen": "全屏模式",
    "Transitions": "过渡效果",
    "Text Speed": "文本显示速度",
    "Auto-Forward Time": "自动前进等待时间",
    "Music Volume": "背景音乐音量",
    "Sound Volume": "环境音效音量",
    "Voice Volume": "角色语音音量",
    "Textbox Opacity": "对话框不透明度",
    "Mute All": "全部静音",
    "Joystick": "手柄设置",
    "Gamepad": "游戏手柄",
    "Keyboard": "键盘",
    "Mouse": "鼠标",
    "Enabled": "开启",
    "Disabled": "禁用",
    "Unseen Text": "未读文本",
    "After Choices": "选项之后",
    "Rollback Side": "回滚触发区域",
    "Disable": "禁用",
    "Left": "左侧",
    "Right": "右侧",
    "Show": "显示",
    "Hide": "隐藏",
    "Yes": "是",
    "No": "否",
    
    # 系统询问确认弹窗
    "Are you sure you want to quit?": "确定要退出游戏吗？",
    "Are you sure you want to return to the main menu?": "确定要返回主菜单吗？",
    "Are you sure you want to overwrite your save?": "确定要覆盖此存档吗？",
    "Are you sure you want to load this save?": "确定要读取此存档吗？",
    "Are you sure you want to delete this save?": "确定要删除此存档吗？"
}

ERROR_MARKERS = (
    "[MTranServer", "[翻译失败", "[批量接口异常", "error:",
    "internal server error", "timed out",
)


def is_valid_translation(result):
    """拒绝空结果和已知错误文本，确保错误不会进入缓存。"""
    if not isinstance(result, str) or not result.strip():
        return False
    lowered = result.strip().lower()
    return not any(marker.lower() in lowered for marker in ERROR_MARKERS)


def should_translate(text, source_lang="AUTO", target_lang="ZH"):
    """保守识别自然语言；返回 (是否翻译, 跳过原因)。"""
    value = str(text).strip()
    source = source_lang.upper()
    target = target_lang.upper()
    if not value:
        return False, "空内容"
    if re.fullmatch(r"[\d\W_]+", value, re.UNICODE):
        return False, "数字/符号"
    if re.fullmatch(r"(?:https?://|www\.)\S+", value, re.IGNORECASE) or re.fullmatch(r"\S+@\S+\.\S+", value):
        return False, "网址/邮箱"
    if re.fullmatch(r"(?:[A-Za-z]:)?[\\/\w .-]+\.(?:rpy|rpyc|py|json|xml|html?|css|js|png|jpe?g|webp|gif|svg|ttf|otf|woff2?|mp3|ogg|wav|mp4|webm)", value, re.IGNORECASE):
        return False, "文件路径"
    if re.fullmatch(r"(?:renpy\.[A-Za-z_]\w*\s*)+", value) or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", value):
        return False, "程序标识符"
    if re.fullmatch(r"(?:\{[^{}]*\}|\[[^\[\]]*\]|<[^<>]*>|%\([^)]+\)[#0 +\-\d.]*[a-zA-Z]|%[a-zA-Z]|\\[nrt])+", value):
        return False, "占位符/标签"
    if re.fullmatch(r"\(?\s*[-+]?\d+(?:\.\d+)?(?:\s*,\s*[-+]?\d+(?:\.\d+)?)+\s*\)?", value):
        return False, "数值序列"
    if re.fullmatch(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+", value) and " " not in value:
        return False, "名称/资源标识"
    if re.search(r"\b(?:Font Software|SPDX-License-Identifier|Copyright \(c\)|All Rights Reserved)\b", value, re.IGNORECASE):
        return False, "许可证/元数据"
    has_latin = bool(re.search(r"[A-Za-z]", value))
    has_kana = bool(re.search(r"[\u3040-\u30ff]", value))
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", value))
    if source == "EN" and not has_latin:
        return False, "非英语内容"
    if source == "JA" and not (has_kana or has_cjk):
        return False, "非日语内容"
    if source == "AUTO" and not (has_latin or has_kana or has_cjk):
        return False, "无自然语言"
    if target.startswith("ZH") and source != "JA" and has_cjk and not (has_latin or has_kana):
        return False, "已有中文"
    return True, ""

# 默认配置文件结构（新增 max_workers 线程配置）
DEFAULT_CONFIG = {
  "max_workers": 10,
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
    """读取 config.json，若不存在则创建默认配置"""
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            print(f"[提示] 未找到配置文件，已自动生成默认 config.json")
            return DEFAULT_CONFIG
        except Exception as e:
            print(f"[警告] 创建默认配置文件失败: {e}")
            return DEFAULT_CONFIG

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            # 兼容处理：确保配置中有 max_workers
            if "max_workers" not in cfg:
                cfg["max_workers"] = 10
            return cfg
    except Exception as e:
        print(f"[警告] 读取 config.json 失败 ({e})，将使用默认配置。")
        return DEFAULT_CONFIG

def load_cache():
    """读取本地翻译缓存数据"""
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                print(f"[缓存] 已加载本地剧情翻译缓存 ({len(cache)} 条记录)")
                return cache
        except Exception as e:
            print(f"[警告] 读取缓存文件失败: {e}")
    return {}

def save_cache(cache_data):
    """原子保存缓存，避免程序中断造成缓存文件损坏。"""
    with cache_lock:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=SCRIPT_DIR, delete=False) as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
                temp_path = f.name
            os.replace(temp_path, CACHE_PATH)
            print(f"[缓存] 本地翻译缓存已同步更新！")
        except Exception as e:
            print(f"[警告] 保存缓存文件失败: {e}")
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

def translate_text(text, from_lang, mtran_cfg):
    """调用 API 翻译剧情文本"""
    if not text or not str(text).strip():
        return text

    server_url = mtran_cfg.get("url", "http://127.0.0.1:8989/translate")
    token = mtran_cfg.get("token", "")
    target_lang = mtran_cfg.get("default_target_lang", "zh")
    timeout = mtran_cfg.get("timeout", 30)

    native_api = server_url.rstrip('/').endswith('/translate')
    payload = ({"from": from_lang, "to": target_lang, "text": text, "html": False}
               if native_api else
               {"text": text, "source_lang": from_lang, "target_lang": target_lang})
    
    data_bytes = json.dumps(payload).encode('utf-8')
    headers = {"Content-Type": "application/json"}
    
    if token:
        headers["Authorization"] = token if native_api else f"Bearer {token}"

    req = urllib.request.Request(
        server_url,
        data=data_bytes,
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_body = response.read().decode('utf-8')
            res_json = json.loads(res_body)
            
            if "data" in res_json:
                return res_json["data"]
            elif "text" in res_json:
                return res_json["text"]
            elif "result" in res_json:
                return res_json["result"]
            return text
            
    except Exception as e:
        if token and "Authorization" in headers:
            try:
                alt_url = f"{server_url}?token={token}"
                req_alt = urllib.request.Request(alt_url, data=data_bytes, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req_alt, timeout=timeout) as response:
                    res_json = json.loads(response.read().decode('utf-8'))
                    return res_json.get("data") or res_json.get("text") or res_json.get("result", text)
            except Exception:
                pass

        raise RuntimeError(f"翻译失败: {e}") from e


def translate_batch(texts, from_lang, mtran_cfg):
    """使用 MTranServer 原生批量端点翻译一组文本。"""
    server_url = mtran_cfg.get("url", "")
    if not server_url.rstrip('/').endswith('/translate'):
        return None
    batch_url = server_url.rstrip('/') + '/batch'
    payload = {
        "from": from_lang,
        "to": mtran_cfg.get("default_target_lang", "zh"),
        "texts": texts,
        "html": False,
    }
    headers = {"Content-Type": "application/json"}
    token = mtran_cfg.get("token", "")
    if token:
        headers["Authorization"] = token
    request = urllib.request.Request(
        batch_url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method="POST",
    )
    timeout = mtran_cfg.get("batch_timeout", max(120, mtran_cfg.get("timeout", 30)))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode('utf-8')).get("results")
    if not isinstance(result, list) or len(result) != len(texts):
        raise ValueError("MTranServer 批量响应数量与请求不一致")
    if any(not is_valid_translation(item) for item in result):
        raise ValueError("MTranServer 批量响应包含空值或错误信息")
    return result

def main(file_path=None, source_lang=None, pause=True):
    config = load_config()
    mtran_cfg = config.get("mtran_server", {})
    max_workers = max(1, min(int(config.get("max_workers", 10)), 64))
    cache = load_cache()

    if file_path is None and len(sys.argv) < 2:
        print("\n【错误】请将待翻译的 JSON 文件直接拖拽到本脚本图标（或 .exe）上！")
        if pause:
            input("\n按回车键退出...")
        return 2

    file_path = file_path or sys.argv[1]

    if not os.path.isfile(file_path) or not file_path.lower().endswith('.json'):
        print(f"\n【错误】目标文件不是有效的 JSON 文件：\n{file_path}")
        if pause:
            input("\n按回车键退出...")
        return 2

    dir_name, full_filename = os.path.split(file_path)
    filename, ext = os.path.splitext(full_filename)

    print("=" * 65)
    print("  Ren'Py 智能分流拖拽翻译工具 (系统词库 + 本地缓存 + 多线程 API)")
    print("=" * 65)
    print(f"当前翻译文本: {full_filename}")
    print(f"目标接口地址: {mtran_cfg.get('url')}")
    print(f"并发线程设置: {max_workers} 个线程\n")

    choice = None
    if source_lang:
        from_lang = source_lang.upper()
    else:
        print("请选择剧情文本的源语言类型：")
        print(" [1] 日语 (JA -> ZH)")
        print(" [2] 英语 (EN -> ZH)")
        choice = input("\n请输入对应数字 (1 或 2) 并按回车: ").strip()

    if source_lang:
        print(f"\n已选择源语言：{from_lang}")
    elif choice == '1':
        from_lang = "JA"
        print("\n已选择模式：[日语剧情 -> 中文]")
    elif choice == '2':
        from_lang = "EN"
        print("\n已选择模式：[英语剧情 -> 中文]")
    else:
        print("\n[提示] 输入有误，默认按配置文件/英语处理。")
        from_lang = mtran_cfg.get("default_source_lang", "EN")

    # 读取输入 JSON
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"\n【错误】读取目标 JSON 失败: {e}")
        if pause:
            input("\n按回车键退出...")
        return 1

    total_keys = len(data)
    print(f"\n开始解析文本，共计 {total_keys} 条...")
    print("------------------------------------------------------------")

    translated_data = {}
    lang_prefix = f"[{from_lang}->{mtran_cfg.get('default_target_lang', 'zh')}] "

    sys_hit_count = 0
    cache_hit_count = 0
    smart_skip_reasons = Counter()
    # cache_key -> {value, keys}，相同文本只请求 API 一次。
    api_tasks = {}

    # -------------------------------------------------------------------------
    # 阶段一：快速本地打标与分流（系统词库 & 缓存命中）
    # -------------------------------------------------------------------------
    for key, value in data.items():
        if isinstance(value, str) and value.strip():
            val_strip = value.strip()

            # 1. 判断是否为系统词汇
            if val_strip in RENPY_SYSTEM_DICT:
                translated_data[key] = RENPY_SYSTEM_DICT[val_strip]
                sys_hit_count += 1
            else:
                needs_translation, skip_reason = should_translate(
                    val_strip,
                    from_lang,
                    mtran_cfg.get('default_target_lang', 'zh'),
                )
                if not needs_translation:
                    translated_data[key] = value
                    smart_skip_reasons[skip_reason] += 1
                else:
                    # 2. 判断是否命中剧情缓存；无效旧缓存视为未命中。
                    cache_key = lang_prefix + val_strip
                    if cache_key in cache and is_valid_translation(cache[cache_key]):
                        translated_data[key] = cache[cache_key]
                        cache_hit_count += 1
                    else:
                        cache.pop(cache_key, None)
                        # 3. 收集缺失项，送入 API 队列。
                        task = api_tasks.setdefault(cache_key, {"value": value, "keys": []})
                        task["keys"].append(key)
        else:
            translated_data[key] = value

    api_total_count = len(api_tasks)
    api_value_count = sum(len(task["keys"]) for task in api_tasks.values())
    completed_counter = 0
    new_cache_added = False

    # 显示初步分流统计结果
    smart_skip_count = sum(smart_skip_reasons.values())
    print(f"本地预检完成：系统词库命中 {sys_hit_count} 条 | 智能跳过 {smart_skip_count} 条 | 剧情缓存命中 {cache_hit_count} 条")
    print(f"需要调用网络 API 翻译的唯一文本：{api_total_count} 条\n")

    # -------------------------------------------------------------------------
    # 阶段二：优先使用原生批量接口；兼容接口回退为多线程单句请求
    # -------------------------------------------------------------------------
    if api_total_count > 0:
        new_cache_added = True

        pending_items = list(api_tasks.items())
        native_batch_succeeded = False
        if mtran_cfg.get("url", "").rstrip('/').endswith('/translate'):
            batch_size = max(1, min(int(mtran_cfg.get("batch_size", 32)), 256))
            batch_retries = max(0, min(int(mtran_cfg.get("batch_retries", 2)), 5))
            checkpoint_size = max(1, int(mtran_cfg.get("cache_checkpoint", 256)))
            checkpoint_counter = 0
            fallback_items = []
            batch_queue = deque(
                pending_items[offset:offset + batch_size]
                for offset in range(0, len(pending_items), batch_size)
            )

            while batch_queue:
                chunk = batch_queue.popleft()
                results = None
                last_error = None
                for attempt in range(batch_retries + 1):
                    try:
                        results = translate_batch(
                            [task_data["value"] for _, task_data in chunk],
                            from_lang,
                            mtran_cfg,
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < batch_retries:
                            print(f"\n[批量请求重试 {attempt + 1}/{batch_retries}] {exc}")
                            time.sleep(min(2 ** attempt, 4))

                if results is None:
                    if len(chunk) > 1:
                        middle = len(chunk) // 2
                        print(f"\n[批量请求失败] {last_error}，将 {len(chunk)} 条拆分后继续。")
                        batch_queue.appendleft(chunk[middle:])
                        batch_queue.appendleft(chunk[:middle])
                    else:
                        print(f"\n[单条批量请求失败] {last_error}，稍后改用单句接口。")
                        fallback_items.extend(chunk)
                    continue

                for (cache_key, task_data), translated in zip(chunk, results):
                    cache[cache_key] = translated
                    for result_key in task_data["keys"]:
                        translated_data[result_key] = translated
                completed_counter += len(chunk)
                checkpoint_counter += len(chunk)
                print(f"\rMTranServer 批量翻译进度: [{completed_counter}/{api_total_count}]", end="", flush=True)
                if checkpoint_counter >= checkpoint_size:
                    save_cache(cache)
                    checkpoint_counter = 0

            if checkpoint_counter:
                save_cache(cache)
            pending_items = fallback_items
            native_batch_succeeded = not fallback_items

        def worker(task):
            nonlocal completed_counter
            t_cache_key, task_data = task
            t_val = task_data["value"]

            try:
                res_trans = translate_text(t_val, from_lang, mtran_cfg)
                if not is_valid_translation(res_trans):
                    raise ValueError("翻译结果为空或包含错误信息")
                with cache_lock:
                    cache[t_cache_key] = res_trans
                success = True
            except Exception as exc:
                res_trans = t_val
                success = False
                print(f"\n[翻译失败，不写缓存] '{t_val[:30]}' | {exc}")

            # 线程安全更新进度计数器
            with counter_lock:
                completed_counter += 1
                print(f"\rAPI 多线程翻译进度: [{completed_counter}/{api_total_count}]", end="", flush=True)

            return task_data["keys"], res_trans, success

        if not native_batch_succeeded:
            print(f"\n剩余 {len(pending_items)} 条改用并发单句接口。")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(worker, task) for task in pending_items]
                for future in as_completed(futures):
                    try:
                        res_keys, res_val, _success = future.result()
                        for res_key in res_keys:
                            translated_data[res_key] = res_val
                    except Exception as exc:
                        print(f"\n[线程异常] 任务执行失败: {exc}")

    print("\n\n处理全部完成！")
    print(f"统计明细：")
    print(f" - 自动匹配 Ren'Py 系统标准词库：{sys_hit_count} 条")
    print(f" - 智能识别无需翻译内容：{smart_skip_count} 条")
    if smart_skip_reasons:
        print("   " + " | ".join(f"{reason} {count}" for reason, count in smart_skip_reasons.most_common()))
    print(f" - 读取本地历史剧情缓存：{cache_hit_count} 条")
    print(f" - 网络翻译唯一文本：{api_total_count} 条（覆盖 {api_value_count} 条）")

    # 产生新翻译时同步写回本地 JSON 缓存文件
    if new_cache_added:
        save_cache(cache)

    # 输出翻译文件
    output_path = os.path.join(dir_name, f"{filename}_translated{ext}")
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(translated_data, f, ensure_ascii=False, indent=2)
        print(f"\n[成功] 翻译文件已生成在同级目录：\n{output_path}")
    except Exception as e:
        print(f"\n【错误】写入 JSON 文件失败: {e}")

    if pause:
        input("\n按下回车键退出程序...")
    return 0

if __name__ == "__main__":
    main()
