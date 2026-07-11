#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域雷达 · Notion 信箱取件配音
轮询「领域雷达信箱」页面 → 发现新的 RADAR-* 子页面 → 读取口播稿 →
MiniMax 配音 → 存入 episodes/ 并更新 feed.xml → 记入 notion_processed.json。
复用 radar.py 里的配音与 RSS 逻辑，与周报共用同一份 profile.json 和同一个播客源。
环境变量: NOTION_TOKEN, NOTION_INBOX_URL, MINIMAX_API_KEY, MINIMAX_GROUP_ID(可选), SITE_BASE_URL
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

from radar import env, load_profile, synthesize, load_manifest, save_manifest, build_feed, cleanup_episodes

ROOT = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PROCESSED_PATH = os.path.join(ROOT, "notion_processed.json")


def notion_get(token, path, params=None):
    r = requests.get(f"{NOTION}{path}", params=params or {}, timeout=60,
                     headers={"Authorization": f"Bearer {token}",
                              "Notion-Version": NOTION_VERSION})
    if r.status_code != 200:
        raise RuntimeError(f"Notion API {r.status_code}: {r.text[:300]}")
    return r.json()


def list_children(token, block_id):
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = notion_get(token, f"/blocks/{block_id}/children", params)
        out += data.get("results", [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def block_plain_text(block):
    content = block.get(block.get("type"), {}) or {}
    return "".join(rt.get("plain_text", "") for rt in content.get("rich_text", []))


def page_full_text(token, page_id):
    lines = [block_plain_text(b) for b in list_children(token, page_id)]
    return "\n".join(l for l in lines if l is not None)


def extract_page_id(url_or_id):
    matches = re.findall(r"[0-9a-f]{32}", url_or_id.replace("-", "").lower())
    if not matches:
        raise RuntimeError("无法从 NOTION_INBOX_URL 中解析出页面 ID，请粘贴完整的页面链接")
    return matches[-1]


def parse_payload(text):
    """解析原型投递的固定格式：【标题】【简介】【模式】【口播稿】"""
    def grab(tag):
        m = re.search(rf"【{tag}】\s*(.*)", text)
        return m.group(1).strip() if m else ""
    title = grab("标题")
    summary = grab("简介")
    mode = grab("模式") or ("duo" if "主持A" in text else "solo")
    m = re.search(r"【口播稿】\s*\n(.*)", text, re.S)
    script = m.group(1).strip() if m else ""
    return title, summary, mode, script


def load_processed():
    if os.path.exists(PROCESSED_PATH):
        with open(PROCESSED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_processed(ids):
    with open(PROCESSED_PATH, "w", encoding="utf-8") as f:
        json.dump(ids[-500:], f, ensure_ascii=False, indent=2)


def main():
    profile = load_profile()
    token = env("NOTION_TOKEN")
    inbox_id = extract_page_id(env("NOTION_INBOX_URL"))
    minimax_key = env("MINIMAX_API_KEY")
    group_id = env("MINIMAX_GROUP_ID", required=False)
    base_url = env("SITE_BASE_URL").rstrip("/")

    processed = load_processed()
    children = list_children(token, inbox_id)
    todo = []
    for b in children:
        if b.get("type") != "child_page":
            continue
        title = (b.get("child_page") or {}).get("title", "")
        if title.startswith("RADAR-") and b["id"] not in processed:
            todo.append((b["id"], title))

    if not todo:
        print("信箱没有新投递，本次收工。")
        return

    manifest = load_manifest()
    for page_id, page_title in todo:
        print(f"[取件] {page_title}")
        text = page_full_text(token, page_id)
        ep_title, summary, mode, script = parse_payload(text)
        if not script or len(script) < 50:
            print(f"[跳过] {page_title} 内容不完整（没找到口播稿）", file=sys.stderr)
            processed.append(page_id)
            continue

        m = re.match(r"RADAR-(\d{4}-\d{2}-\d{2})-(daily|weekly)", page_title)
        stamp = m.group(1) if m else datetime.now(CST).strftime("%Y-%m-%d")
        kind = m.group(2) if m else "daily"

        print(f"[配音] 模式 {mode}，{len(script)} 字…")
        audio = synthesize(profile, minimax_key, group_id, script, mode)
        os.makedirs(os.path.join(ROOT, "episodes"), exist_ok=True)
        audio_file = f"episodes/{stamp}-{kind}.mp3"
        with open(os.path.join(ROOT, audio_file), "wb") as f:
            f.write(audio)
        print(f"       已存 {audio_file}（{len(audio) // 1024} KB）")

        manifest = [e for e in manifest if e["file"] != audio_file]
        manifest.insert(0, {
            "title": ep_title or f"AI影视 · {'今日速览' if kind == 'daily' else '本周深度'} · {stamp}",
            "description": summary or "AI影视赛道最新进展",
            "file": audio_file,
            "size": len(audio),
            "date": datetime.now(CST).isoformat(),
            "kind": kind,
        })
        processed.append(page_id)

    manifest = manifest[:60]
    save_manifest(manifest)
    build_feed(profile, base_url, manifest)
    cleanup_episodes(manifest)
    save_processed(processed)
    print("完成 ✅ 播客源已更新: " + base_url + "/feed.xml")


if __name__ == "__main__":
    main()
