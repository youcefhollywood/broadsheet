# v: bsh-prf-02
"""
Broadsheet — preference memory (the core; memory-type #2).

NOT a scoring formula. The reactions (love / meh / never) are SIGNALS the agent
INTERPRETS. The agent maintains an evolving READER-MODEL: written judgments about
what the reader wants, including TENTATIVE hypotheses it intends to TEST by showing
similar items later and watching the reaction. Over time it confirms or revises.

Two stores:
  - reaction_log : append-only ground truth. Every reaction with the item's tags.
  - reader_model : the agent's current theory (prose judgments + confidence + tests).

Each cycle the agent is given (recent reactions + current reader-model) and reasons
out an updated reader-model. That model then shapes the next edition (selection/
emphasis) AND tells synthesis what hypotheses to TEST (deliberately include a probe).

Reaction vocabulary (signals, not weights):
  love  (❤️) = want more like this
  meh   (👎) = wasn't interesting/useful
  never (❌) = this kind of story isn't wanted

Maps to MemoryAgent scoring: accumulates experience, makes increasingly accurate
decisions, tests hypotheses, forgets disconfirmed theories.

The reasoning call is isolated in `_call_flash`; log/state/prompt logic is offline-testable.
"""

import os
import json
from datetime import datetime, timezone

MODEL_PREF = "qwen3.6-flash"

VALID_REACTIONS = {"love", "meh", "never"}


# ---------------- stores ----------------

def new_memory():
    """Empty preference memory (ships generic; fills at runtime per deployment)."""
    return {
        "reaction_log": [],     # list of {ts, reaction, item:{headline,tags...}}
        "reader_model": "",     # the agent's evolving prose theory of the reader
        "edition_count": 0,
    }


def record_reaction(memory, reaction, item):
    """Append a reaction to the log. item carries the tags the agent reasons over."""
    if reaction not in VALID_REACTIONS:
        raise ValueError(f"reaction must be one of {VALID_REACTIONS}")
    memory["reaction_log"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "reaction": reaction,
        "item": {
            "headline": item.get("headline", ""),
            "source": item.get("source", ""),
            "region": item.get("region", ""),
            "category": item.get("category", ""),
            "source_type": item.get("source_type", ""),
        },
    })
    return memory


# ---------------- reasoning prompt (pure logic) ----------------

def _recent_reactions_block(memory, n=40):
    """Render the most recent reactions for the agent to reason over."""
    recent = memory["reaction_log"][-n:]
    if not recent:
        return "(no reactions yet)"
    lines = []
    for r in recent:
        it = r["item"]
        tags = f"{it['category']}/{it['region']}/{it['source']}/{it['source_type']}"
        lines.append(f"- {r['reaction'].upper():5} | [{tags}] {it['headline']}")
    return "\n".join(lines)


def build_reader_model_prompt(memory):
    """
    Ask the agent to reason from (recent reactions + current reader-model) to an
    UPDATED reader-model. Pure logic — testable offline.
    """
    current = memory["reader_model"].strip() or "(empty — no theory yet; this is early)"
    reactions = _recent_reactions_block(memory)

    instructions = (
        "You are the editor's memory of ONE reader. You maintain an evolving THEORY of what "
        "this reader wants, based on how they react to stories. Reactions are SIGNALS to "
        "interpret, not scores:\n"
        "  LOVE  = wants more like this\n"
        "  MEH   = wasn't interesting or useful (mild negative)\n"
        "  NEVER = this kind of story isn't wanted (strong negative)\n\n"
        "Each story carries tags: category / region / source / source_type. Use these as your "
        "vocabulary, but YOU decide which dimensions actually matter for this reader — it may "
        "be a category, a region, a tone, a source, or a combination.\n\n"
        "From the CURRENT THEORY and the RECENT REACTIONS, write an UPDATED THEORY that:\n"
        "1. States your current best judgments about what the reader wants and doesn't.\n"
        "2. Marks each judgment's CONFIDENCE (confident / tentative).\n"
        "3. For TENTATIVE judgments, names a TEST: a kind of story you'll deliberately show "
        "next to confirm or revise the judgment (e.g. 'they NEVERed two markets items — test "
        "whether it's markets specifically or all business by showing one tech-business item').\n"
        "4. REVISES or DROPS judgments that recent reactions contradict. Don't cling to a "
        "disproven theory.\n"
        "5. Moves reasonably FAST — act on a clear pattern within a couple of reactions, but "
        "keep weak signals marked tentative.\n\n"
        "Write tight prose, grouped by judgment. Keep under ~1500 characters. End with a line "
        "'TESTS:' listing the specific probe stories to include next edition (or 'TESTS: none'). "
        "Output ONLY the updated theory."
    )

    return (
        f"{instructions}\n\n"
        f"CURRENT THEORY:\n{current}\n\n"
        f"RECENT REACTIONS (newest last):\n{reactions}"
    )


def _call_flash(prompt, api_key=None):
    import dashscope
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    key = api_key or os.getenv("DASHSCOPE_API_KEY")
    resp = dashscope.MultiModalConversation.call(
        api_key=key,
        model=MODEL_PREF,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        enable_thinking=False,
        result_format="message",
    )
    # Qwen can occasionally return an empty/blocked response (output is None).
    # Treat that as "no new reasoning" rather than crashing the request.
    out = getattr(resp, "output", None)
    if not out or not getattr(out, "choices", None):
        return ""
    choice = out.choices[0].message.content
    if isinstance(choice, list):
        return "".join(part.get("text", "") for part in choice)
    return str(choice)


def update_reader_model(memory, api_key=None):
    """Agent reasons from reactions -> updated reader-model. Returns memory."""
    # Only reason once there's something to reason about.
    if not memory["reaction_log"]:
        return memory
    prompt = build_reader_model_prompt(memory)
    new_model = _call_flash(prompt, api_key=api_key).strip()
    if new_model:  # only overwrite if Qwen actually returned reasoning
        memory["reader_model"] = new_model
    return memory


def extract_tests(reader_model_text):
    """Pull the 'TESTS:' line into a list of probe descriptions for synthesis."""
    if not reader_model_text:
        return []
    for line in reader_model_text.splitlines():
        if line.strip().upper().startswith("TESTS:"):
            payload = line.split(":", 1)[1].strip()
            if payload.lower() in ("none", "", "n/a"):
                return []
            return [p.strip() for p in payload.split(";") if p.strip()]
    return []


def preference_summary_for_synthesis(memory):
    """
    The reader-model text is what synthesis consumes (its 'READER LEANING' slot).
    Kept as the agent's own prose so emphasis stays interpretable.
    """
    return memory.get("reader_model", "")


if __name__ == "__main__":
    # Offline test: simulate a few cycles of reactions + reasoning (model mocked),
    # proving the log, prompt construction, test-extraction, and state flow.
    mem = new_memory()

    # Day 1: reader reacts to several items.
    record_reaction(mem, "never", {"headline": "Fed holds rates steady", "source": "CNBC Intl", "region": "global", "category": "business", "source_type": "markets"})
    record_reaction(mem, "never", {"headline": "Shipping rates spike", "source": "MarketWatch", "region": "global", "category": "business", "source_type": "markets"})
    record_reaction(mem, "love",  {"headline": "Open-weight model rivals closed labs", "source": "Ars Technica", "region": "global", "category": "tech", "source_type": "analysis"})
    record_reaction(mem, "love",  {"headline": "Solid-state battery breakthrough", "source": "Japan Times", "region": "asia", "category": "world", "source_type": "newspaper"})
    record_reaction(mem, "meh",   {"headline": "Streaming giant restructures", "source": "The Verge", "region": "global", "category": "tech", "source_type": "product-news"})

    print("=== REASONING PROMPT (agent reasons over signals) ===\n")
    p = build_reader_model_prompt(mem)
    print(p[:1500], "\n...[truncated]...\n")

    # Simulate the agent's updated reader-model output.
    fake_model = (
        "Markets/finance: reader NEVERed two markets items (Fed, shipping) — CONFIDENT they "
        "don't want pure markets/macro coverage. Tech: LOVEd an open-model analysis but only "
        "MEH on a tech product-news item — TENTATIVE that they like tech ANALYSIS/depth, not "
        "product/business churn. Science/innovation: LOVEd a battery breakthrough (tagged "
        "world, but innovation-flavoured) — TENTATIVE they like applied science/innovation "
        "regardless of region. Region: no clear regional preference yet.\n"
        "TESTS: include one tech-analysis piece and one applied-science/innovation item to "
        "confirm the depth-over-churn and innovation hypotheses; include one business item "
        "that is NOT markets to check whether the dislike is markets-specific or all-business"
    )
    mem["reader_model"] = fake_model

    print("=== UPDATED READER-MODEL (the evolving theory) ===\n")
    print(mem["reader_model"])
    print(f"\n[{len(mem['reader_model'])} chars]")

    print("\n=== TESTS EXTRACTED (probes synthesis will deliberately include) ===")
    for t in extract_tests(mem["reader_model"]):
        print("  •", t)

    print("\n=== WHAT SYNTHESIS RECEIVES (READER LEANING slot) ===")
    print(preference_summary_for_synthesis(mem)[:200], "...")
    print("\nPreference memory logic OK. (Live flash reasoning runs on your machine.)")
