#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域雷达 · DeepSeek 对比版
DeepSeek(展开查询) → Tavily(免费检索) → DeepSeek(综合成研报) → briefs-deepseek/*.md
与 Claude 版共用 profile.json 与输出格式，便于逐日对比。不做 TTS（纯文本对比）。
用法: python radar_deepseek.py daily | python radar_deepseek.py weekly
环境变量: DEEPSEEK_API_KEY, TAVILY_API_KEY
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"  # 旧别名 deepseek-chat 将于 2026-07-24 停用
TAVILY_URL = "https://api.tavily.com/search"


def load_profile():
    with open(os.path.join(ROOT, "profile.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[错误] 缺少环境变量 {name}", file=sys.stderr)
        sys.exit(1)
    return v


def call_deepseek(api_key, system, user, max_tokens=6000, retries=2):
    body = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(DEEPSEEK_URL, json=body, timeout=600,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"})
            if r.status_code != 200:
                raise RuntimeError(f"DeepSeek API {r.status_code}: {r.text[:400]}")
            text = r.json()["choices"][0]["message"]["content"].strip()
            if not text:
                raise RuntimeError("DeepSeek 返回为空")
            return text
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[重试] DeepSeek 调用失败({attempt + 1}): {e}", file=sys.stderr)
            time.sleep(10)
    raise RuntimeError(f"DeepSeek 调用最终失败: {last}")


# ---------- 第一步：把画像展开成一批检索查询 ----------

def expand_queries(api_key, profile, kind):
    window = "过去24小时" if kind == "daily" else "过去7天"
    sys_p = ("你是检索策划。根据监测画像，生成用于新闻检索的查询词。"
             "只输出一个 JSON 数组，含 12 个字符串：6 个英文查询、6 个中文查询，"
             "各自覆盖不同实体与维度，短小、适合搜索引擎。不要输出任何其他内容。")
    usr_p = (f"主题：AI 影视 / AI 视频生成，时间窗口：{window}。\n"
             f"核心实体：{'、'.join(profile['entities'][:12])}\n"
             f"关注维度：{'；'.join(profile['dimensions'])}\n"
             f"英文术语：{', '.join(profile['terms_en'][:10])}\n"
             f"中文术语：{'、'.join(profile['terms_zh'][:10])}")
    try:
        raw = call_deepseek(api_key, sys_p, usr_p, max_tokens=800)
        m = re.search(r"\[.*\]", raw, re.S)
        queries = json.loads(m.group(0)) if m else []
        queries = [q for q in queries if isinstance(q, str) and q.strip()][:12]
        if len(queries) >= 6:
            return queries
    except Exception as e:  # noqa: BLE001
        print(f"[提示] 查询展开失败，改用默认查询: {e}", file=sys.stderr)
    # 兜底：从画像直接拼
    return (["AI video generation news", "text-to-video model release",
             "Runway OR Kling OR Veo update", "AI filmmaking studio adoption",
             "AI video startup funding", "open source video model weights"]
            + ["AI视频生成 最新", "可灵 OR 即梦 OR 海螺 更新", "文生视频 发布",
               "AI短剧 行业", "视频大模型 开源", "AI影视 版权"])


# ---------- 第二步：Tavily 检索（免费档每月 1000 次） ----------

def tavily_search(api_key, query, kind):
    body = {
        "query": query,
        "topic": "news",
        "days": 1 if kind == "daily" else 7,
        "max_results": 5,
        "search_depth": "basic",
    }
    r = requests.post(TAVILY_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"})
    if r.status_code != 200:
        print(f"[提示] Tavily 查询失败({r.status_code}): {query}", file=sys.stderr)
        return []
    return r.json().get("results", [])


def collect(api_key, queries, kind):
    seen, items = set(), []
    for q in queries:
        for res in tavily_search(api_key, q, kind):
            url = res.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            items.append({
                "title": (res.get("title") or "")[:150],
                "url": url,
                "snippet": (res.get("content") or "")[:400],
                "date": res.get("published_date") or "",
            })
        time.sleep(0.5)
    return items


# ---------- 第三步：综合成稿（与 Claude 版同一输出格式） ----------

def synth_prompts(profile, kind, today_str, items):
    window = "最近 24 小时内" if kind == "daily" else "最近 7 天内"
    depth = ("偏「发生了什么 + 一句话为什么重要」，简洁但有判断。" if kind == "daily"
             else "偏「本周脉络 + 趋势研判」，把散点连成线，指出方向。")
    trend = "" if kind == "daily" else "\n## 趋势研判\n（2–4 点。把本周的点连成趋势，指出方向与值得盯的信号。）\n"
    system = f"""你是一名专注「AI 影视 / AI 视频生成」赛道的行业研究分析师。下面会给你一批检索到的候选材料。

工作方式（严格执行）：
1. 只使用给定材料中的信息与链接，不得引入材料之外的"事实"或编造链接。
2. 自行判断：哪些是{window}的真进展（新模型/版本、能力突破、产品上线、融资并购、行业采用、政策版权、开源发布、榜单变化）？滤掉营销软文、教程、旧闻、纯观点，以及命中排除词的内容。
3. 讲同一件事的合并为一条，保留最好的来源。
4. 全程用中文成稿。{depth}

输出格式（用 Markdown）：
## 一句话综述
（一句话点明{window}这个赛道最值得注意的事）

## 头条进展
（3–6 条。每条：**加粗标题** → 一句发生了什么 → 一句为什么重要/你的点评 → 末尾用 [来源](链接) 标注，链接必须来自给定材料。）

## 也发生了
（次要进展，每条一行短句，可多条）
{trend}
纪律：材料不足以支撑某个板块时，直说没有确切进展，不要凑数。"""
    corpus = "\n".join(
        f"- [{it['date']}] {it['title']} | {it['url']}\n  摘要：{it['snippet']}"
        for it in items
    )
    user = (f"今天是 {today_str}。排除噪声词：{'、'.join(profile['negatives'])}。\n"
            f"以下是检索到的 {len(items)} 条候选材料：\n\n{corpus}\n\n请按格式成稿。")
    return system, user


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if kind not in ("daily", "weekly"):
        print("用法: python radar_deepseek.py daily|weekly", file=sys.stderr)
        sys.exit(1)

    profile = load_profile()
    ds_key = env("DEEPSEEK_API_KEY")
    tv_key = env("TAVILY_API_KEY")
    now = datetime.now(CST)

    print("[1/3] 展开检索查询…")
    queries = expand_queries(ds_key, profile, kind)
    print("      " + " | ".join(queries))

    print("[2/3] Tavily 检索…")
    items = collect(tv_key, queries, kind)
    print(f"      去重后共 {len(items)} 条候选")
    if len(items) < 3:
        print("[错误] 候选太少，本次放弃成稿（检查 TAVILY_API_KEY 或稍后再试）", file=sys.stderr)
        sys.exit(1)

    print("[3/3] DeepSeek 综合成稿…")
    sys_p, usr_p = synth_prompts(profile, kind, now.strftime("%Y年%m月%d日"), items[:60])
    brief = call_deepseek(ds_key, sys_p, usr_p, max_tokens=6000)

    os.makedirs(os.path.join(ROOT, "briefs-deepseek"), exist_ok=True)
    out = f"briefs-deepseek/{now.strftime('%Y-%m-%d')}-{kind}.md"
    with open(os.path.join(ROOT, out), "w", encoding="utf-8") as f:
        f.write(brief)
    print(f"完成 ✅  已存 {out}（与 briefs/ 里同日的 Claude 版对照阅读）")


if __name__ == "__main__":
    main()
