
# GLaDOS 自动签到（多账号 · 双站点 · 微信/邮箱/浏览器插件推送）

基于 GitHub Actions 的 GLaDOS 自动签到，**代码底座取自 [Devilstore/Gladoscheckin](https://github.com/Devilstore/Gladoscheckin)（社区 486⭐ / 2583 fork，每日 CI 实测可用）**，本仓库在此之上保留 PushPlus 推送、增加「剩余天数告急降级续期」，并对失败诊断做了加强。

## 注册一个 GLaDOS 账号（[注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG)）

原帖主的邀请码：[`0A58E-NV28S-6U3QV-33VMG`](https://0a58e-nv28s-6u3qv-33vmg.glados.space)，来自 [@Devilstore](https://github.com/Devilstore/Gladoscheckin/commits?author=Devilstore)

## 它解决了什么问题

1. **一个账号可能只在一个站有效**：GLaDOS 已分裂出 `glados.cloud` 与 `railgun.info` 两套站点，迁移过的账号只认 railgun 的会话。
   脚本对**每个 Cookie × 每个域名**都尝试签到，由返回码判定归属，你不需要知道账号迁到了哪个站。
2. **服务端做设备绑定**：签到请求的 User-Agent 与会话浏览器不一致会返回 `code=4 / device-mismatch`。
   脚本会读取服务端返回的 `loginDevice`，**自动切换对应平台的 UA 重试**，无需你手动配置 UA。
3. **会话会被服务端吊销**：实测寿命 14~31 天，且**在浏览器重新登录会立刻吊销旧 token**。失效时日志与推送会明确提示 `code=-2`。

## 使用方式

### 1. Fork 本仓库

![1](imgs/1.png)

### 2. 配置 Secrets

`Settings` → `Secrets and variables` → `Actions`：

#### 2.1 `GLADOS_COOKIES`（必填）

**每个账号一个独立无痕窗口**（同一个窗口切换登录多个账号会互相吊销 gld:sess）：

1. 无痕窗口登录 `glados.cloud`（若提示已迁移，改在 `railgun.info` 登录）
2. `F12` → `Network` → 刷新 → 点第一个请求 → `Request Headers` → 复制**完整的 Cookie**
3. 必须同时包含两份，缺 `.sig` 直接失效：

   > `gld:sess=eyJ1c2...; gld:sess.sig=xJkO...`

4. 多账号用 `&` 连接：`账号1的cookie&账号2的cookie&...`

![图片加载失败](imgs/2.png)
![图片加载失败](imgs/3.png)

> 老格式 `koa:sess=...` 已不被新接口接受（服务端直接回 `-2`），如果你还留着它，请按上面的新格式重取。

#### 2.2 `GLADOS_EXCHANGE_PLAN`（可选，默认 `plan500`）

| 值 | 积分要求 | 兑换天数 |
| --- | --- | --- |
| `plan100` | 100 积分 | 10 天 |
| `plan200` | 200 积分 | 30 天 |
| `plan500` | 500 积分 | 100 天（默认） |

#### 2.3 `PUSHPLUS_TOKEN`（可选）

[pushplus](https://www.pushplus.plus/) 用户 token，支持微信服务号 / 邮箱 / 浏览器插件推送。未配置则只在 Actions 日志里输出结果。

> 也兼容 `PUSHDEER_SENDKEY`、`SERVERCHAN_SENDKEY`；配置了几个就同时发几个。

#### 2.4 `GLADOS_VERBOSE`（可选）

设为 `true` 会输出每个请求的完整响应，排查时用。

### 3. 紧急续期（逐级降级）

当某账号**剩余天数 < 3 天**、而积分**没攒到配置档位**时，按「贵的先试」逐级降级，避免积分没攒够就直接断档：

| 条件 | 行为 |
| --- | --- |
| 积分 ≥ 配置档位（默认 500） | 按配置档位兑换（500 分 = 100 天） |
| 剩余 < 3 天 且 积分 ≥ 200 | 降级 **`plan200`**（200 分 = 30 天），推送里标注「紧急续期」 |
| 剩余 < 3 天 且 100 ≤ 积分 < 200 | 再降到 **`plan100`**（100 分 = 10 天） |
| 剩余 < 3 天 且 积分 < 100 | 不兑换，日志 + 推送里 `❗` 告警：无法续期 |
| 剩余 ≥ 3 天 | 攒积分，不兑换 |

降级链只会在「比配置档位更便宜」的计划里挑（配置 `plan500` 时是 `plan200 → plan100`；配置 `plan200` 时只剩 `plan100`；配置 `plan100` 时没有降级空间）。
阈值与降级链在 `checkin.py` 的 `Config.URGENT_DAYS_THRESHOLD` / `Config.URGENT_CASCADE_PLANS` 调整。

### 4. 定时

`18 1,4 * * *`（UTC）= 北京时间 **09:18 / 12:18** 每天两次；也支持在 Actions 页面手动 `workflow_dispatch`。
另外配了 keepalive 步骤，防止 60 天无提交被 GitHub 自动停用定时任务。

## 失败时怎么读日志

| 日志 | 含义 | 处理 |
| --- | --- | --- |
| `🔴 无权限(code=-2)` | 该 Cookie 在这个域名上不存在/已被吊销 | 重新登录该域名，重取 Cookie 更新 Secret |
| `🔴 设备平台不匹配(code=4, loginDevice=…)` | UA 与会话浏览器平台不符，且自动重试仍失败 | 用同一浏览器登录后重取，或在 issue 反馈该平台名 |
| `结果: 签到失败（No permission）` | railgun.info 侧的同类拒绝 | 同上 |
| 全部账号都 -2 | Secret 过期（最常见） | 按 2.1 重取，注意每账号独立窗口 |
| `❗ 剩余 N 天，积分不足 100 无法续期` | 积分见底、即将断档 | 充值/多攒积分或手动兑换 |

**注意**：签到失败**不会**把 Actions 标记成红色（避免刷红），但会在运行摘要的 Annotations 里打 `::error::`，并推送里显示 `❌`。只有脚本自身崩溃才会 exit 1。

## 文件结构

```text
checkin.py            # 签到主脚本（cookie × 域名矩阵 + 设备 UA 自适应 + 推送）
logging_config.py     # 日志（北京时间、[I]/[W]/[E] 短标签）
requirements.txt      # 依赖
.github/workflows/glados.yml   # Actions 配置
```

## 问题排查与定位

在 Actions 里打开 `glados/railgun checkin` 这次运行的日志，按上面的对照表读 `[E]` 开头的行即可。

![图片加载失败](imgs/3.3.png)

![](/imgs/日志检查.png)

## 声明

1. 本项目底座来自 [Devilstore/Gladoscheckin](https://github.com/Devilstore/Gladoscheckin)，原作者与本仓库均不保证稳定运行与更新，因 GitHub 相关规定可能会删库，请注意备份。
2. Cookie 等于账号控制权，只应放进仓库 Secret，不要提交到代码里或贴给别人。
