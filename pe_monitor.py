#!/usr/bin/env python3
"""
沪深300市盈率监控信号系统 (PE Monitor)
=====================================
每日抓取乐咕乐股沪深300静态市盈率中位数，根据市场阶段逻辑计算信号，
通过飞书/微信/邮件发送通知。

用法:
    python pe_monitor.py                  # 正常运行（抓取+计算+发送）
    python pe_monitor.py --test 21.12     # 测试模式（手动输入C值）
    python pe_monitor.py --dry-run        # 干跑模式（只计算不发送）
    python pe_monitor.py --show-state     # 查看当前状态
"""

import json
import os
import sys
import argparse
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("缺少依赖，请运行: pip install requests beautifulsoup4 pyyaml")
    sys.exit(1)

try:
    import yaml
except ImportError:
    yaml = None

# ============================================================
# 常量
# ============================================================
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
DATA_FILE = DATA_DIR / "history.json"
CONFIG_FILE = SCRIPT_DIR / "config.yaml"

SCRAP_URL = "https://legulegu.com/stockdata/hs300-ttm-lyr"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# 信号定义
# ============================================================
# daily=True: 稳态信号，每天C在此范围内都发送
# send_days=N: 过渡信号，首次触发后连续发N天
# include_c=True: 消息中包含C值
SIGNALS = {
    "a": {
        "msg": "注意！历史大底！！！请加杠杆抄底！！！参考代码510310",
        "daily": True, "include_c": True,
    },
    "b": {
        "msg": "请ALLIN !!!参考代码510310",
        "daily": True, "include_c": True,
    },
    "c": {
        "msg": "请建仓至少到80%参考代码510310",
        "daily": True, "include_c": True,
    },
    "d": {
        "msg": "请建仓至少到50% 参考代码510310",
        "daily": True, "include_c": True,
    },
    "e": {
        "msg": "请建仓20%-50% 参考代码510310",
        "send_days": 2, "include_c": True,
    },
    "f": {
        "msg": "根据个人风险偏好建仓，仓位超过50的请保持持仓，并关注大盘上涨趋势，考虑补仓。",
        "send_days": 2, "include_c": True,
    },
    "g": {
        "msg": "建议持仓。",
        "send_days": 2, "include_c": False,
    },
    "h": {
        "msg": "初级牛市市场！请关注近期股市情况，准备减仓",
        "send_days": 2, "include_c": True,
    },
    "i": {
        "msg": "初级牛市市场！根据市场情况和个人风险偏好，减仓20~50%",
        "daily": True, "include_c": True,
    },
    "j": {
        "msg": "牛市市场！根据市场情况和个人风险偏好，再减仓20~50%，留20%~40%底仓",
        "daily": True, "include_c": True,
    },
    "k": {
        "msg": "牛市市场！根据市场情况和个人风险偏好，留0%~20%~40%底仓 剩余仓位根据市场情况待涨博取高风险收益",
        "daily": True, "include_c": True,
    },
    "l": {
        "msg": "大牛市来了！根据市场情况和个人风险偏好，逢高减仓",
        "send_days": 2, "include_c": True,
    },
    "m": {
        "msg": "BIGBANG!大牛市！根据市场情况和个人风险偏好，逢高减仓",
        "send_days": 2, "include_c": True,
    },
    "n": {
        "msg": "疯牛！请减仓清仓！",
        "send_days": 2, "include_c": True,
    },
    "o": {
        "msg": "史无前例疯牛！请清仓回家买玛莎！",
        "send_days": 2, "include_c": True,
    },
    "p": {
        "msg": "请关注股市，历史低位可能即将到来！",
        "send_days": 2, "include_c": True,
    },
    "q": {
        "msg": "请关注股市，低位到来,逢低建仓！",
        "send_days": 2, "include_c": True,
    },
    "r": {
        "msg": "小牛市反复，根据市场情况考虑减仓",
        "send_days": 2, "include_c": False,
    },
    "s": {
        "msg": "小牛市反复，根据市场情况考虑减仓",
        "send_days": 2, "include_c": False,
    },
    "t": {
        "msg": "牛市回调或转向，根据市场情况考虑减仓",
        "send_days": 2, "include_c": False,
    },
    "u": {
        "msg": "牛市回调或转向，根据市场情况考虑减仓",
        "send_days": 2, "include_c": False,
    },
    "v": {
        "msg": "",
        "send_days": 0, "include_c": False,
    },
}

# 信号 → 市场阶段描述 (用于日志和状态追踪)
SIGNAL_LABELS = {
    "a": "历史大底", "b": "极度低估", "c": "严重低估", "d": "低估",
    "e": "从底部回升(20-21)", "f": "进入正常偏低(21-25)", "g": "正常偏低(25-30)",
    "h": "初级牛市起步(30-33)",
    "i": "初级牛市", "j": "牛市", "k": "强牛市",
    "l": "大牛市(40-49)", "m": "超级大牛市(49-55)", "n": "疯牛(55-75)", "o": "史无前例疯牛(>75)",
    "p": "从高位回落(21-23)", "q": "从高位回落(20-21)",
    "r": "牛市反复回落(21-30)", "s": "牛市反复回落(30-33)",
    "t": "牛市回调(33-35)", "u": "牛市回调(35-38)",
    "v": "无信号",
}


# ============================================================
# 配置加载
# ============================================================
def load_config():
    """加载配置文件，优先从 config.yaml，其次从环境变量"""
    config = {
        "feishu": {"enabled": False, "webhook_url": ""},
        "wechat": {"enabled": False, "sctkey": ""},
        "email": {"enabled": False, "smtp_server": "", "smtp_port": 465,
                  "smtp_user": "", "smtp_password": "", "recipients": []},
        "notify_mode": "full",  # full=含C值, simple=不含C值
    }

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                if yaml:
                    user_cfg = yaml.safe_load(f) or {}
                else:
                    # Fallback: simple JSON-like parsing
                    import json
                    user_cfg = json.loads(f.read())
                deep_merge(config, user_cfg)
            logger.info(f"配置已加载: {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"配置加载失败: {e}，使用默认值")

    # 环境变量覆盖（适合 GitHub Actions secrets）
    if os.getenv("FEISHU_WEBHOOK"):
        config["feishu"]["webhook_url"] = os.getenv("FEISHU_WEBHOOK")
        config["feishu"]["enabled"] = True
    if os.getenv("WECHAT_SCTKEY"):
        config["wechat"]["sctkey"] = os.getenv("WECHAT_SCTKEY")
        config["wechat"]["enabled"] = True
    if os.getenv("NOTIFY_MODE"):
        config["notify_mode"] = os.getenv("NOTIFY_MODE")

    return config


def deep_merge(base, override):
    """递归合并字典"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


# ============================================================
# 数据持久化
# ============================================================
def load_state():
    """加载历史数据和状态"""
    default_state = {
        "history": [],          # [{date: "YYYY-MM-DD", value: float}, ...]
        "max_c": 0,             # 历史最高C值
        "last_signal": None,    # 上次信号
        "last_signal_date": None,
        "consecutive_days": 0,  # 当前信号连续天数
        "held_signal": None,    # 被持有的过渡信号
        "held_until": None,     # 持有截止日期
        # 下降穿越触发标记
        "fired": {},
    }

    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            deep_merge(default_state, saved)
            logger.info(f"状态已加载，共 {len(default_state['history'])} 条历史记录")
        except Exception as e:
            logger.warning(f"状态加载失败: {e}，使用默认值")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    return default_state


def save_state(state):
    """保存状态到文件"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info(f"状态已保存: {DATA_FILE}")


# ============================================================
# 数据抓取
# ============================================================
def scrape_pe():
    """抓取乐咕乐股沪深300静态市盈率中位数"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        resp = requests.get(SCRAP_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"

        soup = BeautifulSoup(resp.text, "html.parser")

        # 方法1: 通过 class 查找表格
        table = soup.find("table", class_="lg-data-chart-description-table")
        if table:
            value = _extract_from_table(table)
            if value is not None:
                return value

        # 方法2: 通过文本匹配 td
        value = _extract_by_text(soup)
        if value is not None:
            return value

        logger.error("未找到'沪深300静态市盈率中位数'数据")
        return None

    except Exception as e:
        logger.error(f"抓取失败: {e}")
        return None


def _extract_from_table(table):
    """从表格中提取值"""
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            label = tds[0].get_text(strip=True)
            if "沪深300静态市盈率中位数" in label:
                value_text = tds[1].get_text(strip=True)
                value = float(value_text)
                logger.info(f"抓取成功: {label} = {value}")
                return value
    return None


def _extract_by_text(soup):
    """通过文本内容查找"""
    for td in soup.find_all("td"):
        if "沪深300静态市盈率中位数" in td.get_text(strip=True):
            next_td = td.find_next_sibling("td")
            if next_td:
                try:
                    value = float(next_td.get_text(strip=True))
                    logger.info(f"抓取成功(文本匹配): 沪深300静态市盈率中位数 = {value}")
                    return value
                except ValueError:
                    pass
    return None


# ============================================================
# 信号计算核心
# ============================================================
def calc_signal(c, state):
    """
    根据当前C值和历史状态计算信号。

    参数:
        c: 当前沪深300静态市盈率中位数 (float)
        state: 状态字典 (会被原地修改)

    返回:
        signal: 信号代码 (a-v)

    注意: 前值 = 历史所有已记录的C值（不含当天的c），
          信号计算基于前值判断，之后再更新max_c和历史记录。
    """
    if c <= 0:
        return "v"

    today_str = str(date.today())
    history = state.get("history", [])
    prev_values = [h["value"] for h in history]
    # 前值最高 = 所有历史记录的最大值（不含当天）
    prev_max = max(prev_values) if prev_values else 0
    fired = state.setdefault("fired", {})

    # ---- 重置下降穿越标记（当C回升超过阈值时） ----
    if c > 23: fired.pop("p", None)
    if c > 21: fired.pop("q", None)
    if c >= 30: fired.pop("r", None)
    if c >= 33: fired.pop("s", None)
    if c >= 35: fired.pop("t", None)
    if c >= 38: fired.pop("u", None)

    # ---- 1. 上升穿越信号（前值<X，第一次达到） ----
    # 前值 = 历史记录，不含当天
    if prev_max < 20 and 20 < c <= 21:
        return "e"
    if prev_max < 21 and 21 < c <= 25:
        return "f"
    if prev_max < 25 and 25 < c <= 30:
        return "g"
    if prev_max < 30 and 30 < c <= 33:
        return "h"
    if prev_max < 40 and 40 < c <= 49:
        return "l"
    if prev_max < 49 and 49 < c <= 55:
        return "m"
    if prev_max < 55 and 55 < c <= 75:
        return "n"
    if prev_max < 75 and c > 75:
        return "o"

    # ---- 2. 下降穿越信号（前值>X，第一次降到） ----
    # 使用历史最高值判断是否曾处于高位
    max_c = state.get("max_c", 0)
    if max_c > 23 and 21 < c <= 23 and "p" not in fired:
        fired["p"] = today_str
        return "p"
    if max_c > 21 and 20 < c <= 21 and "q" not in fired:
        fired["q"] = today_str
        return "q"
    if max_c >= 30 and 21 <= c < 30 and "r" not in fired:
        fired["r"] = today_str
        return "r"
    if max_c >= 33 and 30 <= c < 33 and "s" not in fired:
        fired["s"] = today_str
        return "s"
    if max_c >= 35 and 33 <= c < 35 and "t" not in fired:
        fired["t"] = today_str
        return "t"
    if max_c >= 38 and 35 <= c < 38 and "u" not in fired:
        fired["u"] = today_str
        return "u"

    # ---- 3. 稳态区间信号（每天C在此范围内都触发） ----
    if 0 < c <= 16:
        return "a"
    if 16 < c <= 17:
        return "b"
    if 17 < c <= 19:
        return "c"
    if 19 < c <= 20:
        return "d"
    if 33 < c <= 35:
        return "i"
    if 35 < c <= 38:
        return "j"
    if 38 < c <= 40:
        return "k"

    # ---- 4. 无信号 ----
    return "v"


def process_daily(c, state):
    """
    处理每日逻辑: 计算信号，判断是否需要发送通知。

    返回:
        (signal, should_send) 或 (None, False)
    """
    # 先计算今天的真实信号
    signal = calc_signal(c, state)

    # 清除已过期的持有信号
    held_signal = state.get("held_signal")
    held_until = state.get("held_until")
    if held_signal and held_until:
        try:
            until = date.fromisoformat(held_until)
            if date.today() > until:
                state["held_signal"] = None
                state["held_until"] = None
                held_signal = None
        except (ValueError, TypeError):
            state["held_signal"] = None
            state["held_until"] = None
            held_signal = None

    # 如果今天有新信号，优先用新信号（中断持有）
    if signal != "v":
        logger.info(f"今日信号 {signal}，覆盖持有信号 {held_signal}")
        state["held_signal"] = None
        state["held_until"] = None
    elif held_signal:
        # 今天无信号，但持有信号还在有效期内，继续发送
        try:
            until = date.fromisoformat(held_until)
            if date.today() <= until:
                logger.info(f"持有信号 {held_signal} 有效至 {held_until}")
                signal = held_signal
            else:
                state["held_signal"] = None
                state["held_until"] = None
                return None, False
        except (ValueError, TypeError):
            return None, False
    else:
        return None, False

    # 更新连续天数
    last_signal = state.get("last_signal")
    consecutive = state.get("consecutive_days", 0)

    if signal != last_signal:
        consecutive = 1
    else:
        consecutive += 1

    state["last_signal"] = signal
    state["last_signal_date"] = str(date.today())
    state["consecutive_days"] = consecutive

    sig_def = SIGNALS.get(signal, {})

    # 稳态信号: 每天都发送
    if sig_def.get("daily"):
        return signal, True

    # 过渡信号: 检查 send_days
    send_days = sig_def.get("send_days", 0)
    if consecutive <= send_days:
        # 设置持有期（确保即使明天信号变了也能继续发送）
        if consecutive == 1 and send_days > 1:
            hold_end = date.today() + timedelta(days=send_days - 1)
            state["held_signal"] = signal
            state["held_until"] = str(hold_end)
            logger.info(f"过渡信号 {signal} 将持有至 {hold_end}")
        return signal, True

    return None, False


def format_message(signal, c, notify_mode="full"):
    """格式化通知消息"""
    sig = SIGNALS.get(signal, {})
    msg = sig.get("msg", "")

    if not msg:
        return None

    header = f"📊 沪深300市盈率监控信号 [{signal.upper()}]"
    label = SIGNAL_LABELS.get(signal, "")
    date_str = date.today().strftime("%Y-%m-%d")

    parts = [f"**{header}**", f"📅 {date_str}", f"📍 阶段: {label}", "", f"💡 {msg}"]

    if sig.get("include_c", False) and notify_mode == "full":
        parts.append(f"\n📈 今日沪深300静态市盈率中位数: **{c:.2f}**")

    parts.append(f"\n📊 参考代码: 510310 (沪深300ETF)")
    parts.append(f"📡 数据来源: {SCRAP_URL}")

    return "\n".join(parts)


# ============================================================
# 通知发送
# ============================================================
def send_feishu(msg, webhook_url):
    """发送飞书群机器人消息"""
    if not webhook_url:
        logger.warning("飞书 webhook URL 未配置")
        return False
    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "📊 沪深300 PE 监控信号"},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": msg},
                ],
            },
        }
        resp = requests.post(webhook_url, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            logger.info("飞书通知发送成功")
            return True
        else:
            logger.error(f"飞书通知失败: {result}")
            return False
    except Exception as e:
        logger.error(f"飞书通知异常: {e}")
        return False


def send_wechat_sct(msg, sctkey):
    """通过 Server酱 发送微信通知"""
    if not sctkey:
        logger.warning("Server酱 SendKey 未配置")
        return False
    try:
        # 提取纯文本标题
        title = msg.split("\n")[0].replace("**", "").replace("*", "")
        # 去除 markdown 格式
        desp = msg.replace("**", "").replace("*", "")
        resp = requests.get(
            f"https://sctapi.ftqq.com/{sctkey}.send",
            params={"title": title, "desp": desp},
            timeout=10,
        )
        result = resp.json()
        if result.get("code") == 0:
            logger.info("微信(Server酱)通知发送成功")
            return True
        else:
            logger.error(f"微信(Server酱)通知失败: {result}")
            return False
    except Exception as e:
        logger.error(f"微信(Server酱)通知异常: {e}")
        return False


def send_email(msg, config):
    """发送邮件通知"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        smtp_server = config.get("smtp_server", "")
        smtp_port = config.get("smtp_port", 465)
        smtp_user = config.get("smtp_user", "")
        smtp_password = config.get("smtp_password", "")
        recipients = config.get("recipients", [])

        if not all([smtp_server, smtp_user, smtp_password, recipients]):
            logger.warning("邮件配置不完整")
            return False

        # 纯文本版本
        plain_msg = msg.replace("**", "").replace("*", "")

        email_msg = MIMEMultipart("alternative")
        email_msg["Subject"] = f"📊 沪深300 PE 监控信号 - {date.today()}"
        email_msg["From"] = smtp_user
        email_msg["To"] = ", ".join(recipients)

        email_msg.attach(MIMEText(plain_msg, "plain", "utf-8"))
        # 简单 HTML 版本
        html_msg = msg.replace("\n", "<br>").replace("**", "<b>").replace("*", "<b>")
        email_msg.attach(MIMEText(html_msg, "html", "utf-8"))

        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, email_msg.as_string())

        logger.info(f"邮件通知发送成功 → {recipients}")
        return True
    except Exception as e:
        logger.error(f"邮件通知异常: {e}")
        return False


def send_all(msg, config):
    """发送所有启用的通知渠道"""
    results = {}

    # 飞书
    if config.get("feishu", {}).get("enabled"):
        webhook = config["feishu"].get("webhook_url", "")
        results["feishu"] = send_feishu(msg, webhook)

    # 微信 (Server酱)
    if config.get("wechat", {}).get("enabled"):
        sctkey = config["wechat"].get("sctkey", "")
        results["wechat"] = send_wechat_sct(msg, sctkey)

    # 邮件
    if config.get("email", {}).get("enabled"):
        results["email"] = send_email(msg, config["email"])

    return results


# ============================================================
# 状态展示
# ============================================================
def show_state(state):
    """打印当前状态摘要"""
    history = state.get("history", [])
    print("=" * 60)
    print("📊 沪深300 PE 监控 - 当前状态")
    print("=" * 60)

    if history:
        latest = history[-1]
        print(f"📅 最新数据: {latest['date']}  C = {latest['value']}")
        print(f"📈 历史最高: {state.get('max_c', 0):.2f}")
        print(f"📝 记录总数: {len(history)}")

        if len(history) >= 2:
            prev = history[-2]
            change = latest["value"] - prev["value"]
            arrow = "↑" if change > 0 else ("↓" if change < 0 else "→")
            print(f"📊 较上次变化: {arrow} {abs(change):.2f}")
    else:
        print("📭 暂无历史数据")

    last_signal = state.get("last_signal")
    if last_signal:
        label = SIGNAL_LABELS.get(last_signal, "")
        print(f"🔔 上次信号: {last_signal} ({label})")
        print(f"📅 信号日期: {state.get('last_signal_date')}")
        print(f"🔢 连续天数: {state.get('consecutive_days', 0)}")

    held = state.get("held_signal")
    if held:
        print(f"📌 持有信号: {held} (至 {state.get('held_until')})")

    fired = state.get("fired", {})
    if fired:
        print(f"🚫 已触发下降穿越: {', '.join(fired.keys())}")

    print("=" * 60)


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="沪深300市盈率监控信号系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pe_monitor.py --test 21.12    # 用 21.12 测试
  python pe_monitor.py --test 15.0     # 模拟历史大底
  python pe_monitor.py --dry-run       # 只计算不发送
  python pe_monitor.py --show-state    # 查看当前状态
        """,
    )
    parser.add_argument("--test", type=float, help="测试模式：手动输入C值")
    parser.add_argument("--dry-run", action="store_true", help="干跑模式：计算但不发送通知")
    parser.add_argument("--show-state", action="store_true", help="查看当前状态和历史数据")
    parser.add_argument("--mode", choices=["full", "simple"], default=None,
                        help="通知模式: full=含C值, simple=不含C值")

    args = parser.parse_args()

    # 加载配置和状态
    config = load_config()
    state = load_state()

    # 查看状态
    if args.show_state:
        show_state(state)
        return

    # 覆盖通知模式
    if args.mode:
        config["notify_mode"] = args.mode

    # 获取C值
    test_env = os.getenv("PE_TEST_VALUE")
    github_actions = os.getenv("GITHUB_ACTIONS") == "true"

    if args.test is not None:
        c = args.test
        print(f"\n🧪 [测试模式] 手动输入 C = {c}")
    elif test_env and not github_actions:
        c = float(test_env)
        print(f"\n🧪 [测试模式] 环境变量 PE_TEST_VALUE = {c}")
    else:
        print(f"\n🌐 正在抓取 {SCRAP_URL} ...")
        c = scrape_pe()
        if c is None:
            logger.error("数据抓取失败，退出")
            sys.exit(1)

    # 计算信号（使用不含当天的历史数据）
    signal, should_send = process_daily(c, state)

    # 记录历史（在信号计算之后）
    today_str = str(date.today())
    if state["history"] and state["history"][-1]["date"] == today_str:
        state["history"][-1]["value"] = c
        logger.info(f"更新今日数据: {today_str} → {c}")
    else:
        state["history"].append({"date": today_str, "value": c})
        logger.info(f"新增数据: {today_str} → {c}")

    # 更新历史最高值
    if c > state.get("max_c", 0):
        state["max_c"] = c

    # 显示结果
    print(f"\n{'=' * 60}")
    print(f"📈 今日C值: {c:.2f}")
    print(f"📈 历史最高: {state['max_c']:.2f}")

    if signal:
        label = SIGNAL_LABELS.get(signal, "")
        print(f"🔔 信号: {signal.upper()} ({label})")
        print(f"📤 是否发送: {'是' if should_send else '否'}")
    else:
        print("🔇 信号: 无 (v)")

    print(f"{'=' * 60}")

    # 发送通知
    if signal and should_send:
        msg = format_message(signal, c, config.get("notify_mode", "full"))
        if msg:
            print(f"\n📱 消息内容:\n{msg}\n")

            if args.dry_run:
                print("🏃 [干跑模式] 跳过通知发送")
            else:
                results = send_all(msg, config)
                for channel, ok in results.items():
                    status = "✅ 成功" if ok else "❌ 失败"
                    print(f"  {channel}: {status}")
    elif args.dry_run:
        print("🏃 [干跑模式] 无需发送通知")

    # 保存状态
    save_state(state)


if __name__ == "__main__":
    main()
