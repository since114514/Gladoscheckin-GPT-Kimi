"""GLaDOS 自动签到（多账号 × 多域名，设备 UA 自适应）

底座：Devilstore/Gladoscheckin 2026-10-18 版（社区实测可用：cookie × 域名矩阵 + 设备绑定自适应）
本仓库在此之上保留/新增：
  1. PushPlus 推送后端（微信 / 邮箱 / 浏览器插件），兼容原有 PUSHPLUS_TOKEN
  2. 剩余天数告急且积分不够配置档位时，降级 plan100 紧急续期
  3. 签到失败不变红（Actions 保持绿色），但失败原因写进 annotation 与推送
  4. 每个账号按「任意域名成功即算成功」聚合，推送里一个账号一行，不再刷出重复的红行

为什么是 cookie × 域名矩阵：
  GLaDOS 账号已分裂到 glados.cloud 与 railgun.info 两套站点，一个 Cookie 只在一个站有效。
  逐个域名尝试签到，由 code 判定归属，调用方不需要知道账号迁到了哪个站。
"""

import os
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from logging_config import init_logger


def _force_utf8_stdio() -> None:
    """本地 Windows 控制台是 GBK 时，打印 emoji/表格会 UnicodeEncodeError 打断整次运行，这里兜底。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -2


class ExchangePlan(Enum):
    """兑换计划"""

    PLAN100 = "plan100"
    PLAN200 = "plan200"
    PLAN500 = "plan500"


class APIEndpoint(Enum):
    """API端点"""

    CHECKIN = "/api/user/checkin"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    PENDING = "⏳"
    CHECKIN = "🎫"
    STATUS = "📊"
    POINTS = "💰"
    EXCHANGE = "🎁"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    DOMAIN = "🌐"
    WARNING = "⚠️"
    ERROR = "🔴"
    INFO = "ℹ️"


def log_method(func):
    """日志装饰器"""

    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        emoji_map = {
            "checkin": LogEmoji.CHECKIN,
            "get_status": LogEmoji.STATUS,
            "get_points": LogEmoji.POINTS,
            "exchange": LogEmoji.EXCHANGE,
        }
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            logger.error(
                f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] "
                f"{LogEmoji.ERROR} {method_name} 执行失败: {e}（{emoji_map.get(method_name, LogEmoji.INFO)}）"
            )

            DEFAULT_ERRORS = {
                "checkin": {"status": "签到失败", "points": "0", "message": f"执行失败: {e}"},
                "get_status": ("None 天", None, -2),
                "get_points": ("None 积分", 0),
                "exchange": "",
            }

            if method_name in DEFAULT_ERRORS:
                error_template = DEFAULT_ERRORS[method_name]
                if isinstance(error_template, dict):
                    return error_template.copy()
                return error_template
            raise

    return wrapper


class Config:
    """应用配置"""

    ENV_PUSHPLUS_KEY = "PUSHPLUS_TOKEN"
    ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
    ENV_SERVERCHAN_KEY = "SERVERCHAN_SENDKEY"
    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
    ENV_VERBOSE = "GLADOS_VERBOSE"

    """默认兑换计划（none 表示不自动兑换）"""
    DEFAULT_EXCHANGE_PLAN = "none"

    """默认是否输出详细响应"""
    DEFAULT_VERBOSE = False

    """默认域名（一账号只会在其中一个站有效，矩阵会自动命中）"""
    DOMAINS = ["glados.cloud", "railgun.info"]

    """兑换计划列表"""
    EXCHANGE_PLANS = {
        ExchangePlan.PLAN100.value: 100,
        ExchangePlan.PLAN200.value: 200,
        ExchangePlan.PLAN500.value: 500,
    }

    """紧急续期：剩余天数低于阈值且积分不够配置档位时降级兑换"""
    URGENT_DAYS_THRESHOLD = 3
    URGENT_PLAN = ExchangePlan.PLAN100.value

    def __init__(self):
        self.pushplus_token: str = ""
        self.push_key: str = ""
        self.serverchan_key: str = ""
        self.cookies_list: List[str] = []
        self.exchange_plan: str = self.DEFAULT_EXCHANGE_PLAN
        self.verbose: bool = self.DEFAULT_VERBOSE
        self._load_config()

    def _load_config(self) -> None:
        """加载配置"""
        pushplus_env: Optional[str] = os.environ.get(self.ENV_PUSHPLUS_KEY)
        push_key_env: Optional[str] = os.environ.get(self.ENV_PUSH_KEY)
        serverchan_key_env: Optional[str] = os.environ.get(self.ENV_SERVERCHAN_KEY)
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        exchange_plan_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_PLAN)
        verbose_env: Optional[str] = os.environ.get(self.ENV_VERBOSE)

        self.pushplus_token = (pushplus_env or "").strip()
        self.push_key = (push_key_env or "").strip()
        self.serverchan_key = (serverchan_key_env or "").strip()

        if not raw_cookies_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 未设置。")
            self.cookies_list = []
        else:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

        if not exchange_plan_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 未设置，将使用默认设置（不自动兑换）。")
            self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN
        else:
            if exchange_plan_env in self.EXCHANGE_PLANS:
                self.exchange_plan = exchange_plan_env
                logger.info(f"{LogEmoji.SUCCESS} 使用指定的兑换计划: {self.exchange_plan}")
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 的值 '{exchange_plan_env}' 无效，将跳过自动兑换。")
                self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN

        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.cookies_list)} 个 Cookie 用于签到。")
        logger.info(
            f"{LogEmoji.INFO} 推送后端: PushPlus {'已设置' if self.pushplus_token else '未设置'} / "
            f"PushDeer {'已设置' if self.push_key else '未设置'} / "
            f"Server酱 {'已设置' if self.serverchan_key else '未设置'}。"
        )
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_EXCHANGE_PLAN}: {self.exchange_plan}。")
        logger.info(
            f"{LogEmoji.INFO} 紧急续期: 剩余 < {self.URGENT_DAYS_THRESHOLD} 天且积分不足档位时，降级 {self.URGENT_PLAN}。"
        )

        if verbose_env is not None:
            verbose_env_lower = verbose_env.lower()
            if verbose_env_lower in ["true", "1", "yes", "y"]:
                self.verbose = True
            elif verbose_env_lower in ["false", "0", "no", "n"]:
                self.verbose = False
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效，将使用默认值 {self.DEFAULT_VERBOSE}。")

        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_VERBOSE}: {self.verbose}。")


# 各平台合法 User-Agent（版本号无关，仅平台 token 关键）
PLATFORM_UA = {
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Android": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}


class API:
    """API 调用"""

    CHECKIN_URL = APIEndpoint.CHECKIN.value
    STATUS_URL = APIEndpoint.STATUS.value
    POINTS_URL = APIEndpoint.POINTS.value
    EXCHANGE_URL = APIEndpoint.EXCHANGE.value

    def __init__(self, domain: str, cookie_index: int = 0, verbose: bool = False):
        self.domain: str = domain
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
        self.headers: Dict[str, str] = self._get_headers()
        self._device_ua_cache: Dict[str, str] = {}
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        # 网络抖动时自动重试（指数退避），避免误报失败
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def __del__(self):
        self.close()

    def close(self) -> None:
        if hasattr(self, "session"):
            try:
                self.session.close()
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _get_headers(self) -> Dict[str, str]:
        return {
            "origin": f"https://{self.domain}",
            "user-agent": PLATFORM_UA["Windows"],
        }

    def _log(self, level: str, emoji: str, message: str, force: bool = False) -> None:
        prefix = f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}]"
        log_message = f"{prefix} {emoji} {message}"
        if force or self.verbose:
            if level == "info":
                logger.info(log_message)
            elif level == "warning":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)

    def _get_full_url(self, path: str) -> str:
        return f"https://{self.domain}{path}"

    def _make_request(
        self,
        url: str,
        method: str,
        data: Optional[Dict] = None,
        cookies: str = "",
        user_agent: Optional[str] = None,
    ) -> Optional[requests.Response]:
        session_headers = self.headers.copy()
        if user_agent:
            session_headers["user-agent"] = user_agent
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                response = self.session.post(url, headers=session_headers, data=data, timeout=(60, 120))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None

            if not response.ok:
                self._log(
                    "warning",
                    LogEmoji.WARNING,
                    f"向 {url} 发起的请求失败，状态码 {response.status_code}。响应内容: {response.text}",
                    force=True,
                )
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

    def _get_checkin_data(self) -> Dict[str, str]:
        return {"token": self.domain}

    @log_method
    def checkin(self, cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行签到，含设备平台自适应重试"""
        url = self._get_full_url(self.CHECKIN_URL)
        checkin_data = self._get_checkin_data()

        # 起始 UA 优先复用已学得的设备 UA，否则用会话默认 UA
        start_ua = self._device_ua_cache.get("_last") or self.headers.get("user-agent")

        raw = self._checkin_attempt(url, checkin_data, cookies, start_ua)

        # 设备不匹配(code 4)时按服务端 loginDevice 切换 UA 重试一次
        if raw is not None and raw.get("code") == 4 and raw.get("reason") == "device-mismatch":
            login_device = raw.get("loginDevice")
            recover_ua = self._device_ua_cache.get(login_device) or PLATFORM_UA.get(login_device)
            if recover_ua and recover_ua != start_ua:
                self._log("warning", LogEmoji.WARNING, f"设备平台不匹配 (loginDevice={login_device})，切换 UA 重试", force=True)
                raw = self._checkin_attempt(url, checkin_data, cookies, recover_ua)

        return self._parse_checkin(raw)

    def _checkin_attempt(self, url: str, data: Dict, cookies: str, user_agent: str) -> Optional[Dict]:
        """发起一次签到请求并返回解析后的响应体（失败返回 None）"""
        response = self._make_request(url, "POST", data, cookies, user_agent=user_agent)
        if not response:
            return None
        try:
            raw = response.json()
        except ValueError:
            self._log("error", LogEmoji.ERROR, "签到响应解析失败", force=True)
            return None

        # 缓存学得的设备 UA，供本次运行后续请求复用
        if raw.get("code") == 4 and raw.get("reason") == "device-mismatch":
            login_device = raw.get("loginDevice")
            if login_device in PLATFORM_UA:
                self._device_ua_cache[login_device] = PLATFORM_UA[login_device]
        elif raw.get("code") in (CheckinStatus.SUCCESS.value, CheckinStatus.REPEAT.value):
            self._device_ua_cache["_last"] = user_agent

        return raw

    def _parse_checkin(self, raw: Optional[Dict]) -> Dict[str, Union[str, CheckinStatus]]:
        """解析签到响应为结果字典"""
        result: Dict[str, Union[str, CheckinStatus]] = {
            "status": "签到失败",
            "points": "0",
            "message": "",
            "code": CheckinStatus.FAILURE,
        }

        if not raw:
            result["message"] = "网络请求失败"
            return result

        code = raw.get("code", -2)
        message = raw.get("message", "无消息字段")
        points = str(raw.get("points", 0))

        if code == CheckinStatus.SUCCESS.value:
            self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points}, message : {message} }}")
            result["code"] = CheckinStatus.SUCCESS
            result["status"] = "签到成功"
            result["points"] = points
            result["message"] = message
        elif code == CheckinStatus.REPEAT.value:
            self._log("info", LogEmoji.REPEAT, f"{{ code : {code}, message : {message} }}", force=True)
            result["code"] = CheckinStatus.REPEAT
            result["status"] = "重复签到"
            result["points"] = "0"
            result["message"] = message
        else:
            # 失败分支：按 code 细分诊断，便于快速定位根因
            if code == 4:
                reason = raw.get("reason")
                if reason == "device-mismatch":
                    login_device = raw.get("loginDevice") or "未知"
                    if login_device in PLATFORM_UA:
                        self._log("error", LogEmoji.ERROR, f"设备平台不匹配(loginDevice={login_device})，重试后仍失败，请确认登录设备平台", force=True)
                    else:
                        self._log(
                            "error",
                            LogEmoji.ERROR,
                            f"设备平台不匹配(loginDevice={login_device} 不在支持列表)，无法自动切换 UA；"
                            f"请用同一浏览器登录后在 issue 里反馈该设备名称",
                            force=True,
                        )
                else:
                    self._log("error", LogEmoji.ERROR, f"服务端拒绝签到(code=4, reason={reason})，请检查账户风控状态", force=True)
            elif code == -2:
                self._log(
                    "error",
                    LogEmoji.ERROR,
                    f"无权限(code=-2)：Cookie 可能已失效，请重新登录 {self.domain} 刷新 Cookie"
                    f"（需含 gld:sess 与 gld:sess.sig）并更新仓库 Secret GLADOS_COOKIES 后重试",
                    force=True,
                )
            else:
                self._log("error", LogEmoji.ERROR, f"未预期响应(code={code}, message={message})，请检查 GLaDOS 服务状态或 Cookie", force=True)
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["points"] = "0"
            result["message"] = message

        return result

    @log_method
    def get_status(self, cookies: str) -> Tuple[str, Optional[int], int]:
        """获取状态，返回 (天数文案, 天数数值或 None, code)"""
        url = self._get_full_url(self.STATUS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if not response:
            self._log("warning", LogEmoji.WARNING, "获取状态失败")
            return "None 天", None, -2

        data = response.json()
        code = data.get("code", -2)
        left_days = data.get("data", {}).get("leftDays", None) if isinstance(data.get("data"), dict) else None

        if left_days is None:
            self._log("info", LogEmoji.FAIL, f"{{ code : {code}, leftDays : {left_days} 天}}")
            return "None 天", None, code

        left_days_int = int(float(left_days))
        self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, leftDays : {left_days_int} 天}}")
        return f"{left_days_int} 天", left_days_int, code

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int]:
        """获取积分"""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if not response:
            self._log("warning", LogEmoji.WARNING, "获取积分失败")
            return "None 积分", 0

        data = response.json()
        code = data.get("code", -2)
        points = data.get("points", None)

        if points is None:
            self._log("info", LogEmoji.FAIL, f"{{ code : {code}, points : {points} 积分}}")
            return "None 积分", 0

        points_int = int(float(points))
        self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points_int} 积分}}")
        return f"{points_int} 积分", points_int

    @log_method
    def exchange(self, cookies: str, plan: str, required_points: int) -> str:
        """执行兑换（新接口按表单提交，与签到保持一致）"""
        url = self._get_full_url(self.EXCHANGE_URL)
        response = self._make_request(url, "POST", {"planType": plan}, cookies)

        if not response:
            self._log("warning", LogEmoji.WARNING, "兑换失败", force=True)
            return "兑换失败"

        data = response.json()
        code = data.get("code", -2)
        message = data.get("message", "未知错误")

        if code == 0:
            self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, message : {message} }}", force=True)
            return f"兑换成功: {plan}"

        self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
        return f"兑换失败: {message}"


@dataclass()
class CheckinResult:
    """单个「账号 × 域名」的签到结果"""

    cookie_index: int
    domain: str
    status: str = "签到失败"
    points: str = "0"
    days: str = "None"
    points_total: str = "None"
    exchange: str = "未兑换"
    code: CheckinStatus = CheckinStatus.FAILURE
    authenticated: bool = False
    points_num: int = 0
    days_num: Optional[int] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Union[str, CheckinStatus]]:
        return asdict(self)


def render_payload(payload: dict, line_sep: str = "\n") -> str:
    """将结构化签到结果渲染为文本。line_sep 控制行分隔：
    PushDeer/PushPlus 用 \\n，Server酱 用 '  \\n'（Markdown 硬换行）。"""
    lines = []
    for r in payload.get("rows", []):
        line = (
            f"账号 {r['index']}  {r['emoji']} {r['status']}  {r['domain']}  "
            f"积分 {r['points_total']}（{r['points']}） · 剩余 {r['days']}"
        )
        if r.get("exchange"):
            line += f" · {r['exchange']}"
        lines.append(line)
    note = payload.get("note")
    if note:
        lines.append(note)
    return line_sep.join(lines)


class PushBackend:
    """推送后端抽象基类（策略模式）"""

    name: str = "base"

    def send(self, title: str, payload: dict) -> bool:
        raise NotImplementedError

    def _render(self, payload: dict) -> str:
        return render_payload(payload, "\n")


class PushPlusBackend(PushBackend):
    """PushPlus 后端（微信服务号 / 邮箱 / 浏览器插件）"""

    name = "PushPlus"
    URL = "http://www.pushplus.plus/send"

    def __init__(self, token: str):
        self.token = token

    def send(self, title: str, payload: dict) -> bool:
        try:
            resp = requests.post(
                self.URL,
                json={"token": self.token, "title": title, "content": self._render(payload), "template": "txt"},
                timeout=15,
            )
            data = resp.json() if resp.ok else {}
            if data.get("code") == 200:
                return True
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {resp.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class PushDeerBackend(PushBackend):
    """PushDeer 后端（延迟导入，未安装 SDK 不影响其他后端）"""

    name = "PushDeer"

    def __init__(self, push_key: str):
        self.push_key = push_key

    def send(self, title: str, payload: dict) -> bool:
        try:
            from pypushdeer import PushDeer
        except ImportError:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 未安装 pypushdeer，请先执行 pip install pypushdeer。")
            return False
        try:
            PushDeer(pushkey=self.push_key).send_text(title, desp=self._render(payload))
            return True
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class ServerChanBackend(PushBackend):
    """Server酱 后端（延迟导入，未安装 SDK 不影响其他后端）"""

    name = "Server酱"

    def __init__(self, send_key: str):
        self.send_key = send_key

    def _render(self, payload: dict) -> str:
        # Server酱 按 Markdown 渲染，单 \n 不换行；用行尾两空格+\n 做硬换行
        return render_payload(payload, "  \n")

    def send(self, title: str, payload: dict) -> bool:
        try:
            from serverchan_sdk import sc_send
        except ImportError:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 未安装 serverchan-sdk，请先执行 pip install serverchan-sdk。")
            return False
        try:
            resp = sc_send(sendkey=self.send_key, title=title, desp=self._render(payload))
            if isinstance(resp, dict) and resp.get("code") == 0:
                return True
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 接口返回 {resp}")
            return False
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class PushService:
    """推送服务：聚合多个后端，向全部已配置后端发送（都发）"""

    def __init__(self, config: Optional[Config]):
        self.backends: List[PushBackend] = []
        if config:
            if getattr(config, "pushplus_token", ""):
                self.backends.append(PushPlusBackend(config.pushplus_token))
            if getattr(config, "push_key", ""):
                self.backends.append(PushDeerBackend(config.push_key))
            if getattr(config, "serverchan_key", ""):
                self.backends.append(ServerChanBackend(config.serverchan_key))

    def send(self, title: str, payload: dict) -> bool:
        if not self.backends:
            logger.info(f"{LogEmoji.WARNING} 未配置推送密钥，跳过。")
            return False

        backend_names = "、".join(b.name for b in self.backends)
        logger.info(f"{LogEmoji.INFO} 已配置 {len(self.backends)} 个后端（{backend_names}），开始发送")
        results = [backend.send(title, payload) for backend in self.backends]
        if any(results):
            logger.info(f"{LogEmoji.SUCCESS} 发送完成：{sum(results)}/{len(results)} 个后端成功")
            return True
        logger.error(f"{LogEmoji.ERROR} 全部后端发送失败")
        return False


class Checker:
    """签到"""

    def __init__(self, config: Config):
        self.config = config
        self.results: List[CheckinResult] = []

    def _log(self, cookie_idx: int, domain: str, emoji: str, message: str, force: bool = False) -> None:
        if self.config.verbose or force:
            logger.info(f"{LogEmoji.COOKIE}[{cookie_idx}] {LogEmoji.DOMAIN}[{domain}] {emoji} {message}")

    def checkin_all(self) -> None:
        """执行所有签到任务（cookie × 域名 矩阵）"""
        cookie_count = len(self.config.cookies_list)
        domain_count = len(self.config.DOMAINS)
        total_tasks = cookie_count * domain_count
        task_idx = 0

        logger.info(f"{LogEmoji.INFO} 共 {cookie_count} 个账号, {domain_count} 个域名, 共 {total_tasks} 个任务")

        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            logger.info(f"{LogEmoji.START} ========== 开始处理账号 {cookie_idx} ==========")

            for domain in self.config.DOMAINS:
                task_idx += 1
                logger.info(
                    f"{LogEmoji.INFO} ----- 任务 {task_idx}/{total_tasks}: "
                    f"{LogEmoji.COOKIE}[{cookie_idx}] on {LogEmoji.DOMAIN}[{domain}] -----"
                )
                result = self._checkin_on_domain(cookie, cookie_idx, domain)
                self.results.append(result)

                if result.authenticated:
                    self._log(cookie_idx, domain, LogEmoji.SUCCESS, f"结果: {result.status}", force=True)
                else:
                    self._log(cookie_idx, domain, LogEmoji.WARNING, f"结果: {result.status}（{result.message}）", force=True)

    def _checkin_on_domain(self, cookie: str, cookie_idx: int, domain: str) -> CheckinResult:
        result = CheckinResult(cookie_idx, domain)

        with API(domain, cookie_idx, verbose=self.config.verbose) as api:
            self._log(cookie_idx, domain, LogEmoji.STATUS, "查询剩余天数")
            days_str, days_num, _ = api.get_status(cookie)
            result.days = days_str
            result.days_num = days_num

            self._log(cookie_idx, domain, LogEmoji.CHECKIN, "执行签到")
            checkin_result = api.checkin(cookie)
            result.status = str(checkin_result["status"])
            result.code = checkin_result.get("code", CheckinStatus.FAILURE)
            result.points = str(checkin_result.get("points", "0"))
            result.message = str(checkin_result.get("message", ""))
            result.authenticated = result.code in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT)

            if not result.authenticated:
                # 认证失败就别再查积分/兑换，省请求也少噪音
                return result

            self._log(cookie_idx, domain, LogEmoji.POINTS, "查询总积分")
            points_str, points_num = api.get_points(cookie)
            result.points_total = points_str
            result.points_num = points_num

            result.exchange = self._maybe_exchange(api, cookie, cookie_idx, domain, result)

        return result

    def _maybe_exchange(self, api: API, cookie: str, cookie_idx: int, domain: str, result: CheckinResult) -> str:
        """兑换决策：够配置档位就换；剩余天数告急则降级 plan100 紧急续期"""
        plan = self.config.exchange_plan
        if plan not in self.config.EXCHANGE_PLANS:
            self._log(cookie_idx, domain, LogEmoji.INFO, "未配置兑换计划，跳过自动兑换")
            return "未配置兑换计划，跳过"

        need = self.config.EXCHANGE_PLANS[plan]
        urgent_need = self.config.EXCHANGE_PLANS[self.config.URGENT_PLAN]
        target: Optional[str] = None
        urgent = False

        if result.points_num >= need:
            target = plan
            self._log(cookie_idx, domain, LogEmoji.EXCHANGE, f"积分够 {need}，开始兑换 {plan}", force=True)
        elif (
            result.days_num is not None
            and result.days_num < self.config.URGENT_DAYS_THRESHOLD
            and result.points_num >= urgent_need
        ):
            target = self.config.URGENT_PLAN
            urgent = True
            self._log(
                cookie_idx,
                domain,
                LogEmoji.WARNING,
                f"剩余 {result.days_num} 天 < {self.config.URGENT_DAYS_THRESHOLD} 天，"
                f"积分 {result.points_num} 不够 {need}，降级紧急续期 {target}",
                force=True,
            )
        elif result.days_num is not None and result.days_num < self.config.URGENT_DAYS_THRESHOLD:
            self._log(
                cookie_idx,
                domain,
                LogEmoji.ERROR,
                f"剩余 {result.days_num} 天且积分仅 {result.points_num}，不足 {urgent_need} 无法续期",
                force=True,
            )
            return f"❗ 剩余 {result.days_num} 天，积分不足 {urgent_need} 无法续期"
        else:
            diff = need - result.points_num
            self._log(cookie_idx, domain, LogEmoji.INFO, f"积分不足，还需 {diff} 分")
            return f"待攒积分（还需 {diff} 分）"

        exchange_msg = api.exchange(cookie, target, urgent_need if urgent else need)
        if urgent and exchange_msg.startswith("兑换成功"):
            exchange_msg += "（紧急续期）"

        if exchange_msg.startswith("兑换成功"):
            # 兑换后刷新积分与天数，推送里给最新值
            points_str, points_num = api.get_points(cookie)
            result.points_total = points_str
            result.points_num = points_num
            days_str, days_num, _ = api.get_status(cookie)
            result.days = days_str
            result.days_num = days_num

        return exchange_msg

    def get_results(self) -> List[Dict[str, Union[str, CheckinStatus]]]:
        return [result.to_dict() for result in self.results]

    def best_per_account(self) -> List[CheckinResult]:
        """每个账号只留一条：成功 > 重复 > 失败"""
        rank = {CheckinStatus.SUCCESS: 0, CheckinStatus.REPEAT: 1, CheckinStatus.FAILURE: 2}
        best: Dict[int, CheckinResult] = {}
        for result in self.results:
            current = best.get(result.cookie_index)
            if current is None or rank[result.code] < rank[current.code]:
                best[result.cookie_index] = result
        return [best[idx] for idx in sorted(best)]

    def format_results(self) -> dict:
        """按账号聚合结果，生成结构化 payload（表现交给各推送后端渲染）"""
        best = self.best_per_account()
        raw_results = self.get_results()

        success_count = sum(1 for r in best if r.code == CheckinStatus.SUCCESS)
        repeat_count = sum(1 for r in best if r.code == CheckinStatus.REPEAT)
        fail_count = sum(1 for r in best if r.code == CheckinStatus.FAILURE)
        total = len(best)

        if total and fail_count == total:
            status_word = "失败"
        elif fail_count:
            status_word = "警告"
        elif total and repeat_count == total:
            status_word = "重复"
        else:
            status_word = "成功"

        title = f"{status_word} | GLaDOS 签到 ✅{success_count} 🔁{repeat_count} ❌{fail_count}"

        status_emoji_map = {
            CheckinStatus.SUCCESS: LogEmoji.SUCCESS,
            CheckinStatus.REPEAT: LogEmoji.REPEAT,
            CheckinStatus.FAILURE: LogEmoji.ERROR,
        }

        rows = []
        for res in best:
            rows.append(
                {
                    "index": res.cookie_index,
                    "emoji": status_emoji_map.get(res.code, LogEmoji.ERROR),
                    "status": res.status,
                    "domain": res.domain if res.authenticated else "未登录（两站皆失效）",
                    "points": res.points,
                    "days": res.days if res.authenticated else "-",
                    "points_total": res.points_total if res.authenticated else "-",
                    "exchange": res.exchange,
                }
            )

        note = ""
        if fail_count:
            note = (
                "🛠 修复：为每个账号开独立无痕窗口，登录 glados.cloud（旧号可能已迁移到 railgun.info），"
                "F12 → Network → 刷新 → 复制完整 Cookie（需含 gld:sess 与 gld:sess.sig），"
                "用 & 拼接后更新 Secret GLADOS_COOKIES。不要在同一个窗口切换登录多个账号，会互相吊销会话。"
            )

        return {
            "title": title,
            "rows": rows,
            "note": note,
            "success": success_count,
            "fail": fail_count,
            "repeat": repeat_count,
            "total_tasks": len(raw_results),
        }


def _disp_width(text: str) -> int:
    """按终端显示宽度计算（CJK 全角字符按 2 列），用于对齐表格"""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    """右侧补空格到指定显示宽度"""
    return str(text) + " " * max(0, width - _disp_width(text))


def render_summary_table(best: List[CheckinResult]) -> str:
    """生成对齐的控制台摘要表"""
    headers = [("账号", 6), ("状态", 12), ("域名", 18), ("总积分", 12), ("剩余天数", 12), ("兑换", 18)]
    lines = ["", "=" * 80, "📊 GLaDOS 签到执行摘要", "=" * 80]
    lines.append("".join(_pad(name, width) for name, width in headers))
    lines.append("-" * 80)
    for res in best:
        cells = [
            (res.cookie_index, 6),
            (res.status, 12),
            (res.domain if res.authenticated else "-", 18),
            (res.points_total if res.authenticated else "-", 12),
            (res.days if res.authenticated else "-", 12),
            (res.exchange, 18),
        ]
        lines.append("".join(_pad(value, width) for value, width in cells))
    lines.append("-" * 80)
    lines.append("=" * 80)
    return "\n".join(lines)


class AccountSummary:
    """账号级统计（供 main 输出 annotation 用）"""

    def __init__(self, payload: dict):
        self.title = payload["title"]
        self.success = payload.get("success", 0)
        self.repeat = payload.get("repeat", 0)
        self.fail = payload.get("fail", 0)

    def annotation(self) -> Optional[str]:
        if self.fail and not self.success and not self.repeat:
            return f"::error::GLaDOS 签到全部失败：{self.fail} 个账号 Cookie 失效或服务端拒绝，请更新 GLADOS_COOKIES"
        if self.fail:
            return f"::warning::GLaDOS 签到部分失败：{self.fail} 个账号失败（{self.title}）"
        return None


logger = init_logger()


def main():
    """主函数（账号失败不算运行失败，保持 Actions 绿色；只有脚本异常才 exit 1）"""
    exit_code = 0
    config: Optional[Config] = None

    try:
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()

        if not config.cookies_list:
            logger.error(f"{LogEmoji.ERROR} 未找到有效的 Cookie, 退出程序。")
            print("::error::GLADOS_COOKIES 未设置或为空，无法签到")
            payload = {"title": "失败 | GLaDOS 签到 ❌未配置 Cookie", "rows": [], "note": "请先配置 Secret GLADOS_COOKIES"}
        else:
            logger.info(f"{LogEmoji.START} 步骤 2: 执行签到")
            checker = Checker(config)
            checker.checkin_all()

            logger.info(f"{LogEmoji.START} 步骤 3: 汇总结果")
            payload = checker.format_results()
            best = checker.best_per_account()
            try:
                print(render_summary_table(best))
            except Exception as e:  # 打印失败不该影响签到结果与推送
                logger.warning(f"{LogEmoji.WARNING} 摘要表打印失败（不影响签到）: {e}")
            logger.info(f"\n{LogEmoji.END} ========== 签到总结 ==========\n{payload['title']}\n{render_payload(payload)}")

            annotation = AccountSummary(payload).annotation()
            if annotation:
                print(annotation)

    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        payload = {"title": "失败 | GLaDOS 脚本异常", "rows": [], "note": str(e)}
        print(f"::error::GLaDOS 签到脚本异常: {e}")
        exit_code = 1

    logger.info(f"{LogEmoji.START} 步骤 4: 发送推送")
    PushService(config).send(payload["title"], payload)
    logger.info(f"{LogEmoji.END} 签到完成")
    logger.info(f"{LogEmoji.INFO} 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")

    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
