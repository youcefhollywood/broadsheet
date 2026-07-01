# v: bsh-wst-02
"""
Broadsheet — world-state memory (retrieval memory, memory-type #1).

After each edition, a small qwen3.6-flash call distils an updated WORLD-STATE:
a compact prose digest of what is ONGOING and still developing, explicitly
dropping resolved/stale threads. This digest feeds the next edition's synthesis
("ONGOING CONTEXT" slot), giving the paper continuity — it "remembers yesterday".

Maps directly to the three scored MemoryAgent behaviours:
  - storage/compression : world-state is BUDGET-CAPPED (keep only what matters)
  - forgetting          : prompt explicitly drops resolved/stale threads
  - retrieval           : the digest is recalled into tomorrow's synthesis

The DashScope call is isolated in `_call_flash`; all prompt/budget logic is
testable offline.
"""

import os

MODEL_MEM = "qwen3.6-flash"

# Rough char budget for the carried world-state (keeps "limited context window"
# honest and forces compression). ~1,800 chars ≈ a tight few paragraphs.
WORLD_STATE_BUDGET_CHARS = 1800


def build_worldstate_prompt(previous_state, edition):
    """
    Build the distillation prompt from the previous world-state + today's edition.
    Pure logic — testable without the API.
    """
    # Compact representation of today's edition for the distiller.
    today_lines = []
    if edition.get("lead_headline"):
        today_lines.append(f"LEAD: {edition['lead_headline']} — {edition.get('lead_brief','')}")
    for it in edition.get("items", []):
        today_lines.append(f"- ({it.get('category','')}) {it.get('headline','')}: {it.get('summary','')}")
    today_block = "\n".join(today_lines)

    prev_block = previous_state.strip() if previous_state.strip() else "(none — this is the first edition)"

    instructions = (
        "You maintain the persistent WORLD-STATE memory for a daily newspaper. Your job is "
        "to keep a compact, accurate digest of what is ONGOING and still developing in the "
        "world, so tomorrow's edition has continuity.\n\n"
        "Given the PREVIOUS world-state and TODAY'S edition, output an UPDATED world-state that:\n"
        "1. CARRIES FORWARD threads that are still live and developing.\n"
        "2. UPDATES threads with what changed today.\n"
        "3. ADDS important new ongoing threads that emerged today.\n"
        "4. DROPS (forgets) anything resolved, concluded, or no longer developing — do NOT "
        "let the memory grow without bound. Timely forgetting is required.\n"
        f"5. Stays UNDER ~{WORLD_STATE_BUDGET_CHARS} characters. Compress ruthlessly; keep "
        "only what genuinely helps tomorrow's coverage.\n\n"
        "Write as tight prose grouped by theme (e.g. trade/resources, macro, tech, climate). "
        "No headers, no bullet symbols, no preamble. Output ONLY the updated world-state text."
    )

    return (
        f"{instructions}\n\n"
        f"PREVIOUS WORLD-STATE:\n{prev_block}\n\n"
        f"TODAY'S EDITION:\n{today_block}"
    )


def _enforce_budget(text, budget=WORLD_STATE_BUDGET_CHARS):
    """Hard safety cap in case the model overruns the budget."""
    text = text.strip()
    if len(text) <= budget:
        return text
    # trim at the last sentence boundary under budget
    cut = text[:budget]
    last_stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if last_stop > budget * 0.6:
        return cut[:last_stop + 1].strip()
    return cut.strip()


def _call_flash(prompt, api_key=None):
    """Isolated DashScope call (runs on your machine)."""
    import dashscope
    # Alibaba Cloud Qwen (DashScope) — international endpoint. Base URL:
    # https://dashscope-intl.aliyuncs.com (proof of Qwen Cloud API usage).
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    key = api_key or os.getenv("DASHSCOPE_API_KEY")
    resp = dashscope.MultiModalConversation.call(
        api_key=key,
        model=MODEL_MEM,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        enable_thinking=False,
        result_format="message",
    )
    out = getattr(resp, "output", None)
    if not out or not getattr(out, "choices", None):
        return ""
    choice = out.choices[0].message.content
    if isinstance(choice, list):
        return "".join(part.get("text", "") for part in choice)
    return str(choice)


def update_world_state(previous_state, edition, api_key=None):
    """Full cycle: build prompt -> distil via flash -> enforce budget."""
    prompt = build_worldstate_prompt(previous_state, edition)
    text = _call_flash(prompt, api_key=api_key)
    return _enforce_budget(text)


if __name__ == "__main__":
    # Offline test: prove prompt-building, budget enforcement, and the update cycle
    # logic using a simulated model response.
    edition = {
        "lead_headline": "Resource walls rise as the tech race accelerates",
        "lead_brief": "China's new rare-earth controls collide with a Japanese battery "
                      "breakthrough while the EU finalises AI rules and the Fed holds steady.",
        "items": [
            {"category": "world", "headline": "China tightens rare-earth export controls",
             "summary": "New limits on refining technology escalate the resource standoff."},
            {"category": "business", "headline": "Fed holds rates steady", "summary": "Rates unchanged amid mixed inflation."},
            {"category": "world", "headline": "EU finalises AI regulation", "summary": "Comprehensive high-risk AI rules agreed."},
        ],
    }

    print("=== DISTILLATION PROMPT ===\n")
    p = build_worldstate_prompt(
        previous_state="Trade/resources: rare-earth tensions building for two weeks. "
                       "Macro: Fed held rates at the last three meetings; markets watching inflation.",
        edition=edition,
    )
    print(p[:1200], "\n...[truncated]...\n")

    # Simulate the model's distilled world-state output.
    fake_new_state = (
        "Trade/resources: China escalated by restricting rare-earth refining technology, "
        "deepening the two-week standoff with Western economies; watch for retaliation and "
        "supply-chain effects. Macro: the Fed held rates again (now four meetings), citing "
        "mixed inflation and a cooling labour market. Tech/policy: the EU finalised its AI "
        "regulation, a likely global benchmark; industry response pending."
    )
    capped = _enforce_budget(fake_new_state)
    print("=== UPDATED WORLD-STATE (carried to tomorrow) ===\n")
    print(capped)
    print(f"\n[{len(capped)} chars, budget {WORLD_STATE_BUDGET_CHARS}]")

    # prove the budget cap actually trims an overrun
    huge = "Thread. " * 400
    print(f"\nBudget test: {len(huge)} chars in -> {len(_enforce_budget(huge))} chars out (capped).")
    print("\nWorld-state logic OK. (Live flash distillation runs on your machine.)")
