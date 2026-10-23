 
# Glados自动签到,微信/邮箱/浏览器插件 推送结果

## 重要声明：本帖源自--->本站项目[原贴链接](https://github.com/Devilstore/Gladoscheckin)，我仅就推送相关内容作了部分更改。

## 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG))

## 原帖主的邀请码：([0A58E-NV28S-6U3QV-33VMG](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 来自原帖主@[Devilstore](https://github.com/Devilstore/Gladoscheckin/commits?author=Devilstore)

1. ###  **Fork**本仓库

![1](imgs/1.png)


2. ### 添加secret

 跳转至                                                                                                                                                的仓库的`Settings`->`Secrets and variables`->`Action`
添加1个`repository secret`，命名为`GLADOS_COOKIES`，其值对应GLaDOS账号的cookie值中的有效部分（获取方式如下）

- 在GLaDOS的签到页面按`F12`

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 点击第一个选项卡后在`Request Headers`下找到`Cookie`，右键复制cookie的值即可

  > 参考格式：koa:sess=eyJ1c2xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxAwMH0=; koa:sess.sig=xJkOxxxxxxxxxxxxxxxtnM;

![图片加载失败](imgs/3.png)

- 多账号请在 `COOKIES` 中 添加多个 `cookies` 中间使用 `&`连接即可。（例如： `c1&c3&c3...`）

- 配置积分兑换策略（非必须）

- 添加1个`repository secret`，命名为`GLADOS_EXCHANGE_PLAN`，配置自动兑换积分策略：

| 值        | 积分要求 | 兑换天数      |
| --------- | -------- | ------------- |
| `plan100` | 100 积分 | 10 天         |
| `plan200` | 200 积分 | 30 天         |
| `plan500` | 500 积分 | 100 天 (默认) |

> 不配置时默认为 `plan500`，即积分达到 500 时自动兑换 100 天

- **紧急续期（剩余天数不足时降级兑换）**

  当某账号**剩余天数 < 3 天**、而积分**还没攒到配置的档位**（默认 500）时，脚本不再干等断档，
  会自动降级用 `plan100` 兑换 10 天救急（`POST /api/user/exchange`，请求体 `{"planType":"plan100"}`），
  成功后日志与推送里标记为 `✅ 兑换成功 plan100（紧急续期）`。

  决策优先级：

  | 条件 | 行为 |
  | --- | --- |
  | 积分 ≥ 配置档位（默认 500） | 按配置档位兑换（如 `plan500` 换 100 天） |
  | 积分 < 配置档位 且 剩余 < 3 天 且 积分 ≥ 100 | 降级 `plan100` 紧急续期 |
  | 积分 < 100 且 剩余 < 3 天 | 无法续期，日志 + 推送里 ❗ 提醒（Actions 里显示为 error） |
  | 剩余 ≥ 3 天 | 不兑换，继续攒积分等配置档位 |

  剩余天数取**签到后**重新查询的状态（不再用签到前的旧值）；兑换成功后会再刷一次积分和天数。
  阈值可在 `glados_checkin.py` 顶部调整：`URGENT_DAYS_THRESHOLD = 3`、`URGENT_PLAN = "plan100"`。

- **手机推送（自选）**
  - 这里应用了[pushplus](https://www.pushplus.plus/) 来实现微信、邮箱或者浏览器插件推送，并且优化了结果UI，更美观。
  - 方式：关注微信号pushplus，注册，实名，个人中心找到`用户token`
  
- 添加1个`repository secret`，命名为`PUSHPLUS_TOKEN`，其值对应`用户token`

  推送结果如下：

  1. 浏览器插件（在edge插件商店搜“pushplus”即可）结果如图：

  ##### ![](/imgs/3.1.png)

  2. 微信消息结果如图：

  ![](/imgs/3.2.png)
### **star**自己的仓库

![图片加载失败](imgs/4.png)

## 问题排查与定位

- 大家可以通过查询 actions 中的 `GLaDOS Checkin`日志快速定位问题

  ![](/imgs/3.3.png)

------

![](/imgs/日志检查.png)

## 声明

1. 本项目来自GitHub项目[原贴链接](https://github.com/Devilstore/Gladoscheckin)，与原作者不同的是，我用了pushplus来实现微信端、邮箱或者浏览器插件推送，用AI优化了一下页面，需要的可以fork使用一下。

2. 本项目不保证稳定运行与更新, 因GitHub相关规定可能会删库, 请注意备份。



 
