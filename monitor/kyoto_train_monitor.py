#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京都周辺 電車運行異常モニター v1.2.3
=================================

出租车司机用：京都相关线路出现運転見合わせ/大幅延误（→振替輸送→车站前
出租车需求激增）时，通过 ntfy 推送 + 邮件立刻通知。

数据源:
  1. JR西日本（可靠）: 列車走行位置サービスの運行情報JSON
     https://www.train-guide.westjr.co.jp/api/v3/area_kinki_trafficinfo.json
     - 有 transfer 字段 = 振替輸送実施中
  2. 私铁（实验性）: 阪急/京阪/近铁 官方運行情報页面的关键词扫描
     - 解析失败或无法判断时只记日志、不报警（宁漏勿误）
  3. 京都市営地下鉄 烏丸線・東西線（v1.2 新增, T-010）: 交通局首页の
     緊急情報块（▼▼緊急情報▼▼/▲▲緊急情報▲▲ 注释锚点之间）关键词扫描
     - 平常时块为空（2026-08-08 实测）；只扫块内文本,新闻标题不会误报
     - 锚点缺失=页面改版 → 按获取失败计数,连续 3 次自报警

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
from typing import Optional

# ---------------------------------------------------------------- 配置

JST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).parent / "state.json"
ALERTS_FILE = Path(__file__).parent / "alerts.jsonl"  # 警报历史留档（Phase 3 分析用）
# v1.2.1 心跳（T-010.1）：每轮覆盖写，供下游判「监控还活着吗」。
# 绝不写进 alerts.jsonl——留档保持纯警报事件，Phase 3 分析数据不掺心跳。
STATUS_FILE = Path(__file__).parent / "monitor_status.json"
NTFY_URL = "https://ntfy.sh"
HTTP_TIMEOUT = 20
USER_AGENT = "KyotoTrainMonitor/1.2.3 (personal use; polls every 10min)"

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

# 各線が止まった時に「実際に客が湧く駅」（設計稿 §4.2 の駅→区マッピングと同源）。
# v1.2.1 まで全 8 線が "京都駅・山科駅" 固定だった＝嵯峨野線が止まっても
# 嵐山ではなく京都駅へ誘導していた（司机の実地指摘, 2026-08-08 修正）。
# 選定ルール：スマホ通知で一目で読める上限＝主要 3-4 駅。§4.2 の全駅は載せず、
# 重み ★★★/★★ の駅を優先（嵯峨野の花園・丹波口・梅小路京都西、奈良線の六地蔵は
# 意図的に省略）。市外中心の 3 線は「行き先を指示しない」文言にする——§4.2 の
# 重みが 0.2 しかない線で駅名を断言すると、行動指示に化けて司机を空振りさせる。
JR_LINE_HINTS = {
    "kyoto": "京都駅",
    "kosei": "山科駅・京都駅",
    "nara": "東福寺・稲荷・桃山",
    "sagano": "嵯峨嵐山・二条・円町・太秦",
    "hokurikubiwako": "山科駅・京都駅",
    "kusatsu": "なし(市外中心・京都市内への影響は限定的)",
    "sanin1": "なし(市外中心・京都市内への影響は限定的)",
    "yamatoji": "なし(市外中心・京都市内への影響は限定的)",
}

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
            # App宣传语，含「運転見合わせ」——v1 误报的元凶 (2026-07-19 修复)
            "遅れや運転見合わせなどの情報をプッシュ通知",
            "遅延証明書",
        ],
        "normal_markers": ["平常通り", "平常どおり", "平常運転"],
        "alert_words": ["運転見合わせ", "運転を見合わせ", "運休", "遅れが発生", "遅延が発生"],
        "lines": {
            "京都本線": "阪急京都線", "京都線": "阪急京都線",
            "嵐山線": "阪急嵐山線",                    # §4.2 単列（本線警報では立てない）
            "神戸本線": "阪急神戸線", "神戸線": "阪急神戸線",
            "宝塚本線": "阪急宝塚線", "宝塚線": "阪急宝塚線",
            "千里線": "阪急千里線", "今津線": "阪急今津線",
        },
    },
    {
        "id": "keihan",
        "company": "京阪電車",
        "url": "https://www.okeihan.net/",
        "boilerplate": ["遅延証明書"],
        "normal_markers": ["平常通り", "平常どおり", "平常運転", "遅れはございません"],
        "alert_words": ["運転見合わせ", "運転を見合わせ", "運休", "遅れが発生", "遅延が発生"],
        "lines": {
            "京阪本線": "京阪本線", "本線": "京阪本線",
            "鴨東線": "京阪本線",                      # 三条-出町柳は本線と直通運転＝§4.2 の本線行に含む
            "宇治線": "京阪宇治線", "交野線": "京阪交野線",
            "大津線": "京阪大津線", "石山坂本線": "京阪大津線", "京津線": "京阪大津線",
        },
    },
    {
        "id": "kintetsu",
        "company": "近鉄",
        # v1 的 co.jp 域名是 404，正确域名是 kintetsu.jp (2026-07-19 修复)
        "url": "https://www.kintetsu.jp/unkou/unkou.html",
        "boilerplate": [
            # 固定说明文，含「見合わせ」「遅れ」——先剔除再扫描
            "遅延・運行見合わせ情報をご確認いただけます",
            "5分以上の遅れが生じている場合",
            "15分以上の遅れがない場合",
            "遅延証明書",
        ],
        "normal_markers": ["支障はございません", "平常通り", "平常どおり", "平常運転"],
        "alert_words": ["運転見合わせ", "運転を見合わせ", "運行見合わせ", "運休", "遅れが発生", "遅延が発生"],
        "lines": {
            "京都線": "近鉄京都線",
            "奈良線": "近鉄奈良線",                    # ⚠️ JR奈良線と衝突させない（社名前置が必須）
            "大阪線": "近鉄大阪線", "南大阪線": "近鉄南大阪線",   # 南大阪線 を先に取らないと大阪線に化ける
            "名古屋線": "近鉄名古屋線",
            "橿原線": "近鉄橿原線", "けいはんな線": "近鉄けいはんな線",
            "生駒線": "近鉄生駒線", "天理線": "近鉄天理線", "吉野線": "近鉄吉野線",
        },
    },
]

# ---- 私鉄 3 層分流（T-010.2）----------------------------------------------
# a 層 = 京都関連線（§4.2 に重みあり）→ 推送 + 専用駅ヒント
# b 層 = 非京都線（神戸線・名古屋線・大津線…）→ 留档のみ、推送しない
# c 層 = 線名不明 → 会社レベルで推送するが【駅ヒントを出さない】
# 正式線名は tools/decision/params.py の LINE_ZONE_W キーと逐字一致（命名対齐铁律）。
PRIVATE_LINE_HINTS = {
    "阪急京都線": "桂・烏丸・京都河原町・西院",
    "阪急嵐山線": "嵐山・桂",
    "京阪本線": "祇園四条・三条・七条・伏見稲荷",
    "近鉄京都線": "近鉄京都駅・丹波橋・東寺",
}
UNKNOWN_LINE_HINT = "なし(路線不明・公式サイト要確認)"

# 京都市営地下鉄（v1.2, T-010）: 交通局首页緊急情報块
SUBWAY_URL = "https://www.city.kyoto.lg.jp/kotsu/"
SUBWAY_BLOCK_START = "▼▼緊急情報▼▼"   # HTML 注释锚点（2026-08-08 实测存在）
SUBWAY_BLOCK_END = "▲▲緊急情報▲▲"
SUBWAY_ALERT_WORDS = ["運転見合わせ", "運転を見合わせ", "運行見合わせ", "運休",
                      "遅れが発生", "遅延が発生"]
# line 命名与设计稿 §4.2 映射表键对齐（烏丸線/東西線）
SUBWAY_LINE_HINTS = {
    # 烏丸線は §4.2 で ⑧(今出川)・⑫(北大路/北山) も受益区 → 北側を取りこぼさない
    "烏丸線": "京都駅・四条・烏丸御池・今出川・北大路",
    "東西線": "山科・三条京阪・烏丸御池",
}

FAILURE_ALERT_THRESHOLD = 3  # 连续失败 N 次才报一次数据源异常

# 誤回復ガード（v1.2.3 / T-010.3）: 「平常」表記でも異常語が残っているうちは
# ✅ を出さない。宁漏勿误の完全形は『漏报可接受・误报不可接受』であり、
# ✅ も「報せ」——需要の波が終わったと断言して外れるのは誤報（PM 裁定 2026-08-09）。
# 広告文言が常駐するページで永久滞留しないよう、保留は下記時間で打ち切る
# （decision 側 FORCE_DECAY_AFTER_MIN=180 分と同期）。
RECOVERY_GUARD_MAX_H = 3

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


def record_event(event_type: str, key: str, inc: dict, now: datetime) -> None:
    """把状态变化事件追加写入 alerts.jsonl（无论是否处于静默时段都留档）。
    这是 Phase 3「电车异常×收入」分析的原材料。"""
    line = json.dumps({
        "ts": now.isoformat(),
        "type": event_type,          # new / change / recover
        "key": key,
        "company": inc.get("company", ""),
        "line": inc.get("line", ""),
        "status": inc.get("status", ""),
        "transfer": bool(inc.get("transfer")),
        # T-020(硬冻结解冻一小口, 司机8/21裁定): 事故原文留档——判定逻辑零改动,
        # 纯数据流字段。.get 是铁律(state.json 外部持久化可能缺键)。显示级清洗+120截断。
        "detail": re.sub(r"\s+", " ", str(inc.get("cause", ""))).strip()[:120],
    }, ensure_ascii=False)
    with open(ALERTS_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_status(now: datetime, active_count: int) -> None:
    """每轮运行结束后覆盖写心跳（T-010.1）。

    下游（tools/decision/alerts.py）用 last_run 判新鲜度：单一长时异常期间
    alerts.jsonl 不会有新事件，只按留档末条判会把仍在持续的警报错杀成 A≡0。
    """
    STATUS_FILE.write_text(
        json.dumps({"last_run": now.isoformat(), "active": int(active_count)},
                   ensure_ascii=False, sort_keys=True) + "\n",
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
# 特例: 返回 None = "获取成功但无法判定"（保留上次 active, 不计失败）——目前仅地下鉄源使用


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
            "hint": JR_LINE_HINTS.get(key, "京都駅"),
        }
    return incidents


def _to_sentences(html: str) -> str:
    """块级标签边界→句点，保住"每条通知一句"的分界（strip_tags 会压平空白）"""
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)     # 注释里的旧通知は現状ではない
    html = re.sub(r"(?i)</(p|div|li|td|h\d)>|<br[^>]*>", "。", html)
    return strip_tags(html)


def _strip_boilerplate(text: str, phrases) -> str:
    """固定説明文を落とす。タグ→「。」変換で文中に句点が割り込んだ版も一緒に落とす
    （落とし損ねると説明文中の「運転見合わせ」で誤報するため）。"""
    for p in phrases:
        text = text.replace(p, " ")
        text = re.sub("。?".join(map(re.escape, p)), " ", text)
    return text


def _parse_iso(value):
    """→ tz-aware datetime か None（壊れた値・非文字列・**無時区**は None）。
    naive を通すと JST-aware な now との減算で TypeError→データ源失敗扱いになる"""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _worst(words) -> str:
    """状態語の中から一番重いものを返す（停运級を優先）。
    軽い方を採ると「遅れ…、別区間で運転見合わせ」で priority 4 に落ち、
    停运を軽く報せてしまう。"""
    words = list(words)
    return next((w for w in words if any(s in w for s in SUSPEND_WORDS)),
                words[0] if words else "")


def _match_lines(text: str, lines: dict) -> list:
    """長い表記から順に取り、取れた分はテキストから削る。
    部分文字列の共食いを防ぐための必須処理——素直に `t in text` で回すと
    「石山坂本線」が『本線』(=京阪本線, 京都の線!) に、「南大阪線」が『大阪線』に
    化ける。b 層の事故が a 層に化けて虚假指令が復活するので、ここは削り取り式。"""
    found, rest = [], text
    for token in sorted(lines, key=len, reverse=True):
        if token in rest:
            found.append(lines[token])
            rest = rest.replace(token, " ")
    return found


def _attribute(sentences: str, watch: dict) -> tuple:
    """→ ({正式線名: 状態語}, [(帰属できなかった文, その状態語)])
    地下鉄 provider で検証済みの「文→小句」帰属を移植（T-010.2）。"""
    lines = watch["lines"]
    assigned, unresolved = {}, []
    for sentence in sentences.split("。"):
        swords = [w for w in watch["alert_words"] if w in sentence]
        if not swords:
            continue
        local, leftover = {}, []
        normal_lines = set()
        for clause in re.split(r"[、,，]", sentence):
            cw = [w for w in watch["alert_words"] if w in clause]
            cl = _match_lines(clause, lines)
            if any(nm in clause for nm in watch["normal_markers"]):
                normal_lines.update(cl)   # 平常と明言された線には状態を配らない
            if cw and cl:
                for ln in cl:
                    local[ln] = cw[0]
            elif cw:
                leftover.append(cw[0])
        # 【保守方針・司机裁定 2026-08-09】状態語は「同じ小句で名指しされた線」にだけ配る。
        # 文全体からの推測配布は廃止——「神戸線で運転を見合わせ、京都線で振替輸送を実施」で
        # 京都線まで見合わせ扱いになり、**走っている線を止まったと報せる**という
        # 元の bug より悪い挙動を生んだため。推測しない代わりに取りこぼしは必ず
        # c 層（路線不明・駅ヒント無し）として上げる＝宁漏勿误。
        rest = [ln for ln in _match_lines(sentence, lines)
                if ln not in local and ln not in normal_lines]
        if leftover or rest:
            # 配れなかった状態語、または状態不明のまま名指しされた線がある。
            # 同じ文に複数の状態語が残ることがある（読点区切り）ので一番重いものを採る
            unresolved.append((sentence.strip(),
                               _worst(leftover) if leftover else _worst(swords)))
        assigned.update(local)
    return assigned, unresolved


def check_page(watch: dict, previous: dict = None) -> dict:
    """私鉄ページ（T-010.2 で 3 層分流に更新）。
    判定ゲート（平常/異常語）は従来どおり平坦テキストで＝既存挙動ゼロ回帰。
    線名の帰属だけ文境界つきテキストで行う。
    previous: 前回の active。帰属が全滅した回に「線別の異常が回復した」と
    誤認しないための持ち越しに使う（平常表記に戻れば上のゲートで正しく回復）。"""
    html = fetch(watch["url"])
    flat = _strip_boilerplate(strip_tags(html), watch["boilerplate"])

    if any(marker in flat for marker in watch["normal_markers"]):
        # 誤回復ガード（T-010.3）: この会社に active があり、かつ「平常」表記と
        # 異常語が同居しているページでは ✅ を出さない——回復の断言も誤報になりうる。
        # 異常語が完全に消えたら従来どおり素直に回復（漏报路径は無傷）。
        active_keys = [k for k in (previous or {}) if k.startswith(watch["id"] + ":")]
        if active_keys and any(w in flat for w in watch["alert_words"]):
            now = datetime.now(JST)
            keep = {}
            for k in active_keys:
                inc = dict(previous[k])
                since = _parse_iso(inc.get("guard_since"))
                if since is None:
                    inc["guard_since"] = now.isoformat()   # 保留開始を刻む
                    keep[k] = inc
                elif now - since > timedelta(hours=RECOVERY_GUARD_MAX_H):
                    # 打ち切り: 広告文言などが常駐するページで永久滞留しないための上限。
                    # 回復を放行するが、断言はしない——文案に要確認を付ける（run() が読む）
                    previous[k]["recovery_note"] = "(自動判定・要確認)"
                    log(f'{watch["company"]}: 誤回復ガード {RECOVERY_GUARD_MAX_H}h 上限 → '
                        f'{k} の回復を放行（要確認付き）')
                else:
                    keep[k] = inc
            if keep:
                log(f'{watch["company"]}: 平常表記だが異常語が残存 → 回復を保留'
                    f'（誤✅防止, T-010.3）: {flat[:80]}')
            return keep
        return {}

    matched = [w for w in watch["alert_words"] if w in flat]
    if not matched:
        # 既无平常标记也无异常关键词：无法判断 → 只记日志（宁漏勿误）
        log(f'{watch["company"]}: 页面无法判定（unknown），跳过。片段: {flat[:120]}')
        return {}

    assigned, unresolved = _attribute(
        _strip_boilerplate(_to_sentences(html), watch["boilerplate"]), watch)

    incidents = {}
    if not assigned or unresolved:
        # c 層: 帰属できない異常は必ず 1 件立てる（「絶対に真の警報を落とさない」保全）。
        # 帰属済みが別にあっても落とさない——落とすと未知の事故が消える（終審#3）。
        # 状態語は【未帰属の文のもの】を使う。全ページの最初の語を使うと
        # 「神戸線は見合わせ・別区間は遅れ」で c 層まで見合わせに化ける（終審二輪#2）。
        # 複数の未帰属がある回は【一番重い】状態語を採る（文をまたいでも同様）
        word = _worst([w for _, w in unresolved]) if unresolved else matched[0]
        log(f'{watch["company"]}: 帰属不能な異常あり → 駅ヒントなしで通知。'
            f'{" / ".join(s for s, _ in unresolved)[:120]}')
        # T-020: cause=状態語と同じ句(語句錯位防止——標題見合わせ・原文遅れの自己矛盾を出さない)
        cause_sentence = next((s0 for s0, w0 in unresolved if w0 == word), "") if unresolved else ""
        incidents[f'{watch["id"]}:page'] = {
            "company": watch["company"], "line": "", "status": word,
            "cause": cause_sentence.strip()[:120], "transfer": False, "hint": UNKNOWN_LINE_HINT,
        }

    if not assigned and previous:
        # 帰属が全滅した回: 前回まで線別に立っていた異常を「回復」と誤認しない
        # （表現が曖昧になっただけで、電車が動き出したわけではない——終審二輪#3）
        for k, v in previous.items():
            if k.startswith(watch["id"] + ":") and not k.endswith(":page"):
                incidents[k] = v
                log(f"  ↑ 帰属不能のため {k} を持ち越し（回復とはみなさない）")

    for line, word in assigned.items():
        kyoto = line in PRIVATE_LINE_HINTS
        incidents[f'{watch["id"]}:{line}'] = {
            "company": watch["company"], "line": line, "status": word,
            "cause": "", "transfer": False,
            "hint": PRIVATE_LINE_HINTS.get(line, ""),
            # b 層（非京都線）は事実だけ留档し、司机のポケットには入れない
            "notify": kyoto,
        }
    return incidents


def check_kyoto_subway() -> "Optional[dict]":
    """京都市営地下鉄（v1.2）: 只扫交通局首页緊急情報锚点之间的文本。

    与私铁全页扫描的差异：平常时块为空=正常（无 normal_markers 可依赖）；
    锚点缺失视为页面改版，抛异常纳入既有连续失败自报警。"""
    html = fetch(SUBWAY_URL)
    start = html.find(SUBWAY_BLOCK_START)
    end = html.find(SUBWAY_BLOCK_END, start + 1)
    if start == -1 or end == -1:
        raise ValueError("緊急情報锚点缺失（交通局首页可能改版）")
    raw = html[start + len(SUBWAY_BLOCK_START):end]
    # ① 块内完整注释整体移除——历史通知常被注释保留，不能当现行状态
    raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
    # ② 块级标签边界换成句号，保住"每条通知一句"的分界（strip_tags 会压平空白）
    raw = re.sub(r"(?i)</(p|div|li|td|h\d)>|<br[^>]*>", "。", raw)
    # ③ 锚点切片两端残留的注释碎片清掉，再剥标签
    block = strip_tags(raw.replace("-->", " ").replace("<!--", " ")).strip()
    if not block or block == "。":
        return {}  # 平常时锚点之间为空（2026-08-08 实测）

    # 三态归属（终审二轮#1）：
    #   确认正常(空块/无异常词) → {}   有事件 → incidents   无法判定 → None
    # None 时主循环保留该源上次 active、不发恢复也不计失败——防止
    # 「東西線見合わせ」在下一轮页面措辞含糊时被误报为 ✅ 恢复。
    incidents = {}
    unattributed = []
    for sentence in block.split("。"):
        if not any(w in sentence for w in SUBWAY_ALERT_WORDS):
            continue
        # 句内再按読点分小句归属，防同句两线不同状态串线（终审二轮#2）：
        # 「烏丸線は遅延が発生、東西線は運転を見合わせています」各归各
        assigned = {}
        leftover = []
        clause_ambiguous = False
        normal_lines = set()
        for clause in re.split(r"[、,，]", sentence):
            cw = [w for w in SUBWAY_ALERT_WORDS if w in clause]
            cl = [ln for ln in SUBWAY_LINE_HINTS if ln in clause]
            if "平常" in clause:
                normal_lines.update(cl)  # 明说平常运转的线路，不接受 fallback 补状态
            if cw and cl:
                for ln in cl:
                    assigned[ln] = cw[0]
            elif cw:
                if "地下鉄" in clause:
                    clause_ambiguous = True   # 「地下鉄で…」含糊 → None 保护
                elif "バス" in clause:
                    pass                      # 明确是巴士的事，丢弃（终审三轮）
                else:
                    leftover.extend(cw)
        if leftover:
            # 状态词单独成小句（「烏丸線・東西線は大雨のため、運転を見合わせています」）:
            # 只补给同句内点名、未拿到状态、且没被明说平常的线路
            rest = [ln for ln in SUBWAY_LINE_HINTS
                    if ln in sentence and ln not in assigned and ln not in normal_lines]
            for ln in rest:
                assigned[ln] = leftover[0]
            if not rest and not assigned and not clause_ambiguous:
                unattributed.append(sentence.strip())
        if clause_ambiguous:
            unattributed.append(sentence.strip())
        for ln, word in assigned.items():
            incidents[f"kyotosubway:{ln}"] = {
                "company": "京都市交通局",
                "line": ln,
                "status": word,
                # T-020: 命中句原文を留档(構造点は句ループ内=同源同句で自洽。三態判定零接触)
                "cause": sentence.strip()[:120],
                "transfer": False,
                "hint": SUBWAY_LINE_HINTS[ln],
            }
    if unattributed:
        # 有异常词但没点名烏丸線/東西線（市バス或表述含糊）→ 只记日志不报警（工单铁律），
        # 并整体判"无法判定"保留前态——含糊措辞不能既不报新又误报恢复
        log(f"京都市交通局: 緊急情報含异常词但无法归属到地下鉄线路，不报: {' / '.join(unattributed)[:120]}")
        return None
    return incidents


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
        (w["id"], (lambda w=w: check_page(w, previous))) for w in PAGE_WATCHES
    ] + [("kyotosubway", check_kyoto_subway)]

    for pid, func in providers:
        try:
            result = func()
            state["failures"][pid] = 0
            state["failure_alerted"][pid] = False
            if result is None:
                # 无法判定（页面措辞含糊）: 保留该源上次 active，不发恢复、不计失败
                for key, inc in previous.items():
                    if key.startswith(pid + ":"):
                        current[key] = inc
            else:
                current.update(result)
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
            log(f'新异常: {key} {inc["status"]}'
                + ("" if inc.get("notify", True) else "（非京都線: 留档のみ）"))
            record_event("new", key, inc, now)
            if not quiet and inc.get("notify", True):   # b 層は推送しない（T-010.2）
                title, message, priority = build_alert_message(inc)
                notify(title, message, priority, with_email=True)
        elif (prev.get("status"), prev.get("transfer"), prev.get("line")) != \
                (inc["status"], inc["transfer"], inc.get("line")):
            # line も比較対象（v1.2.1 の line=運行情報 → c 層 line="" の移行を留档に残す）
            # 状态升级/变化（例: 遅延→運転見合わせ、振替輸送开始）
            log(f'状态变化: {key} {prev.get("status")} → {inc["status"]}')
            record_event("change", key, inc, now)
            if not quiet and inc.get("notify", True):
                title, message, priority = build_alert_message(inc)
                title = "🔄 " + title.split(" ", 1)[1]  # 用🔄替换原图标
                notify(title, message, priority, with_email=True)

    # 恢复
    for key, prev in previous.items():
        if key not in current:
            log(f"恢复: {key}")
            record_event("recover", key, prev, now)
            # 推送しなかった異常の「✅回復」も出さない（出したら寝耳に水）。
            # line=="運行情報" は v1.2.1 以前の私鉄レコード＝キー体系の移行で消えるだけ。
            # 実際には復旧していないので ✅ を出さない（留档には正直に残す）。
            legacy = prev.get("line") == "運行情報"
            if legacy:
                log(f"  ↑ v1.2.1 以前のキー体系からの移行のため ✅ 通知は出さない")
            if not quiet and prev.get("notify", True) and not legacy:
                note = prev.get("recovery_note", "")   # 誤回復ガード打ち切り時の要確認（T-010.3）
                notify(
                    f'✅ {prev.get("company","")} {prev.get("line","")} 恢复正常{note}',
                    f'此前: {prev.get("status","")}\n{now.strftime("%H:%M")} JST',
                    3, with_email=False,
                )

    state["active"] = current
    state["checked_at"] = now.isoformat()
    save_state(state)
    write_status(now, len(current))   # v1.2.1 心跳（T-010.1）
    log(f"完成: 当前异常 {len(current)} 件" + ("（深夜静默中）" if quiet else ""))
    return 0


if __name__ == "__main__":
    sys.exit(run())
