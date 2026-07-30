# MTranServer Adapter

一个程序同时提供两种功能：

- 直接运行 `MTranServer_Adapter.exe`：启动 OpenAI 兼容翻译接口服务。
- 将 JSON 文件拖到 exe 上：交互选择源语言并批量翻译。
- 执行 `MTranServer_Adapter.exe --batch 文件.json JA`：非交互批量翻译；源语言可使用 `JA`、`EN` 或 `AUTO`。

接口支持普通及 `stream: true` 的 OpenAI Chat Completions 响应格式。批量翻译会合并重复文本请求，并以原子方式保存缓存。

批量工具会自动跳过数字、纯符号、网址、邮箱、文件路径、程序标识符、资源名称、占位符、Ren'Py 标签、许可证元数据和已经是目标语言的内容。API 超时、空结果及错误文本会保留原文，但不会写入翻译缓存。

默认使用 MTranServer 4.0.33 原生 `/translate` 与 `/translate/batch` 接口。访问适配器的 `/health` 可同时查看适配器状态、MTranServer 健康状态与版本。旧的 `/deeplx` 地址仍可配置使用，但批量翻译会回退到并发单句请求。

配置和批量翻译缓存分别保存在 exe 同级的 `config.json` 与 `translation_cache.json`。
