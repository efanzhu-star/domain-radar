#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域雷达 · 每日/每周自动流水线
Claude(联网检索→研报→口播稿) → MiniMax TTS(配音) → episodes/*.mp3 + feed.xml
用法: python radar.py daily | python radar.py weekly
环境变量: ANTHROPIC_API_KEY, MINIMAX_API_KEY, MINIMAX_GROUP_ID(可选), SITE_BASE_URL
"""
import base64
import binascii
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))  # 北京时间

# ---------------- 配置与环境 ----------------

def load_profile():
    with open(os.path.join(ROOT, "profile.json"), "r", encoding="utf-8") as f:
        return json.load(f)

def env(name, required=True, default=""):
    v = os.environ.get(name, default).strip()
    if required and not v:
        print(f"[错误] 缺少环境变量 {name}", file=sys.stderr)
        sys.exit(1)
    return v

# ---------------- Claude ----------------

def call_claude(api_key, system, user, web=False, max_tokens=8000, retries=2):
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if web:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers=headers, json=body, timeout=900)
            if r.status_code != 200:
                raise RuntimeError(f"Claude API {r.status_code}: {r.text[:500]}")
            data = r.json()
            text = "\n".join(b.get("text", "") for b in data.get("content", [])
                             if b.get("type") == "text").strip()
            if not text:
                raise RuntimeError("Claude 返回为空")
            return text
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[重试] Claude 调用失败({attempt + 1}): {e}", file=sys.stderr)
            time.sleep(15)
    raise RuntimeError(f"Claude 调用最终失败: {last_err}")

def brief_prompts(profile, kind, today_str):
    window = "最近 24 小时内" if kind == "daily" else "最近 7 天内"
    depth = ("偏「发生了什么 + 一句话为什么重要」，简洁但有判断。" if kind == "daily"
             else "偏「本周脉络 + 趋势研判」，把散点连成线，指出方向。")
    trend = "" if kind == "daily" else "\n## 趋势研判\n（2–4 点。把本周的点连成趋势，指出方向与值得盯的信号。）\n"
    system = f"""你是一名专注「AI 影视 / AI 视频生成」赛道的行业研究分析师，为一位需要每天保持敏锐的从业者供稿。

工作方式（严格执行）：
1. 用联网搜索工具，中英文各自用对应术语检索，覆盖多个子维度与你被给到的优先信息源。目标是{window}真正发生的实质进展。
2. 先广后精：先尽量多地找到候选，再自己判断——这是不是真进展？新不新？重要性几级？把讲同一件事的合并成一条，保留最好的来源。
3. 只保留真进展（新模型/版本、能力突破、产品上线、融资并购、行业采用、政策版权、开源发布、榜单变化）。滤掉营销软文、教程、旧闻复述、纯观点。
4. 全程用中文成稿。{depth}

输出格式（用 Markdown）：
## 一句话综述
（一句话点明{window}这个赛道最值得注意的事）

## 头条进展
（3–6 条。每条：**加粗标题** → 一句发生了什么 → 一句为什么重要/你的点评 → 末尾用 [来源](链接) 标注。真实链接，来自你的搜索结果。）

## 也发生了
（次要进展，每条一行短句，可多条）
{trend}
纪律：不编造。找不到可靠信息就说这一项没有确切进展，不要凑数。链接必须来自搜索结果，不要臆造。"""
    user = f"""今天是 {today_str}。请生成一期「{'今日速览' if kind == 'daily' else '本周深度'}」。

监测画像：
· 核心实体：{'、'.join(profile['entities'])}
· 关注维度：{'；'.join(profile['dimensions'])}
· 英文检索词：{', '.join(profile['terms_en'])}
· 中文检索词：{'、'.join(profile['terms_zh'])}
· 优先信息源：{'；'.join(profile['sources'])}
· 排除噪声：{'、'.join(profile['negatives'])}

现在开始联网检索并按格式成稿。"""
    return system, user

def script_prompt(mode):
    if mode == "duo":
        return """把下面这份研报改写成两位主持人的播客对话口播稿，用于通勤收听。
· 两位主持：主持A（引导、提问、串场）、主持B（分析、补充、点评）。
· 每一句都以「主持A：」或「主持B：」开头，交替自然，有你来我往的讨论感。
· 口语化、顺耳，避免书面长句和生僻缩写；专有名词可保留英文。
· 不要读出链接、括号里的来源标注。
· 开头一句欢迎语，结尾一句收束。只输出对话正文，不要额外说明。"""
    return """把下面这份研报改写成一位主持人的播客口播稿，用于通勤收听。
· 一位主播娓娓道来，像在跟朋友讲今天这个赛道发生了什么、为什么值得关注。
· 口语化、顺耳，避免书面长句和生僻缩写；专有名词可保留英文。
· 不要读出链接、括号里的来源标注。
· 开头一句欢迎语，结尾一句收束。只输出正文，不要额外说明。"""

# ---------------- MiniMax TTS ----------------

def tts_once(profile, api_key, group_id, text, voice_id):
    url = profile["tts_endpoint"]
    if group_id:
        url = f"{url}?GroupId={group_id}"
    body = {
        "model": profile["tts_model"],
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice_id, "speed": profile.get("speech_speed", 1.0),
                          "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
    }
    r = requests.post(url, headers={"Authorization": f"Bearer {api_key}",
                                    "Content-Type": "application/json"},
                      json=body, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"MiniMax API {r.status_code}: {r.text[:500]}")
    data = r.json()
    base = data.get("base_resp", {})
    if base.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax 返回错误: {base.get('status_code')} {base.get('status_msg')}")
    audio = (data.get("data") or {}).get("audio") or data.get("audio_file")
    if not audio:
        raise RuntimeError(f"MiniMax 响应里没找到音频字段: {str(data)[:300]}")
    try:  # 官方文档为 hex；个别通道为 base64，两种都兼容
        return bytes.fromhex(audio)
    except (ValueError, binascii.Error):
        return base64.b64decode(audio)

def chunk_text(text, limit=2000):
    parts, buf = [], ""
    for seg in re.split(r"(?<=[。！？!?；;\n])", text):
        if len(buf) + len(seg) > limit and buf:
            parts.append(buf)
            buf = seg
        else:
            buf += seg
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]

def synthesize(profile, api_key, group_id, script, mode):
    jobs = []  # (text, voice_id)
    if mode == "duo":
        cur_voice, cur_buf = None, ""
        for line in script.splitlines():
            line = line.strip()
            if not line:
                continue
            voice = profile["voice_b"] if re.match(r"^主持B", line) else profile["voice_a"]
            clean = re.sub(r"^主持[AB][：:]\s*", "", line)
            if voice == cur_voice:
                cur_buf += "\n" + clean
            else:
                if cur_buf:
                    jobs.append((cur_buf, cur_voice))
                cur_voice, cur_buf = voice, clean
        if cur_buf:
            jobs.append((cur_buf, cur_voice))
    else:
        jobs = [(script, profile["voice_solo"])]

    audio = b""
    for text, voice in jobs:
        for piece in chunk_text(text):
            print(f"[TTS] {voice} ← {len(piece)} 字")
            audio += tts_once(profile, api_key, group_id, piece, voice)
            time.sleep(1.2)  # 尊重速率限制
    if len(audio) < 10000:
        raise RuntimeError("合成的音频过小，疑似失败")
    return audio

# ---------------- 存档与 RSS ----------------

def load_manifest():
    path = os.path.join(ROOT, "episodes.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_manifest(items):
    with open(os.path.join(ROOT, "episodes.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

def first_summary(brief_md):
    lines = [l.strip() for l in brief_md.splitlines() if l.strip() and not l.startswith("#")]
    return lines[0][:200] if lines else "今日 AI 影视赛道速览"

def cleanup_episodes(manifest):
    """仓库只保留清单内（最近 60 期）的音频，更早的自动移除，防止仓库无限膨胀。
    归档提示：每月把整仓库 Download ZIP 存进 NAS，即可在清理前留档。"""
    keep = {e["file"] for e in manifest}
    epdir = os.path.join(ROOT, "episodes")
    if not os.path.isdir(epdir):
        return
    for fn in os.listdir(epdir):
        rel = f"episodes/{fn}"
        if fn.endswith(".mp3") and rel not in keep:
            os.remove(os.path.join(epdir, fn))
            print(f"[清理] 移除过期音频 {rel}（Notion/NAS 里仍有档案）")


def build_feed(profile, base_url, episodes):
    now = format_datetime(datetime.now(timezone.utc))
    items = []
    for ep in episodes:
        pub = format_datetime(datetime.fromisoformat(ep["date"]))
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['description'])}</description>
      <enclosure url="{escape(base_url + '/' + ep['file'])}" length="{ep['size']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(ep['file'])}</guid>
      <pubDate>{pub}</pubDate>
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(profile['channel_name'])}</title>
    <link>{escape(base_url)}</link>
    <description>{escape(profile['channel_description'])}</description>
    <language>{profile.get('language', 'zh-cn')}</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>领域雷达</itunes:author>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""
    with open(os.path.join(ROOT, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)

# ---------------- 主流程 ----------------

def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if kind not in ("daily", "weekly"):
        print("用法: python radar.py daily|weekly", file=sys.stderr)
        sys.exit(1)

    profile = load_profile()
    anthropic_key = env("ANTHROPIC_API_KEY")
    minimax_key = env("MINIMAX_API_KEY")
    group_id = env("MINIMAX_GROUP_ID", required=False)
    base_url = env("SITE_BASE_URL").rstrip("/")

    now = datetime.now(CST)
    today_str = now.strftime("%Y年%m月%d日")
    stamp = now.strftime("%Y-%m-%d")
    label = "今日速览" if kind == "daily" else "本周深度"

    print(f"[1/4] 生成研报（{label}）…")
    sys_p, usr_p = brief_prompts(profile, kind, today_str)
    brief = call_claude(anthropic_key, sys_p, usr_p, web=True, max_tokens=8000)

    os.makedirs(os.path.join(ROOT, "briefs"), exist_ok=True)
    brief_file = f"briefs/{stamp}-{kind}.md"
    with open(os.path.join(ROOT, brief_file), "w", encoding="utf-8") as f:
        f.write(brief)
    print(f"      研报已存 {brief_file}（{len(brief)} 字）")

    print("[2/4] 改写口播稿…")
    mode = profile.get("podcast_mode", "duo")
    script = call_claude(anthropic_key, script_prompt(mode), brief, web=False, max_tokens=4000)

    print("[3/4] MiniMax 配音…")
    audio = synthesize(profile, minimax_key, group_id, script, mode)
    os.makedirs(os.path.join(ROOT, "episodes"), exist_ok=True)
    audio_file = f"episodes/{stamp}-{kind}.mp3"
    with open(os.path.join(ROOT, audio_file), "wb") as f:
        f.write(audio)
    print(f"      音频已存 {audio_file}（{len(audio) // 1024} KB）")

    print("[4/4] 更新 feed.xml …")
    manifest = load_manifest()
    manifest = [e for e in manifest if e["file"] != audio_file]
    manifest.insert(0, {
        "title": f"AI影视 · {label} · {now.strftime('%m月%d日')}",
        "description": first_summary(brief),
        "file": audio_file,
        "size": len(audio),
        "date": now.isoformat(),
        "kind": kind,
    })
    manifest = manifest[:60]  # 最多保留 60 期
    save_manifest(manifest)
    build_feed(profile, base_url, manifest)
    cleanup_episodes(manifest)
    print("完成 ✅  订阅地址: " + base_url + "/feed.xml")

if __name__ == "__main__":
    main()
