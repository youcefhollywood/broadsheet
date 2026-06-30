# v: bsh-qst-02
"""
Broadsheet — the agent-judged question.

At most ONE binary (Yes/No) question per edition, shown near the top. The AGENT
ITSELF decides whether to ask: only when a binary answer would MEANINGFULLY change
the next edition. When confident, it asks nothing.

It draws on the reader-model's own uncertainties (the tentative judgments / tests).
Character: NEVER a preference quiz ("do you like crypto?"). Always either
INFERENCE-CONFIRMATION ("you've skipped markets all week so I've dropped it — keep
it that way?") or, early on, broad ORIENTATION ("lead with world news or tech?").

A Yes/No answer becomes another strong signal fed back into the reader-model.

Output contract (strict JSON):
  {"ask": true,  "question": "...", "on_yes": "...", "on_no": "..."}
  {"ask": false, "reason": "..."}
"ask: false" is a valid, expected, and frequent outcome.

Reasoning call isolated in `_call_flash`; prompt/parse logic is offline-testable.
"""

import os
import json

MODEL_Q = "qwen3.6-flash"


def build_question_prompt(reader_model, edition_count):
    """Ask the agent to DECIDE whether to ask, and if so, craft the binary. Pure logic."""
    model_block = reader_model.strip() or "(no theory yet — very early; you know little about the reader)"

    stage = (
        "VERY EARLY (you know little — a broad orientation question may be worthwhile)"
        if edition_count <= 1 else
        "ESTABLISHED (only ask if a genuine uncertainty's answer would change coverage)"
    )

    instructions = (
        "You decide whether to ask the reader ONE binary (Yes/No) question at the top of "
        "today's edition. You are NOT required to ask — silence is the right choice when you "
        "are confident enough that an answer wouldn't change what you publish.\n\n"
        "Ask ONLY if ALL hold:\n"
        "  (a) you have a specific uncertainty about this reader,\n"
        "  (b) resolving it would actually CHANGE the next edition (high impact),\n"
        "  (c) it fits a single clear Yes/No.\n\n"
        "The question MUST be one of:\n"
        "  - INFERENCE-CONFIRMATION: state a conclusion you drew and ask them to confirm/veto "
        "(e.g. 'You've passed on every markets story this week, so I've stopped featuring them "
        "— keep it that way?').\n"
        "  - ORIENTATION (only when very early / little data): a broad either-or framed as "
        "Yes/No (e.g. 'Want me to lead with world news rather than tech?').\n"
        "NEVER a bare preference quiz ('do you like X?'). NEVER ask about something a reaction "
        "already answers.\n\n"
        f"STAGE: {stage}\n\n"
        "Output STRICT JSON only:\n"
        '  if asking:    {"ask": true, "question": "<one binary Yes/No>", "on_yes": "<what '
        'you will do if Yes>", "on_no": "<what you will do if No>"}\n'
        '  if not asking:{"ask": false, "reason": "<why silence is right now>"}\n'
        "No preamble, no markdown."
    )

    return f"{instructions}\n\nCURRENT READER-MODEL:\n{model_block}\n\nEDITION #: {edition_count}"


def parse_question(model_text):
    """Parse the agent's decision JSON. Robust to fences/preamble."""
    text = model_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        # if the agent rambled, default to NOT asking (safe)
        return {"ask": False, "reason": "unpar_seable; defaulting to silence"}
    try:
        obj = json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return {"ask": False, "reason": "invalid JSON; defaulting to silence"}
    if not obj.get("ask"):
        return {"ask": False, "reason": obj.get("reason", "")}
    return {
        "ask": True,
        "question": obj.get("question", "").strip(),
        "on_yes": obj.get("on_yes", "").strip(),
        "on_no": obj.get("on_no", "").strip(),
    }


def _call_flash(prompt, api_key=None):
    import dashscope
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    key = api_key or os.getenv("DASHSCOPE_API_KEY")
    resp = dashscope.MultiModalConversation.call(
        api_key=key,
        model=MODEL_Q,
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


def decide_question(reader_model, edition_count, api_key=None):
    """Agent decides whether/what to ask. Returns the parsed decision dict."""
    prompt = build_question_prompt(reader_model, edition_count)
    return parse_question(_call_flash(prompt, api_key=api_key))


def answer_as_reaction(decision, answer_yes):
    """
    Turn a Yes/No answer into a signal line to append to the reader-model context
    next reasoning cycle (so the question's answer feeds learning).
    """
    if not decision.get("ask"):
        return ""
    chosen = decision["on_yes"] if answer_yes else decision["on_no"]
    verdict = "YES" if answer_yes else "NO"
    return f"[READER ANSWERED '{verdict}' to: {decision['question']}] -> {chosen}"


if __name__ == "__main__":
    # Offline test: prove the decide/parse logic across the three outcomes.
    print("=== PROMPT (agent decides whether to ask) ===\n")
    rm = ("Markets/finance: reader NEVERed two markets items — CONFIDENT they don't want "
          "markets. Tech: TENTATIVE they prefer analysis over product-news.\n"
          "TESTS: show one non-markets business item to check if dislike is markets-specific")
    print(build_question_prompt(rm, edition_count=5)[:900], "\n...[truncated]...\n")

    # Simulate the three possible agent decisions and prove parsing of each.
    case_ask = json.dumps({
        "ask": True,
        "question": "You've skipped every markets story this week, so I've dropped markets coverage — keep it that way?",
        "on_yes": "Keep markets out of the edition.",
        "on_no": "Reintroduce selective markets coverage and re-test.",
    })
    case_silent = json.dumps({"ask": False, "reason": "Reader-model is confident; no high-impact uncertainty to resolve."})
    case_messy = "Sure! Here's my decision:\n```json\n" + case_ask + "\n```"

    for label, raw in [("ASK", case_ask), ("SILENT", case_silent), ("MESSY-FENCED", case_messy)]:
        d = parse_question(raw)
        print(f"--- {label} ---")
        if d["ask"]:
            print("  Q:", d["question"])
            print("  on_yes:", d["on_yes"])
            print("  on_no :", d["on_no"])
            # show the answer-as-signal feedback
            print("  if YES ->", answer_as_reaction(d, True))
            print("  if NO  ->", answer_as_reaction(d, False))
        else:
            print("  (no question)  reason:", d["reason"])
        print()

    print("Agent-question logic OK. (Live flash decision runs on your machine.)")
