import sys, os

sys.path.insert(0, os.path.dirname(__file__))
from sentence_split import SentenceSplitter, weight


def collect(deltas, **kw):
    sp = SentenceSplitter(**kw)
    out = []
    for d in deltas:
        out += sp.feed(d)
    tail = sp.flush()
    if tail:
        out.append(tail)
    return out


def chars(text, n=3):
    return [text[i : i + n] for i in range(0, len(text), n)]


# 1. first sentence breaks early on a comma, the rest wait for strong marks
got = collect(chars("今天天气不错，我们一起出去玩吧。天气真好！"))
assert got == ["今天天气不错，", "我们一起出去玩吧。", "天气真好！"], got

# 2. min-char gate: an 8-char clause with only a comma keeps buffering
got = collect(chars("好，我知道了。"))
assert got == ["好，我知道了。"], got

# 3. first-sentence gate is 5 chars, so a 4-char comma clause must not cut
got = collect(chars("真的吗，那太好了呀。"))
assert got == ["真的吗，那太好了呀。"], got

# 4. flush returns the residual so the last words are never dropped
got = collect(chars("我会一直陪着你的"))
assert got == ["我会一直陪着你的"], got

# 5. long run with no punctuation still gets cut (hard_max is the trigger point)
got = collect(list("一二三四五六七八九十" * 5))
assert [weight(s) for s in got] == [40, 10], [weight(s) for s in got]
assert "".join(got) == "一二三四五六七八九十" * 5, got

# 6. punctuation-only segments never reach the TTS queue
sp = SentenceSplitter()
assert sp.feed("。。。") == []
assert sp.flush() is None

# 7. newline is a strong break
got = collect(["第一句话足够长了吧。\n", "第二句也够长了吧。"])
assert got == ["第一句话足够长了吧。", "第二句也够长了吧。"], got

# 8. semicolon counts as strong, so sentence two may break on it
got = collect(chars("你先说第一句话吧。然后第二句也够长；第三句收尾了吧。"))
assert got[-1] == "第三句收尾了吧。", got

# 9. sentence two may break on a weak mark (keeps its synthesis shorter than
#    sentence one's audio); sentence three waits for a strong mark again
got = collect(
    chars("今天天气真的很不错啊，我们一起去郊外玩吧。"
          "顺便带上一些特别好吃的小零食和几瓶矿泉水，明天也可以再出去走走看看。"),
    min_chars=18, first_min_chars=18, second_min_chars=20)
assert got == [
    "今天天气真的很不错啊，我们一起去郊外玩吧。",
    "顺便带上一些特别好吃的小零食和几瓶矿泉水，",
    "明天也可以再出去走走看看。",
], got

# 10. sentence two is force-cut at second_max_chars when it carries no
#     punctuation at all; the remainder flows on as sentence three
got = collect(list("甲" * 18 + "。" + "乙" * 40),
              min_chars=18, first_min_chars=18,
              second_min_chars=20, second_max_chars=26)
assert [weight(s) for s in got] == [18, 26, 14], [weight(s) for s in got]
assert "".join(got) == "甲" * 18 + "。" + "乙" * 40, got

print("sentence_split: 10/10 passed")
