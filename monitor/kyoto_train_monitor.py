#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京都周辺 電車運行異常モニター v1
=================================

出租车司机用：京都相关线路出现運転見合わせ/大幅延误（→振替輸送→车站前
出租车需求激增）时，通过 ntfy 推送 + 邮件立刻通知。

数据源:
  1. JR西日本（可靠）: 列車走行位置サービスの運行情報JSON
     https://www.train-guide.westjr.co.jp/api/v3/area_kinki_trafficinfo.json
     - 有 transfer 字段 = 振替輸送実施中
  2. 私铁（实验性）: 阪急/京阪/近铁 官方運行情報页面的关键词扫描
     - 解析失败或无法判断时只记日志、不报警（宁漏勿误）

设计原则:
  - 报"变化"不报"状态"：异常发生时报一次，恢复时报一次
  - 状态持久化在 state.json（由 GitHub Actions 提交回仓库）
  - 深夜 1:00-4:30 JST 静默（电车不运行，此时的状态无意义）
  - 零第三方依赖，Python 3.9+ 标准库即可

环境变量:
  NTFY_TOPIC     ntfy 频道名（必填，例: taxilogger-kyoto-a8x3k）
  NOTIFY_EMAIL   报警同时发送到的邮箱（可选; 经 ntfy 转发, 有每日条数限制）
  DRY_RUN=1      只打印不推送（本地测试用）
  MOCK_DIR=path  从本地文件读取数据代替 HTTP（测试用）
  QUIET=off      关闭深夜静默
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 配置

JST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).parent / "state.json"
NTFY_URL = "https://ntfy.sh"
HTTP_TIMEOUT = 20
USER_AGENT = "KyotoTrainMonitor/1.0 (personal use; polls every 10min)"

# JR西日本 近畿エリア: 线路key → 名称（京都相关的子集才报警）
JR_KINKI_URL = "https://www.train-guide.westjr.co.jp/api/v3/area_kinki_trafficinfo.json"
JR_LINE_NAMES = {
    "kyoto": "JR京都線",
    "kosei": "湖西線",
    "nara": "奈良線",
    "sagano": "嵯峨野線",
    "hokurikubiwako": "北陸線・琵琶湖線",
    "kusatsu": "草津線",
    "sanin1": "山陰線(園部-福知山)",
    "yamatoji": "大和路線",
}
# 上表之外的近畿线路（大阪環状線等）不报警，避免噪音。
# 想扩大范围就往 JR_LINE_NAMES 里加 key（完整对照见 README）。

# 停运级别关键词（高优先级推送）
SUSPEND_WORDS = ("運転見合わせ", "運転取り止め", "運休", "見合わせ")

# 私铁页面监视（实验性）
PAGE_WATCHES = [
    {
        "id": "hankyu",
        "company": "阪急電鉄",
        "url": "https://www.hankyu.co.jp/railinfo/",
        # 页面固定说明文里含「遅れ」等词，必须先剔除再扫描
        "boilerplate": [
            "20分以上の遅れが発生した、もしくは見込まれる場合に情報を提供",
            "遅延証明書",
        ],
        "normal_markers": ["平常通り", "平常どおり", "平常運転"],
        "alert_words": ["運転見合わせ", "運転を見合わせ", "運休", "遅れが発生", "遅延が発生"],
        "stations_hint": "桂・烏丸・京都河原町",
    },
    {
        "id": "keihan",
        "company": "京阪電車",
        "url": "https://www.okeihan.net/",
        "boilerplate": ["遅延証明書"],
        "normal_markers": ["平常通り", "平常どおり", "平常運転", "遅れはございません"],
        "alert_words": ["運転見合わせ", "運転を見合わせ", "運休", "遅れが発生", "遅延が発生"],
        "stations_hint": "祇園四条・三条・出町柳",
    },
    {
        "id": "kintetsu",
        "company": "近鉄",
        "url": "https://www.kintetsu.co.jp/unkou/unkou.html",
        "boilerplate": ["遅延証明書"],
        "normal_markers": ["支障はございません", "平常通り", "平常どおり", "平常運転"],
        "alert_words": ["運転見合わせ", "運転を見合わせ", "運休", "遅れが発生", "遅延が発生"],
        "stations_hint": "近鉄京都駅・丹波橋",
    },
]

FAILURE_ALERT_THRESHOLD = 3  # 连续失败 N 次才报一次数据源异常

# ---------------------------------------------------------------- 基础设施


def log(msg: str) -> None:
    print(f"[{datetime.now(JST).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch(url: str) -> str:
    """取 URL 文本。MOCK_DIR 模式下读本地文件（文件名=简化的url）。"""
    mock_dir = os.environ.get("MOCK_DIR")
    if mock_dir:
        name = re.sub(r"[^a-z0-9.]+", "_", url.lower())
        path = Path(mock_dir) / name
        return path.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("state.json 损坏，重置")
    return {"active": {}, "failures": {}, "failure_alerted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )


def in_quiet_hours(now: datetime) -> bool:
    if os.environ.get("QUIET") == "off":
        return False
    minutes = now.hour * 60 + now.minute
    return 60 <= minutes < 270  # 1:00 - 4:30 JST


def notify(title: str, message: str, priority: int, with_email: bool) -> None:
    """经 ntfy 推送；with_email 时同时转发邮件。DRY_RUN 只打印。"""
    topic = os.environ.get("NTFY_TOPIC", "")
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": ["train", "kyoto"],
    }
    email = os.environ.get("NOTIFY_EMAIL")
    if with_email and email:
        payload["email"] = email

    if os.environ.get("DRY_RUN") or not topic:
        log(f"DRY-RUN 推送: {json.dumps(payload, ensure_ascii=False)}")
        return

    req = urllib.request.Request(
        NTFY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        resp.read()
    log(f"已推送: {title}")


# ---------------------------------------------------------------- Provider

# 每个 provider 返回 incidents: {唯一id: {company, line, status, cause, transfer, hint}}


def check_jr_west() -> dict:
    data = json.loads(fetch(JR_KINKI_URL))
    incidents = {}
    for key, info in (data.get("lines") or {}).items():
        if key not in JR_LINE_NAMES:
            continue  # 京都无关线路，跳过
        status = str(info.get("status", "運行情報あり"))
        incidents[f"jrwest:{key}"] = {
            "company": "JR西日本",
            "line": JR_LINE_NAMES[key],
            "status": status,
            "cause": str(info.get("cause", "") or ""),
            "transfer": bool(info.get("transfer")),
            "hint": "京都駅・山科駅",
        }
    return incidents


def check_page(watch: dict) -> dict:
    text = strip_tags(fetch(watch["url"]))
    for phrase in watch["boilerplate"]:
        text = text.replace(phrase, " ")

    if any(marker in text for marker in watch["normal_markers"]):
        return {}

    matched = [w for w in watch["alert_words"] if w in text]
    if matched:
        return {
            f'{watch["id"]}:page': {
                "company": watch["company"],
                "line": "運行情報",
                "status": matched[0],
                "cause": "",
                "transfer": False,
                "hint": watch["stations_hint"],
            }
        }

    # 既无平常标记也无异常关键词：无法判断 → 只记日志（宁漏勿误）
    log(f'{watch["company"]}: 页面无法判定（unknown），跳过。片段: {text[:120]}')
    return {}


# ---------------------------------------------------------------- 主逻辑


def build_alert_message(inc: dict) -> tuple:
    suspended = any(w in inc["status"] for w in SUSPEND_WORDS)
    icon = "🚨" if suspended else "⚠️"
    title = f'{icon} {inc["company"]} {inc["line"]}: {inc["status"]}'

    lines = []
    if inc["cause"]:
        lines.append(f'原因: {inc["cause"]}')
    if inc["transfer"]:
        lines.append("🔁 振替輸送実施中 → 车站前出租车需求可能激增！")
    elif suspended:
        lines.append("停运级别异常，可能启动振替輸送")
    lines.append(f'关注车站: {inc["hint"]}')
    lines.append(f'{datetime.now(JST).strftime("%H:%M")} JST')

    priority = 5 if (suspended or inc["transfer"]) else 4
    return title, "\n".join(lines), priority


def run() -> int:
    now = datetime.now(JST)
    state = load_state()
    previous = state.get("active", {})
    current = {}

    providers = [("jrwest", check_jr_west)] + [
        (w["id"], (lambda w=w: check_page(w))) for w in PAGE_WATCHES
    ]

    for pid, func in providers:
        try:
            current.update(func())
            state["failures"][pid] = 0
            state["failure_alerted"][pid] = False
        except Exception as e:
            n = state["failures"].get(pid, 0) + 1
            state["failures"][pid] = n
            log(f"{pid} 获取失败({n}次): {e}")
            # 失败时保留该 provider 上次的 active 条目，避免误报"恢复"
            for key, inc in previous.items():
                if key.startswith(pid + ":"):
                    current[key] = inc
            if n >= FAILURE_ALERT_THRESHOLD and not state["failure_alerted"].get(pid):
                state["failure_alerted"][pid] = True
                if not in_quiet_hours(now):
                    notify(f"📡 监控数据源异常: {pid}",
                           f"连续 {n} 次获取失败，该数据源的报警暂不可用。", 3, False)

    quiet = in_quiet_hours(now)

    # 新异常
    for key, inc in current.items():
        prev = previous.get(key)
        if prev is None:
            log(f'新异常: {key} {inc["status"]}')
            if not quiet:
                title, message, priority = build_alert_message(inc)
                notify(title, message, priority, with_email=True)
        elif (prev.get("status"), prev.get("transfer")) != (inc["status"], inc["transfer"]):
            # 状态升级/变化（例: 遅延→運転見合わせ、振替輸送开始）
            log(f'状态变化: {key} {prev.get("status")} → {inc["status"]}')
            if not quiet:
                title, message, priority = build_alert_message(inc)
                title = "🔄 " + title.split(" ", 1)[1]  # 用🔄替换原图标
                notify(title, message, priority, with_email=True)

    # 恢复
    for key, prev in previous.items():
        if key not in current:
            log(f"恢复: {key}")
            if not quiet:
                notify(
                    f'✅ {prev.get("company","")} {prev.get("line","")} 恢复正常',
                    f'此前: {prev.get("status","")}\n{now.strftime("%H:%M")} JST',
                    3, with_email=False,
                )

    state["active"] = current
    state["checked_at"] = now.isoformat()
    save_state(state)
    log(f"完成: 当前异常 {len(current)} 件" + ("（深夜静默中）" if quiet else ""))
    return 0


if __name__ == "__main__":
    sys.exit(run())
