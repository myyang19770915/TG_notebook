"""Artifact catalogue: generation kinds, download formats, style/param parsing."""
from __future__ import annotations

import re

# key -> (nblm generate kind, 中文名, download kind, 副檔名, download --format, 預估分鐘)
CATALOG: dict[str, tuple[str, str, str, str, str | None, int]] = {
    "audio":       ("audio",       "語音摘要", "audio",       ".mp3",  None,       15),
    "infographic": ("infographic", "資訊圖表", "infographic", ".png",  None,       10),
    "mindmap":     ("mind-map",    "心智圖",   "mind-map",    ".json", None,        1),
    "slides":      ("slide-deck",  "簡報",     "slide-deck",  ".pdf",  None,       12),
    "report":      ("report",      "報告",     "report",      ".md",   None,       10),
    "quiz":        ("quiz",        "測驗",     "quiz",        ".md",   "markdown", 10),
    "flashcards":  ("flashcards",  "學習卡",   "flashcards",  ".md",   "markdown", 10),
    "datatable":   ("data-table",  "資料表",   "data-table",  ".csv",  None,       10),
    "video":       ("video",       "影片",     "video",       ".mp4",  None,       30),
}

PRIMARY = ["audio", "infographic", "mindmap", "slides"]
SECONDARY = ["report", "quiz", "flashcards", "datatable", "video"]

EMOJI = {
    "audio": "🎧", "infographic": "📊", "mindmap": "🧠", "slides": "📽",
    "report": "📄", "quiz": "📝", "flashcards": "🃏", "datatable": "📋", "video": "🎬",
}

# ------------------------------------------------------------------ language

LANGUAGES = {
    "繁體中文": "zh_Hant", "繁中": "zh_Hant", "正體中文": "zh_Hant",
    "简体中文": "zh_Hans", "簡體中文": "zh_Hans", "简中": "zh_Hans",
    "英文": "en", "english": "en", "英語": "en",
    "日文": "ja", "日語": "ja", "japanese": "ja",
    "韓文": "ko", "韓語": "ko",
}

# ------------------------------------------------------- per-kind style tables

INFOGRAPHIC_STYLES = {
    "手繪": "sketch-note", "手绘": "sketch-note", "速記": "sketch-note",
    "專業": "professional", "商務": "professional",
    "便當": "bento-grid", "bento": "bento-grid", "網格": "bento-grid",
    "雜誌": "editorial", "編輯": "editorial",
    "教學": "instructional",
    "積木": "bricks", "黏土": "clay",
    "動漫": "anime", "可愛": "kawaii", "科學": "scientific",
    "自動": "auto",
}

VIDEO_STYLES = {
    "經典": "classic", "白板": "whiteboard", "可愛": "kawaii", "動漫": "anime",
    "水彩": "watercolor", "復古": "retro-print", "傳統": "heritage",
    "剪紙": "paper-craft", "自動": "auto",
}

ORIENTATIONS = {"直式": "portrait", "橫式": "landscape", "方形": "square",
                "正方": "square", "直": "portrait", "橫": "landscape"}

AUDIO_FORMATS = {"深入": "deep-dive", "深度": "deep-dive", "簡短": "brief",
                 "評論": "critique", "辯論": "debate"}

REPORT_FORMATS = {"簡報文件": "briefing-doc", "簡報": "briefing-doc",
                  "學習指南": "study-guide", "指南": "study-guide",
                  "部落格": "blog-post", "blog": "blog-post"}

DIFFICULTY = {"簡單": "easy", "容易": "easy", "中等": "medium",
              "困難": "hard", "難": "hard"}


def parse_params(kind: str, text: str) -> tuple[str, dict]:
    """Split free text into (instructions, nblm option dict).

    Style tokens are conventionally given after a `/` or as slash-separated
    trailing words, e.g. "QA 繁體中文/手繪".
    """
    opts: dict[str, str] = {}
    tokens = [t for t in re.split(r"[\s/、,，]+", text.strip()) if t]
    remaining: list[str] = []

    style_table = {}
    if kind == "infographic":
        style_table = INFOGRAPHIC_STYLES
    elif kind == "video":
        style_table = VIDEO_STYLES

    for tok in tokens:
        low = tok.lower()
        if low in LANGUAGES or tok in LANGUAGES:
            opts["language"] = LANGUAGES.get(tok) or LANGUAGES[low]
        elif tok in style_table:
            opts["style"] = style_table[tok]
        elif kind == "infographic" and tok in ORIENTATIONS:
            opts["orientation"] = ORIENTATIONS[tok]
        elif kind == "audio" and tok in AUDIO_FORMATS:
            opts["format"] = AUDIO_FORMATS[tok]
        elif kind == "report" and tok in REPORT_FORMATS:
            opts["format"] = REPORT_FORMATS[tok]
        elif kind in ("quiz", "flashcards") and tok in DIFFICULTY:
            opts["difficulty"] = DIFFICULTY[tok]
        else:
            remaining.append(tok)

    # mind-map takes no options at all
    if kind == "mindmap":
        opts = {}
    return " ".join(remaining), opts
