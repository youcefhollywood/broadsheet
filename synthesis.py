# v: bsh-syn-03
"""
Broadsheet — synthesis layer.
Turns tagged articles into an edition: ONE lead (headline + synthesised brief)
plus N short individual story summaries, each carrying its source tags so the
preference model can learn from reactions to them.

Model: qwen3.6-flash (chosen by bake-off — equal/better synthesis at ~1/3 the tokens).
Call path: native DashScope SDK, multimodal endpoint, thinking DISABLED for speed/cost.

The DashScope call is isolated in `_call_flash()`. Everything else (prompt building,
output parsing) is pure logic and testable without the API.
"""

import json
import os

MODEL_SYNTH = "qwen3.6-flash"


# ---------- prompt construction (pure logic, testable offline) ----------

def build_synthesis_prompt(articles, world_state="", preference_summary="", suppressed=None):
    """
    Construct the synthesis prompt. Returns a single string.
    - articles: list of dicts from sources.fetch_all()
    - world_state: carried-forward memory of past editions (what's ongoing)
    - preference_summary: what the reader has been learned to prefer (shapes emphasis)
    - suppressed: list of headlines/summaries the reader marked 👎 or ❌ before; the
      model JUDGES whether a candidate article is "the same story" and excludes it.
    """
    # Number the articles so the model can reference them by index in its output.
    lines = []
    for i, a in enumerate(articles):
        lines.append(
            f"[{i}] ({a['category']}/{a['region']}/{a['source']}) "
            f"{a['title']} — {a['summary']}"
        )
    article_block = "\n".join(lines)

    memory_block = ""
    if world_state.strip():
        memory_block = (
            "\n\nONGOING CONTEXT (from previous editions — use to add continuity, "
            "note what's developed; do NOT repeat verbatim):\n" + world_state.strip()
        )

    pref_block = ""
    if preference_summary.strip():
        pref_block = (
            "\n\nREADER LEANING (emphasise accordingly, but stay honest and broad — "
            "do not omit major news just because it's off-preference):\n"
            + preference_summary.strip()
        )

    suppress_block = ""
    if suppressed:
        sup_lines = "\n".join(f"- {s}" for s in suppressed)
        suppress_block = (
            "\n\nDO-NOT-REPEAT: the reader already saw and marked these stories 👎 (not "
            "useful) or ❌ (something wrong with it). Use your JUDGMENT: if any candidate "
            "article below is THE SAME STORY as one of these (even if reworded, or a minor "
            "update with no real new development), DO NOT include it again. A genuinely NEW "
            "development in an ongoing story IS allowed — judge whether there's real news.\n"
            + sup_lines
        )

    instructions = (
        "You are the editor of a daily broadsheet newspaper for a globally-minded reader. "
        "From the numbered wire articles below, produce today's edition as STRICT JSON with "
        "this exact shape:\n"
        "{\n"
        '  "lead": {"headline": "<short punchy headline>", "brief": "<150-180 word '
        "synthesised front-page brief that GROUPS related stories, draws connections, and "
        'explains what matters and why — clean newspaper prose, no bullet points>"},\n'
        '  "items": [{"i": <article index>, "headline": "<tight rewritten headline>", '
        '"summary": "<35-50 word neutral summary>", "category": "<the story\'s TRUE topic: '
        "one of world, politics, business, tech, science, sport, culture, health>\"}, ...]\n"
        "}\n"
        "Rules: the lead synthesises the 3-5 most important threads. The items cover the "
        "most notable stories of the day as short standalone summaries — select 12-15 items "
        "(fewer only if there genuinely isn't enough notable news). Include the lead stories "
        "among the items too. Reference each item by its original article index in 'i'. "
        "For 'category', judge the story's ACTUAL subject, NOT the source it came from — a "
        "tech story from a general world outlet is 'tech', not 'world'. Neutral, "
        "factual tone. Output ONLY the JSON, no preamble, no markdown fences."
    )

    return f"{instructions}{memory_block}{pref_block}{suppress_block}\n\nARTICLES:\n{article_block}"


def parse_edition(model_text, articles, today=None, first_seen_map=None):
    """
    Parse the model's JSON output into a structured edition, re-attaching source tags
    to each item (so reactions can be learned against region/category/source/type).
    Each item is stamped with `first_seen`: the date the story first appeared. If it
    reappears (carried-forward news), the ORIGINAL date is preserved via first_seen_map.
    Robust to stray markdown fences or preamble.
    """
    today = today or _today_str()
    first_seen_map = first_seen_map or {}

    text = model_text.strip()
    # strip accidental ```json fences
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        text = text.lstrip("json").strip("` \n")
    # find the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    obj = json.loads(text[start:end + 1])

    lead = obj.get("lead", {})
    items = []
    for it in obj.get("items", []):
        idx = it.get("i")
        src = articles[idx] if (isinstance(idx, int) and 0 <= idx < len(articles)) else {}
        link = src.get("link", "")
        # first_seen: keep the original date if this story (by link) appeared before
        first_seen = first_seen_map.get(link, today) if link else today
        items.append({
            "headline": it.get("headline", "").strip(),
            "summary": it.get("summary", "").strip(),
            "link": link,
            "first_seen": first_seen,
            # tags carried through for the preference model:
            "source": src.get("source", ""),
            "region": src.get("region", ""),
            "category": (it.get("category") or src.get("category", "")).strip().lower(),
            "source_type": src.get("source_type", ""),
            "article_index": idx,
        })
    return {
        "date": today,
        "lead_headline": lead.get("headline", "").strip(),
        "lead_brief": lead.get("brief", "").strip(),
        "items": items,
    }


# ---------- the DashScope call (runs on your machine, needs the key) ----------

def _call_flash(prompt, api_key=None):
    """
    Single non-streaming, thinking-DISABLED call to qwen3.6-flash via DashScope SDK.
    Returns the model's text. Isolated so the rest is testable offline.
    """
    import dashscope
    # Alibaba Cloud Qwen (DashScope) — international endpoint. Base URL:
    # https://dashscope-intl.aliyuncs.com (proof of Qwen Cloud API usage).
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    key = api_key or os.getenv("DASHSCOPE_API_KEY")

    resp = dashscope.MultiModalConversation.call(
        api_key=key,
        model=MODEL_SYNTH,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # synthesis needs no web search; disable thinking for speed + cost
        enable_thinking=False,
        result_format="message",
    )
    # multimodal content comes back as a list of parts
    out = getattr(resp, "output", None)
    if not out or not getattr(out, "choices", None):
        return ""
    choice = out.choices[0].message.content
    if isinstance(choice, list):
        return "".join(part.get("text", "") for part in choice)
    return str(choice)


def _today_str():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def synthesise(articles, world_state="", preference_summary="",
               suppressed=None, first_seen_map=None, today=None, api_key=None):
    """Full synthesis: build prompt -> call flash -> parse edition (with dating + suppression)."""
    prompt = build_synthesis_prompt(articles, world_state, preference_summary, suppressed=suppressed)
    text = _call_flash(prompt, api_key=api_key)
    return parse_edition(text, articles, today=today or _today_str(), first_seen_map=first_seen_map)


if __name__ == "__main__":
    # Offline test: prove prompt-building and parsing work without the API.
    from sample_articles import SAMPLE

    print("=== PROMPT (what gets sent to flash) ===\n")
    p = build_synthesis_prompt(
        SAMPLE,
        world_state="Rare-earth export tensions have been building for two weeks. "
                    "The Fed has held rates at the last three meetings.",
        preference_summary="Leans toward tech and business; lukewarm on sport.",
    )
    print(p[:1400], "\n...[truncated]...\n")

    # Simulate a model JSON response to prove the parser + tag re-attachment.
    fake = json.dumps({
        "lead": {
            "headline": "Resource walls rise as the tech race accelerates",
            "brief": "A synthesised brief would go here connecting rare earths, AI rules, and batteries..."
        },
        "items": [
            {"i": 0, "headline": "Fed holds steady", "summary": "The US central bank kept rates unchanged amid mixed inflation."},
            {"i": 5, "headline": "Open model rivals the closed labs", "summary": "A lab released open weights matching proprietary benchmarks."},
            {"i": 8, "headline": "Brain study maps memory pathway", "summary": "Researchers found a sleep-active signalling route for long-term memory."},
        ],
    })
    print("=== PARSED EDITION (tags re-attached for the preference model) ===\n")
    ed = parse_edition(fake, SAMPLE)
    print("LEAD:", ed["lead_headline"])
    print("     ", ed["lead_brief"][:80], "...")
    print("\nITEMS:")
    for it in ed["items"]:
        print(f"  • [{it['category']}/{it['region']}/{it['source']}] {it['headline']}")
        print(f"      {it['summary']}")
    print("\nParser + tag re-attachment OK. (Live flash call runs on your machine.)")
