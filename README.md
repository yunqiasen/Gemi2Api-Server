# Gemi2Api-Server
[HanaokaYuzu / Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) 的服务端简单实现

[![pE79pPf.png](https://s21.ax1x.com/2025/04/28/pE79pPf.png)](https://imgse.com/i/pE79pPf)

## 快捷部署

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/zhiyu1998/Gemi2Api-Server)

### HuggingFace（由佬友@qqrr部署）

[![Deploy to HuggingFace](https://img.shields.io/badge/%E7%82%B9%E5%87%BB%E9%83%A8%E7%BD%B2-%F0%9F%A4%97-fff)](https://huggingface.co/spaces/ykl45/gmn2a)

## 直接运行

0. 填配置。现在支持三种 Cookie 方式，推荐顺序：
   1. **`COOKIES_JSON_PATH`**：直接复用 `Gemini-API` 的 `cookies.json`
   2. **`SECURE_1PSID` + `SECURE_1PSIDTS`**：手工填双 Cookie
   3. **都不填**：让底层 `gemini-webapi` 走本机浏览器 Cookie / 本地缓存自动发现（前提是已安装 `browser-cookie3` 且浏览器已登录）
```properties
COOKIES_JSON_PATH = "/absolute/path/to/cookies.json" # 推荐。兼容 Gemini-API CLI 的 cookies.json，多余 Cookie 会一并带上并在运行时回写刷新后的值。
SECURE_1PSID = "COOKIE VALUE HERE"
SECURE_1PSIDTS = "COOKIE VALUE HERE"
GEMINI_SECURE_1PSID = "" # 可选。兼容 Gemini-API 的环境变量命名。
GEMINI_SECURE_1PSIDTS = "" # 可选。兼容 Gemini-API 的环境变量命名。
API_KEY= "sk-your-own-token" # 这是你给本服务设置的 Bearer Token，不是 Google 提供的 Key，可自定义。
TEMPORARY_CHAT = "false" # 使用临时对话模式，此模式会禁用部分功能如思考、图片生成等，默认关闭。
AUTO_DELETE_CHAT = "false" # 低噪音模式建议关闭，避免每次请求额外发 delete 请求。TEMPORARY_CHAT为true时，此项无效。
GEMINI_MAX_CONCURRENT = "1" # 单账号建议保持 1，避免同一 IP / 同一会话并发过高触发风控。
PUBLIC_BASE_URL = "" # 本地测试请留空；只有挂了反向代理/公网域名时才填写外部地址。
GEMINI_SKIP_VERIFY = "false" # 可选。仅在你明确知道证书校验有问题时才打开。
```
1. `uv` 安装一下依赖
> uv init
> 
> uv add fastapi uvicorn gemini-webapi httpx h2

> [!NOTE]  
> 如果存在`pyproject.toml` 那么就使用下面的命令：  
> uv sync

或者 `pip` 也可以

> pip install fastapi uvicorn gemini-webapi httpx h2

2. 激活一下环境
> source venv/bin/activate

3. 启动（推荐直接让 `uvicorn` 读取 `.env`）
> uvicorn main:app --env-file .env --reload --host 127.0.0.1 --port 8000

> [!NOTE]
> 当前 `main.py` 是直接读取 `os.environ`，不会自动加载 `.env`。如果不用 `--env-file .env`，那就需要先手动 `source .env` 再启动。

> [!NOTE]
> 运行时会同步维护三类 Cookie 缓存：
> - `secrets/.cached_1psidts_<psid>.txt`：兼容旧逻辑的 1PSIDTS 缓存
> - `secrets/.cached_cookies_<psid>.json`：`gemini-webapi` 保存的完整 Google Cookie 缓存
> - `COOKIES_JSON_PATH`：如果你配置了它，服务会把刷新后的 Cookie 回写到这个 JSON

> [!WARNING] 
> tips: 如果不填写 API_KEY ，那么就直接使用

## 使用Docker运行（推荐）

### 快速开始

1. 克隆本项目
   ```bash
   git clone https://github.com/zhiyu1998/Gemi2Api-Server.git
   ```

2. 创建 `.env` 文件并填入你的 Gemini Cookie 凭据:
   ```bash
   cp .env.example .env
   # 用编辑器打开 .env 文件，填入你的 Cookie 值
   ```

3. 启动服务:
   ```bash
   docker-compose up -d
   ```

4. 服务将在 http://0.0.0.0:8000 上运行

### 其他 Docker 命令

```bash
# 查看日志
docker-compose logs

# 重启服务
docker-compose restart

# 停止服务
docker-compose down

# 重新构建并启动
docker-compose up -d --build
```

## API端点

- `GET /`: 服务状态检查
- `GET /v1/models`: 获取可用模型列表
- `POST /v1/chat/completions`: 与模型聊天 (类似OpenAI接口)
- `GET /gemini-proxy/image`: 图片代理接口（有生成图片需求时，需要保证此端点可直接访问，如果使用反向代理则需要填写`PUBLIC_BASE_URL`环境变量）
- `GET /gemini-proxy/media`: 视频/音频媒体代理接口

## 模型暴露说明（按源码）

### `/v1/models` 的暴露逻辑

- 服务端优先返回 `GeminiClient.list_models()` 动态发现的**当前账号真实可用模型**
- 如果动态获取失败，才回退到 `gemini_webapi.constants.Model` 里的静态枚举
- 动态模型返回字段包含：
  - `id`
  - `display_name`
  - `description`
  - `advanced_only`

### 静态回退模型（源码内置）

如果当前账号的动态模型列表取不到，服务会回退为下面这 9 个静态模型：

- `gemini-3-pro`
- `gemini-3-flash`
- `gemini-3-flash-thinking`
- `gemini-3-pro-plus`
- `gemini-3-flash-plus`
- `gemini-3-flash-thinking-plus`
- `gemini-3-pro-advanced`
- `gemini-3-flash-advanced`
- `gemini-3-flash-thinking-advanced`

### 文本 / 生图 / 视频 / 音频模型怎么区分

- **文本模型**：源码里静态枚举基本都是通用文本 / 推理模型，上面这 9 个都属于这一类
- **生图 / 视频 / 音频模型**：当前服务**不在源码里写死具体模型名**
- 服务端只做两件事：
  1. `/v1/models` 把账号动态返回的模型原样暴露出来
  2. `map_model_name()` 会根据模型名 / 显示名 / 描述里的关键词做匹配，关键词包括：
     - `vision` / `image`
     - `video` / `veo`
     - `audio` / `music`

也就是说：

- 你的账号如果实际开放了**生图模型**，它会出现在 `/v1/models`
- 你的账号如果实际开放了**视频模型**，它也会出现在 `/v1/models`
- 项目本身支持把这些结果透出：
  - 图片结果走 `/gemini-proxy/image`
  - 视频 / 音频结果走 `/gemini-proxy/media`

### 查看自己账号当前到底暴露了哪些模型

```bash
curl -s http://127.0.0.1:8000/v1/models | python3 -m json.tool
```

## 接口使用示例

### 文本模型

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-own-token' \
  -d '{
    "model": "gemini-3-flash",
    "messages": [
      {"role": "user", "content": "Reply with exactly OK."}
    ]
  }' | python3 -m json.tool
```

### 流式文本

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-own-token' \
  -d '{
    "model": "gemini-3-flash",
    "stream": true,
    "messages": [
      {"role": "user", "content": "用三句话介绍你自己"}
    ]
  }'
```

### 生图模型

如果你的账号在 `/v1/models` 里动态暴露了 image / vision 类模型，可以直接这样请求：

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-own-token' \
  -d '{
    "model": "你的生图模型ID",
    "messages": [
      {"role": "user", "content": "Generate a cyberpunk cat image"}
    ]
  }' | python3 -m json.tool
```

返回里会出现：

```markdown
![🎨 Loading image...](http://127.0.0.1:8000/gemini-proxy/image?... )
```

### 视频 / 音频模型

如果你的账号动态暴露了 video / veo / audio / music 类模型，请求方式一样：

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-own-token' \
  -d '{
    "model": "你的视频或音频模型ID",
    "messages": [
      {"role": "user", "content": "Generate a short video of a cat playing"}
    ]
  }' | python3 -m json.tool
```

返回里会出现：

```markdown
[🎬 Video 1](http://127.0.0.1:8000/gemini-proxy/media?... )
[🎵 Media Audio 1](http://127.0.0.1:8000/gemini-proxy/media?... )
```

## 下载与去水印说明（按源码）

### 图片

- `GET /gemini-proxy/image` 会走 `remove_watermark=True`
- 当前只对这些格式执行去水印：
  - `image/png`
  - `image/jpeg`
  - `image/webp`
- 也就是说：**如果你下载的是服务端返回的代理图片链接，那么保存到本地的是去水印后的版本**

示例下载：

```bash
curl -L 'http://127.0.0.1:8000/gemini-proxy/image?url=...&sig=...' -o image.png
```

### 视频 / 音频

- `GET /gemini-proxy/media` 只是流式透传媒体内容
- **不会做视频去水印**
- **不会做音频去水印**

示例下载：

```bash
curl -L 'http://127.0.0.1:8000/gemini-proxy/media?url=...&sig=...' -o video.mp4
```

## 上游 Gemini-API 原本写了什么

上游 [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) 的 README 和源码里，明确写了这些原生能力：

- 初始化后会后台自动刷新 `__Secure-1PSIDTS`
- `GeminiClient.generate_content()`：单轮问答
- `GeminiClient.start_chat()` / `ChatSession.send_message()`：多轮对话
- `GeminiClient.list_models()`：动态列出当前账号可用模型
- `Image.save()`：保存图片到本地
- `GeneratedVideo.save()`：保存视频到本地
- `GeneratedMedia.save(download_type=\"audio\" | \"video\" | \"both\")`：保存音频/视频到本地
- `cli.py` 内置命令：
  - `ask`
  - `reply`
  - `research`
  - `list`
  - `read`
  - `models`
  - `download`
  - `inspect`

上游 CLI 示例（原 README 有写）：

```bash
python cli.py --cookies-json cookies.json ask "What is quantum computing?"
python cli.py --cookies-json cookies.json reply c_abc123 "Tell me more"
python cli.py --cookies-json cookies.json models
python cli.py --cookies-json cookies.json download "https://..." -o output.png
python cli.py --cookies-json cookies.json inspect
```

## 常见问题

### 服务器报 500 问题解决方案

500 的问题一般是 IP 不太行 或者 请求太频繁（后者等待一段时间或者重新新建一个隐身标签登录一下重新导出 Cookie 即可）。当前版本已经接入了 Gemini-API 的多来源 Cookie 逻辑：
- 手工双 Cookie（`SECURE_1PSID` / `SECURE_1PSIDTS`）
- `COOKIES_JSON_PATH`
- `gemini-webapi` 的完整 Cookie 缓存
- 浏览器自动读取（底层 `browser-cookie3`）

运行期间依赖 `gemini-webapi` 后台自动刷新 `__Secure-1PSIDTS`，同时会把最新值同步到 `secrets/.cached_1psidts_<psid>.txt`、`secrets/.cached_cookies_<psid>.json`，如果你配置了 `COOKIES_JSON_PATH`，还会把刷新后的 Cookie 回写到该 JSON。见 issue：
- [__Secure-1PSIDTS · Issue #6 · HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API/issues/6)
- [Failed to initialize client. SECURE_1PSIDTS could get expired frequently · Issue #72 · HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API/issues/72)

更推荐的解决步骤：
1. 使用隐身标签访问 [Google Gemini](https://gemini.google.com/) 并登录
2. 导出完整 `cookies.json`（或至少重新复制最新的 `__Secure-1PSID` / `__Secure-1PSIDTS`）
3. 优先把 `.env` 改成 `COOKIES_JSON_PATH=/absolute/path/to/cookies.json`
4. 如果仍异常，再清掉本项目 `secrets/.cached_1psidts_*` 与 `secrets/.cached_cookies_*`
5. 重新启动服务：`docker-compose up -d --build`

## 致谢

- 图片去水印算法基于 [journey-ad/gemini-watermark-remover](https://github.com/journey-ad/gemini-watermark-remover)以及[allenk/GeminiWatermarkTool](https://github.com/allenk/GeminiWatermarkTool)实现，并直接使用了其中的两张png图片。

## 贡献

同时感谢以下开发者对 `Gemi2Api-Server` 作出的贡献：

<a href="https://github.com/zhiyu1998/Gemi2Api-Server/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=zhiyu1998/Gemi2Api-Server&max=1000" />
</a>
