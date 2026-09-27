import requests
import json
import os
import logging
import datetime
import time
import re
import sys
import hashlib
from typing import Dict, List, Optional, Tuple
from functools import wraps

# ===================== 环境检测 =====================
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
RUNNER_DEBUG = os.environ.get("RUNNER_DEBUG") == "1"

# ===================== 时间 & 日志 =====================
def beijing_time_converter(timestamp):
    utc_dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
    return utc_dt.astimezone(beijing_tz).timetuple()

logging.basicConfig(
    level=logging.DEBUG if RUNNER_DEBUG else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if handler.formatter:
        handler.formatter.converter = beijing_time_converter

logger = logging.getLogger(__name__)

# ===================== GitHub Actions 专用输出工具 =====================
class ActionsLogger:
    """GitHub Actions 专用日志工具，支持分组、折叠、颜色"""

    @staticmethod
    def group(title: str):
        if IS_GITHUB_ACTIONS:
            print(f"::group::{title}")
        else:
            print(f"\n{'='*50}\n{title}\n{'='*50}")

    @staticmethod
    def endgroup():
        if IS_GITHUB_ACTIONS:
            print("::endgroup::")
        else:
            print("="*50)

    @staticmethod
    def notice(message: str, title: str = ""):
        if IS_GITHUB_ACTIONS:
            title_param = f" title={title}" if title else ""
            print(f"::notice{title_param}::{message}")
        else:
            prefix = f"[{title}] " if title else ""
            print(f"ℹ️  {prefix}{message}")

    @staticmethod
    def warning(message: str):
        if IS_GITHUB_ACTIONS:
            print(f"::warning::{message}")
        else:
            print(f"⚠️  {message}")

    @staticmethod
    def error(message: str):
        if IS_GITHUB_ACTIONS:
            print(f"::error::{message}")
        else:
            print(f"❌ {message}")

    @staticmethod
    def debug(message: str):
        if IS_GITHUB_ACTIONS:
            print(f"::debug::{message}")
        else:
            print(f"🐛 {message}")

gha = ActionsLogger()

# ===================== 环境变量 =====================
ENV_PUSHPLUS_TOKEN = "PUSHPLUS_TOKEN"
ENV_COOKIES = "GLADOS_COOKIES"
ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
ENV_DEBUG = "DEBUG"

# ===================== API（多域名轮换：GLaDOS 改版后会话可能与域名绑定） =====================
DOMAINS = ["glados.cloud", "railgun.info"]

PUSHPLUS_URL = "http://www.pushplus.plus/send"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"

def checkin_url(domain: str) -> str:
    return f"https://{domain}/api/user/checkin"

def status_url(domain: str) -> str:
    return f"https://{domain}/api/user/status"

def points_url(domain: str) -> str:
    return f"https://{domain}/api/user/points"

def exchange_url(domain: str) -> str:
    return f"https://{domain}/api/user/exchange"

def api_headers(domain: str) -> Dict[str, str]:
    return {
        "referer": f"https://{domain}/console/checkin",
        "origin": f"https://{domain}",
        "user-agent": USER_AGENT,
        "content-type": "application/json;charset=UTF-8"
    }

EXCHANGE_POINTS = {
    "plan100": 100,
    "plan200": 200,
    "plan500": 500
}

DEBUG = os.environ.get(ENV_DEBUG, "false").lower() == "true" or RUNNER_DEBUG

# ===================== 工具函数 =====================
def retry(max_attempts=3, delay=2):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        logger.warning(f"[{func.__name__}] 第 {attempt + 1} 次尝试失败: {e}，{delay}秒后重试...")
                        time.sleep(delay)
                    else:
                        logger.error(f"[{func.__name__}] 最终失败: {e}")
            raise last_exception
        return wrapper
    return decorator

def parse_cookies(raw: str) -> List[str]:
    """把 GLADOS_COOKIES 拆成多账号 cookie 列表。

    支持 & 和换行两种分隔符；账号起始标记为 gld:sess= 或 koa:sess=，
    两者都不含的片段视为上一账号 cookie 值的一部分原样拼回
    （个别值里出现 & 时自动复原）。
    """
    tokens = re.split(r"([&\n])", raw)  # 分隔符保留在奇数位
    merged: List[str] = []
    pending_sep = ""
    for tok in tokens:
        if tok in ("&", "\n"):
            pending_sep = tok
            continue
        p = tok.strip()
        if not p:
            continue
        if merged and pending_sep and "gld:sess=" not in p and "koa:sess=" not in p:
            merged[-1] = merged[-1] + pending_sep + p
        else:
            merged.append(p)
        pending_sep = ""
    return merged

def cookie_fingerprint(cookie: str) -> Dict:
    """生成 cookie 的安全指纹（不含任何原始值，用于排查格式问题）"""
    return {
        "len": len(cookie),
        "gld": "gld:sess=" in cookie,
        "sess": "koa:sess=" in cookie,
        "sha": hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:8]
    }

def load_config() -> Tuple[str, List[str], str]:
    """加载并验证配置，带详细日志"""
    gha.group("🚀 初始化配置")

    try:
        token = os.environ.get(ENV_PUSHPLUS_TOKEN, "")
        cookies_raw = os.environ.get(ENV_COOKIES, "")
        plan = os.environ.get(ENV_EXCHANGE_PLAN, "plan500")

        gha.notice(f"运行环境: {'GitHub Actions' if IS_GITHUB_ACTIONS else '本地'}", "环境")
        gha.notice(f"Python 版本: {sys.version.split()[0]}", "环境")

        if not cookies_raw:
            raise ValueError("❌ 未设置 GLADOS_COOKIES 环境变量")

        cookies = parse_cookies(cookies_raw)
        if not cookies:
            raise ValueError("❌ GLADOS_COOKIES 格式错误")

        logger.info(f"📋 配置信息:")
        logger.info(f"   - 账号数量: {len(cookies)} 个")
        logger.info(f"   - 域名列表: {', '.join(DOMAINS)}")
        logger.info(f"   - 兑换计划: {plan} ({EXCHANGE_POINTS.get(plan, 500)} 积分)")
        logger.info(f"   - PushPlus: {'✅ 已启用' if token else '❌ 未启用'}")

        # 验证 Cookie 结构（只输出指纹，不输出内容）
        for i, cookie in enumerate(cookies, 1):
            fp = cookie_fingerprint(cookie)
            logger.info(f"   - 账号{i} 指纹: 长度={fp['len']} gld:sess={'✓' if fp['gld'] else '✗'} "
                        f"koa:sess={'✓' if fp['sess'] else '✗'} sha={fp['sha']}")
            if not fp["gld"]:
                gha.warning(f"账号 {i} 缺少 gld:sess（改版后的唯一身份凭证），该账号将无法通过认证")

        return token, cookies, plan
    finally:
        gha.endgroup()

@retry(max_attempts=3, delay=2)
def make_request(
    url: str,
    method: str,
    headers: Dict[str, str],
    data: Optional[Dict] = None,
    cookies: str = ""
) -> Optional[requests.Response]:
    """发送 HTTP 请求（带重试和详细日志）"""

    h = headers.copy()
    h["cookie"] = cookies

    if DEBUG:
        logger.debug(f"请求: {method} {url}")

    try:
        if method == "POST":
            r = requests.post(url, headers=h, json=data, timeout=15)
        else:
            r = requests.get(url, headers=h, timeout=15)

        if not r.ok:
            logger.error(f"❌ HTTP 错误: {r.status_code}")
            if DEBUG:
                logger.debug(f"响应: {r.text[:200]}")
            return None
        return r
    except requests.exceptions.Timeout:
        logger.error(f"⏱️ 请求超时")
        raise
    except Exception as e:
        logger.error(f"💥 请求异常: {e}")
        raise

def get_status_raw(cookie: str, domain: str) -> Tuple[bool, Dict]:
    """查询账号状态。返回 (会话在该域名上是否有效, data 字段)"""
    try:
        r = make_request(status_url(domain), "GET", api_headers(domain), cookies=cookie)
        if r:
            data = r.json().get("data")
            if isinstance(data, dict) and data:
                return True, data
    except Exception as e:
        logger.error(f"查询状态异常: {e}")
    return False, {}

def get_points(cookie: str, headers: Dict[str, str], domain: str) -> int:
    """获取当前积分"""
    try:
        r = make_request(points_url(domain), "GET", headers, cookies=cookie)
        if r:
            points = int(float(r.json().get("points", 0)))
            if DEBUG:
                logger.debug(f"当前积分: {points}")
            return points
    except Exception as e:
        logger.error(f"获取积分失败: {e}")
    return 0

def pushplus_send(token: str, title: str, content: str):
    """PushPlus 消息推送"""
    if not token:
        logger.info("📭 未设置 PUSHPLUS_TOKEN，跳过推送")
        return

    gha.group("📤 发送 PushPlus 推送")
    try:
        r = requests.post(PUSHPLUS_URL, json={
            "token": token,
            "title": title,
            "content": content,
            "template": "txt"
        }, timeout=10)

        if r.ok and r.json().get("code") == 200:
            gha.notice("PushPlus 推送成功", "推送")
            logger.info("✅ PushPlus 推送成功")
        else:
            gha.error(f"PushPlus 推送失败: {r.text}")
            logger.error(f"❌ PushPlus 推送失败: {r.text}")
    except Exception as e:
        gha.error(f"PushPlus 推送异常: {e}")
        logger.error(f"💥 PushPlus 推送异常: {e}")
    finally:
        gha.endgroup()

# ===================== 核心逻辑（增强版） =====================
COOKIE_INVALID_STATUS = "Cookie失效"

def checkin_and_process(cookie: str, plan: str, account_idx: int) -> Dict:
    """处理单个账号：先探测会话在哪个域名上有效，再在该域名完成签到/兑换"""

    gha.group(f"👤 处理账号 {account_idx}")

    try:
        # 1. 探测有效域名
        logger.info("🌐 步骤 1/5: 探测会话有效域名...")
        working_domain = None
        status_data = {}
        for domain in DOMAINS:
            ok, data = get_status_raw(cookie, domain)
            if ok:
                working_domain = domain
                status_data = data
                logger.info(f"   ├─ ✅ 会话在 {domain} 上有效")
                break
            logger.info(f"   ├─ ⚠️ {domain} 返回未登录，尝试下一个域名...")

        if not working_domain:
            logger.error("   └─ ❌ 所有域名均未登录")
            gha.warning(f"账号 {account_idx}: 所有域名都返回未登录，gld:sess token 已被服务器吊销或从未有效")
            gha.notice(
                "修复方法(2026-09-25 改版后认证只认 gld:sess): 为每个账号开一个独立的隐身窗口"
                "登录 GLaDOS → F12 → Network → 刷新 → 复制第一个请求 Request Headers 里完整的 Cookie 值。"
                "不要在同一个窗口里切换登录多个账号——新登录会吊销上一个账号的 gld:sess token!",
                "Cookie 失效"
            )
            return {
                "status": COOKIE_INVALID_STATUS,
                "points": 0,
                "days": COOKIE_INVALID_STATUS,
                "total": "未知",
                "exchange": "未兑换",
                "before": 0,
                "after": 0
            }

        headers = api_headers(working_domain)

        # 2. 获取签到前积分
        logger.info("📊 步骤 2/5: 查询当前积分...")
        points_before = get_points(cookie, headers, working_domain)
        logger.info(f"   ├─ 当前积分: {points_before}")

        status_msg = "签到失败"
        gained = 0
        days = "未知"
        total = "未知"
        exchange_msg = "未兑换"

        # 3. 执行签到
        logger.info("📝 步骤 3/5: 执行签到...")
        try:
            r = make_request(checkin_url(working_domain), "POST", headers,
                             {"token": working_domain}, cookie)
            if r:
                data = r.json()
                msg = data.get("message", "")
                code = data.get("code", -1)
                msg_l = msg.lower()

                if DEBUG:
                    logger.debug(f"签到响应: {data}")

                if "checkin! got" in msg_l or code == 0:
                    status_msg = "签到成功"
                    logger.info("   ├─ ✅ 签到成功")
                elif ("repeats" in msg_l or "try tomorrow" in msg_l
                      or "observation logged" in msg_l or "return tomorrow" in msg_l):
                    # 改版后"今日已签到"的新旧两种消息格式都归到这里
                    status_msg = "重复签到"
                    logger.info("   ├─ 🔁 重复签到/今日已签到")
                else:
                    status_msg = f"异常: {msg[:30]}"
                    gha.warning(f"签到返回异常消息: {msg}")
            else:
                status_msg = "网络请求失败"
                logger.error("   ├─ ❌ 签到请求失败")
        except Exception as e:
            status_msg = f"异常: {str(e)[:20]}"
            logger.error(f"   ├─ ❌ 签到异常: {e}")

        # 4. 剩余天数 & 签到后积分
        logger.info("📅 步骤 4/5: 查询剩余天数和最新积分...")
        try:
            days_val = status_data.get("leftDays", 0)
            days = f"{int(float(days_val))} 天"
            logger.info(f"   ├─ 剩余天数: {days}")
        except Exception as e:
            logger.error(f"   ├─ ❌ 解析剩余天数异常: {e}")
            days = "获取失败"

        try:
            points_after = get_points(cookie, headers, working_domain)
            gained = points_after - points_before
            total = f"{points_after} 积分"

            if gained > 0:
                logger.info(f"   ├─ 积分变化: {points_before} → {points_after} (+{gained})")
                if status_msg == "签到成功":
                    status_msg += f" +{gained}"
            else:
                logger.info(f"   ├─ 积分无变化: {points_after}")
        except Exception as e:
            logger.error(f"   ├─ ❌ 更新积分失败: {e}")
            total = "获取失败"
            points_after = points_before

        # 5. 执行兑换
        logger.info("🔄 步骤 5/5: 检查兑换条件...")
        need = EXCHANGE_POINTS.get(plan, 500)
        exchange_success = False

        if points_after >= need:
            logger.info(f"   ├─ 尝试兑换 {plan}（需 {need} 积分）...")
            try:
                r = make_request(
                    exchange_url(working_domain),
                    "POST",
                    headers,
                    {"planType": plan},
                    cookie
                )
                if r:
                    resp_data = r.json()
                    if DEBUG:
                        logger.debug(f"兑换响应: {resp_data}")

                    if resp_data.get("code") == 0:
                        exchange_msg = f"✅ 兑换成功 {plan}"
                        exchange_success = True
                        logger.info(f"   └─ ✅ 兑换成功 {plan}")
                    else:
                        error_msg = resp_data.get("message", "未知错误")
                        exchange_msg = f"❌ 兑换失败: {error_msg[:30]}"
                        gha.warning(f"兑换失败: {error_msg}")
                else:
                    exchange_msg = "❌ 兑换请求失败"
                    logger.error("   └─ ❌ 兑换请求失败")
            except Exception as e:
                exchange_msg = f"❌ 兑换异常: {str(e)[:20]}"
                logger.error(f"   └─ ❌ 兑换异常: {e}")
        else:
            diff = need - points_after
            exchange_msg = f"⏳ 积分不足（还需 {diff} 分）"
            logger.info(f"   └─ ⏳ 积分不足，还需 {diff} 分")

        # 6. 如果兑换成功，重新获取积分
        if exchange_success:
            try:
                final_points = get_points(cookie, headers, working_domain)
                total = f"{final_points} 积分（已兑换 {need}）"
                logger.info(f"   └─ 兑换后余额: {final_points} 积分")
            except:
                pass

        # 汇总输出（在组内显示）
        logger.info("📈 账号处理结果:")
        logger.info(f"   状态: {status_msg}")
        logger.info(f"   积分: {total}")
        logger.info(f"   天数: {days}")
        logger.info(f"   兑换: {exchange_msg}")

        return {
            "status": status_msg,
            "points": gained,
            "days": days,
            "total": total,
            "exchange": exchange_msg,
            "before": points_before,
            "after": points_after
        }
    finally:
        gha.endgroup()

# ===================== 格式化输出 =====================
def count_results(results: List[Dict]) -> Tuple[int, int, int]:
    """统计 成功/重复/失败 数量"""
    success = sum(1 for r in results if "成功" in r['status'] and "重复" not in r['status'])
    repeat = sum(1 for r in results if "重复" in r['status'] or "已签到" in r['status'])
    fail = len(results) - success - repeat
    return success, repeat, fail

def format_results_table(results: List[Dict]) -> str:
    """生成 ASCII 表格形式的摘要"""
    lines = []
    lines.append("\n" + "="*80)
    lines.append("📊 GLaDOS 签到执行摘要")
    lines.append("="*80)
    lines.append(f"{'账号':<5} {'状态':<15} {'积分':<12} {'剩余天数':<10} {'兑换状态':<20}")
    lines.append("-"*80)

    total_gained = 0

    for i, r in enumerate(results, 1):
        status_short = r['status'].replace("签到成功", "成功").replace("重复签到", "重复")[:12]
        points_str = str(r['points']) if r['points'] != "未知" else "-"
        if str(r['points']).isdigit() or (isinstance(r['points'], int) and r['points'] > 0):
            total_gained += int(r['points'])
            points_str = f"+{r['points']}"
        days_short = r['days'].replace(" 天", "")
        exchange_short = r['exchange'][:18]

        lines.append(f"{i:<5} {status_short:<15} {points_str:<12} {days_short:<10} {exchange_short:<20}")

    lines.append("-"*80)

    success, repeat, fail = count_results(results)
    lines.append(f"统计: ✅成功 {success} 个 | 🔁重复 {repeat} 个 | ❌失败 {fail} 个 | 总获得积分: {total_gained}")
    lines.append("="*80)

    return "\n".join(lines)

def format_push(results: List[Dict]) -> Tuple[str, str]:
    """格式化推送消息（手机端）"""
    success, repeat, fail = count_results(results)

    title = f"GLaDOS 签到 | ✅{success} 🔁{repeat} ❌{fail}"

    blocks = []
    for i, r in enumerate(results, 1):
        if "成功" in r["status"] and "重复" not in r["status"]:
            icon = "✅"
        elif "重复" in r["status"] or "已签到" in r["status"]:
            icon = "🔁"
        elif "失败" in r["status"] or COOKIE_INVALID_STATUS in r["status"]:
            icon = "❌"
        else:
            icon = "⚠️"

        delta = f"+{r['points']}" if isinstance(r['points'], int) and r['points'] > 0 else "+0"

        block = (
            f"【账号 {i}】{icon} {r['status']}\n"
            f"💰 积分：{r['total']}（{delta}）\n"
            f"📅 剩余：{r['days']}\n"
            f"🔄 兑换：{r['exchange']}"
        )
        blocks.append(block)

    content = "\n\n".join(blocks)
    content += "\n\n⏰ 北京时间：" + datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    return title, content

# ===================== main =====================
def main():
    start_time = time.time()

    try:
        token, cookies, plan = load_config()
        results = []

        # 处理每个账号
        for i, cookie in enumerate(cookies, 1):
            try:
                result = checkin_and_process(cookie, plan, i)
                results.append(result)
            except Exception as e:
                logger.error(f"💥 账号 {i} 处理异常: {e}")
                results.append({
                    "status": f"异常: {str(e)[:20]}",
                    "points": "0",
                    "days": "未知",
                    "total": "未知",
                    "exchange": "未处理",
                    "before": 0,
                    "after": 0
                })
            # 账号间延迟（避免触发站点频率限制）
            if i < len(cookies):
                time.sleep(3)

        # 生成表格摘要（在 Actions 日志中显示）
        table_summary = format_results_table(results)
        print(table_summary)  # 直接输出到 stdout

        # 生成推送内容
        title, content = format_push(results)

        # 推送详情
        gha.group("📱 推送内容预览")
        logger.info(f"推送标题: {title}")
        logger.info(f"推送内容:\n{content}")
        gha.endgroup()

        # 发送推送
        pushplus_send(token, title, content)

        # 最终统计
        elapsed = time.time() - start_time
        gha.notice(f"所有任务完成，耗时 {elapsed:.2f} 秒", "完成")

        fail_count = count_results(results)[2]
        if fail_count > 0:
            gha.warning(f"有 {fail_count} 个账号处理失败")

    except Exception as e:
        gha.error(f"程序运行失败: {e}")
        logger.exception("详细错误信息:")
        sys.exit(1)

if __name__ == "__main__":
    main()
