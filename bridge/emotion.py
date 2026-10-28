"""本地情绪判断：只看回复文字里的关键词，不调模型、不改提示词。

匹配规则：
- 长关键词优先，命中的字符不再参与短关键词匹配（「讨厌啦」不会同时算「讨厌」）；
- 命中次数多的赢，次数相同看谁先出现；
- 一个都没命中就是 calm，屏幕沿用原来的「悠闲哼歌」。
"""
import unicodedata

CALM = "calm"

# 顺序就是同分时的优先级（实际 Tie 由出现位置决定，这里只用于文档与校验）
EMOTIONS = ("excited", "surprised", "angry", "sad", "shy", "happy")

KEYWORDS = {
    "excited": ("太棒", "好耶", "激动", "迫不及待", "超级", "简直", "太开心", "超棒", "冲呀"),
    "happy": ("哈哈", "嘿嘿", "嘻嘻", "开心", "高兴", "真好", "好棒", "喜欢", "谢谢", "好呀", "可爱"),
    "sad": ("难过", "伤心", "想哭", "失落", "孤单", "寂寞", "委屈", "舍不得", "好累", "难受", "唉"),
    "angry": ("生气", "讨厌", "可恶", "气死", "烦死", "过分", "不高兴", "哼"),
    "surprised": ("竟然", "居然", "真的吗", "不会吧", "天呐", "哎呀", "没想到", "哇"),
    "shy": ("害羞", "不好意思", "脸红", "别看我", "讨厌啦", "捂脸", "羞"),
}

_PIECES = sorted(((kw, emo) for emo, kws in KEYWORDS.items() for kw in kws),
                 key=lambda pair: -len(pair[0]))


def tag(text):
    """返回 EMOTIONS 里的一个，或 CALM。"""
    if not text:
        return CALM
    norm = unicodedata.normalize("NFKC", str(text))
    used = [False] * len(norm)
    hits = {}
    for kw, emo in _PIECES:
        start = 0
        while True:
            i = norm.find(kw, start)
            if i < 0:
                break
            if any(used[i:i + len(kw)]):
                start = i + 1
                continue
            for j in range(i, i + len(kw)):
                used[j] = True
            slot = hits.setdefault(emo, [0, i])
            slot[0] += 1
            slot[1] = min(slot[1], i)
            start = i + len(kw)
    if not hits:
        return CALM
    emo, _ = max(hits.items(), key=lambda kv: (kv[1][0], -kv[1][1]))
    return emo
