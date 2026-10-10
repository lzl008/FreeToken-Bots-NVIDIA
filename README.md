# FreeToken-Bots-NVIDIA

> 把 **NVIDIA API Catalog 的免费端点**接到 Muse / Pi / Grokbot / Dot 这类子代理上，用 NVIDIA 的免费额度跑 token，不动宿主自己的额度。
>
> 本项目是 [limin112/min-skill · FreeToken-Bots](https://github.com/limin112/min-skill/tree/main/skills/FreeToken-Bots) 的 NVIDIA 改造版。原版做 OpenRouter，这版**只做 NVIDIA**。

---

## 它到底做了什么

原版的思路是"扫 OpenRouter 的 `$0` 模型 → 验证 → 喂给 Pi"。NVIDIA 没有 `pricing` 字段，所以这版把判断改成两条**必须同时成立**的证据：

| 证据 | 来源 | 说明 |
|---|---|---|
| 目录标记 | `data/nvidia_catalog.json`（抓 `build.nvidia.com/models`） | 该模型是否挂在 **Free Endpoint** 上 |
| 实时在线 | `GET https://integrate.api.nvidia.com/v1/models` | 推理 API **现在**是否还在提供它 |

**只有两边都点头，才算"免费可用"。** 只信其中一边是踩坑的主要来源：目录里写着 Free Endpoint 但 API 已经下线（本项目实测有 17 个这种），或者 API 还列着但其实是付费端点。

第三层是 **probe**：真发一次请求。一次普通对话跑通 **不等于** 支持 tool calling —— 那要单独再发一次带 `tools` 的请求。子代理靠工具调用干活，所以两者分开报告。

### 实测现状（2026-10-10）

```
API 在served模型     80
Free Endpoint        21
其中 chat ≥100B 旗舰  6
```

旗舰池（默认 `--min-params 100`）：

| 模型 | 参数量 | 工具调用 |
|---|---|---|
| `moonshotai/kimi-k3` | ~2.8T MoE（卡片原文） | 卡片声明 |
| `z-ai/glm-5.3` | 753B | 卡片声明 |
| `deepseek-ai/deepseek-v4.1-flash` | 552B MoE / 8B active | 卡片声明 |
| `nvidia/nemotron-3-ultra-550b-a55b` | 550B / a55b | 卡片声明 |
| `z-ai/glm-5.3-flash` | 320B-total / 18B-active | 卡片声明 |
| `nvidia/nemotron-3-super-120b-a12b` | 120B / a12b | 卡片声明 |

30–90B 那一档（`muse-glimmer-30b`、`nemotron-3.5-lightning-30b-a3b`、`gemma-4-31b-it`、`llama-3.2-90b-vision-instruct`、`gpt-oss-20b`、`laguna-xs-2.1` …）照样是免费端点，加 `--min-params 20` 就会进 `eligible`。

> ⚠️ **`tool_use` 标注的是"官网卡片声称支持"，不是实测结论。** 真正上线前对要用到的模型跑一次 `probe`，结果会写进报告里。

---

## 快速开始

### 0. 先装 Pi（宿主执行层，缺了这步下面全白搭）

```bash
curl -fsSL https://pi.dev/install.sh | sh          # 或 npm i -g @earendil-works/pi-coding-agent
pi --version
```

### 1. 拿 NVIDIA API Key

去 <https://build.nvidia.com> 注册 → 任一模型页点 **Get API Key**，得到 `nvapi-...`。

```bash
export NVIDIA_API_KEY='nvapi-你的key'     # 别写进 git，别进 shell history
```

> 注意：`/v1/models` 列表接口**不需要 key** 也能读（本项目已核实），但真正调用模型必须要。

### 2. 扫描

```bash
python3 scripts/radar.py scan
python3 scripts/radar.py status            # 看上次结果
```

报告写在 `~/.pi/agent/freetoken-bots.json`（权限 0600）。扫到的免费名单超过 24 小时会在下次 `probe` 时自动重扫 —— 免费资格是动态的，过期名单不作数。

### 3. 验证（强烈建议）

```bash
NVIDIA_API_KEY=... python3 scripts/radar.py probe --model 'z-ai/glm-5.3'
```

输出分别给 `chat_ok` 和 `tools_ok`。**不在最新免费名单里的 ID 一律拒绝探测**（退出码 2）—— 手抄的、记得的、pretty-print 出来的 ID 都不算证据，因为叫错名字可能直接走付费额度。

### 4. 生成给 Pi 的配置提案

```bash
python3 scripts/radar.py sync
```

`sync` **只输出提案，不改 `~/.pi/agent/models.json`**。Pi 各版本的 model schema 不一样，盲写会把你原本能用的配置搞坏 —— 一个没写进去的提案只花你一步手工，写坏一次要排查半天。

---

## 命令一览

```bash
python3 scripts/radar.py scan                 # 扫描并写报告
python3 scripts/radar.py status               # 打印上次报告
python3 scripts/radar.py probe --model ID     # 验证 chat + tools（需 NVIDIA_API_KEY）
python3 scripts/radar.py sync                 # 输出 Pi 配置提案
python3 scripts/radar.py catalog              # 看内置目录概况
python3 scripts/refresh_catalog.py            # 重新抓 build.nvidia.com
python3 scripts/refresh_catalog.py --check    # 只看漂移，不写文件

python3 -m unittest discover -s tests         # 26 个测试，不联网
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--min-params N` | 旗舰阈值，默认 `100`（十亿参数）。扫描/同步都吃这个 |
| `--timeout S` | 单次请求超时 |
| `--no-cache` | 只打印，不写报告 |

环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `NVIDIA_API_KEY` | — | 调用模型必需（扫描不需要） |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | 换推理入口 |
| `PI_HOME` | `~/.pi/agent` | 报告与 `models.json` 的位置 |
| `FREETOKEN_CACHE` | `$PI_HOME/freetoken-bots.json` | 报告路径 |

---

## 报告字段

```jsonc
{
  "provider": "nvidia",
  "served_model_count": 80,        // /v1/models 当前提供的模型数
  "free_endpoint_count": 21,       // 目录标记免费 且 仍在提供
  "eligible": [ … ],               // 免费 + chat + 参数量 ≥ 阈值  → 可用的旗舰池
  "needs_parameter_verification": [ … ],  // 免费 + chat，但卡片没写参数量，不做推断
  "other_free_endpoints": [ … ],   // 免费但不是 chat（embedding/ASR/TTS/安全/翻译…），
                                   //   以及低于阈值的 chat
  "inactive_free_endpoints": [ … ],// 目录里是免费端点，但 API 已不再提供
  "catalog_aliases": { … },        // 目录 ID 与 API ID 的命名差异映射
  "probes": { … },                 // probe 结果缓存
  "free_ids": [ … ]                // probe 的白名单
}
```

---

## 安全纪律（照抄原版，一条没松）

- **免费 ≠ 永久。** 资格动态变化，所以必须每天扫。
- **参数量不做推断。** 不看上下文长度、不看 ID 里的数字、不看模型家族。卡片没写就进 `needs_parameter_verification`，不进可用池。少数官网只写了模型名（`…-550b-a55b`）的，写在目录的 `overrides` 里并注明出处。
- **绝不自动路由到付费模型。** 402 / 额度耗尽 / 429 都不是删掉你现有provider 配置的理由。
- **`probe` fail-closed。** 不在最新名单里就拒发请求。
- **不放 key 进 git，不打印 key 值。**
- **不覆盖用户配置。** `sync` 只出提案；要改先备份、原子写、校验 schema、跑通再留。

---

## 每天必须有一次扫描

**这个 skill 自己不会动。** 免费名单会过期（模型下架、改价、端点撤掉都不通知你）。

- 每天早上 8 点跑一次 `python3 /绝对路径/scripts/radar.py scan`。cron / launchd / systemd timer 三选一，选定就别换。
- cron 例子：`0 8 * * * /usr/bin/python3 /绝对路径/scripts/radar.py scan`
- 每次跑完对比上一版报告，重点看：新上架的免费模型、下架的、不再是免费的、`eligible` 池的变化。
- 目录（`build.nvidia.com`）变化比 API 慢，一周刷一次 `refresh_catalog.py` 就够；`--check` 只看漂移不落盘。

---

## 顺手记录的两个坑

1. **`build.nvidia.com` 对额外请求头有反爬。** 只带 `User-Agent` 能拿到 474 KB 的服务端渲染 HTML；一旦加上 `Accept` / `Accept-Language` 就返回 202 + 一个 2.4 KB 的空壳页。`refresh_catalog.py` 里的注释写明了这点。
2. **目录 ID 和 API ID 命名不一致。** 目录写 `z-ai/glm-5-3`、`nvidia/llama-3_1-nemotron-…`，API 却是 `z-ai/glm-5.3`、`nvidia/llama-3.1-nemotron-…`。字面匹配会静默丢掉 753B 的旗舰模型，所以两边都做了标点归一化（`norm_name`），并把映射记进报告的 `catalog_aliases`。

---

## 目录结构

```
.
├── scripts/
│   ├── radar.py              # 扫描 / 探测 / 提案（纯标准库）
│   └── refresh_catalog.py    # 抓 build.nvidia.com 刷新内置目录
├── data/
│   └── nvidia_catalog.json   # 97 个模型；models 自动生成，overrides 手工维护
├── tests/
│   └── test_radar.py         # 26 个测试，全部离线
├── SKILL.md                  # 给 agent 用的技能说明
└── README.md
```

**只想改人工结论就改 `overrides`**，`models` 每次刷新都会被覆盖。

## License

MIT
