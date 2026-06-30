# v: bsh-orc-02
"""
Broadsheet — orchestrator (the conductor).

Wires the five modules into one edition cycle and manages persisted state.

Two phases per day, mirroring the twice-daily cadence:

  PRODUCE an edition:
    load state -> fetch sources -> synthesise (using world_state + reader_model,
    plus any TESTS the agent wants to probe) -> decide whether to ask a question
    -> save edition + pending_question -> return edition for display.

  INGEST reactions (after the reader reacts in the UI):
    load state -> record reactions (+ any answered question) -> agent re-reasons
    the reader-model -> distil the world-state -> save. Next PRODUCE uses the
    sharpened memory.

Separating PRODUCE from INGEST matches reality: the edition is shown, the human
reacts over time, then the next run learns from those reactions. In the live
deployment, a timer triggers PRODUCE; the UI posts reactions that trigger INGEST
(or INGEST runs at the start of the next PRODUCE — both supported).

Network calls (RSS, flash) live in the modules and run on your machine / FC.
This file is pure wiring + state, fully testable offline with injected fakes.
"""

import os

import sources
import synthesis
import world_state as ws
import preference as pref
import question as q
import state_store as store


# ---------------- PRODUCE ----------------

def produce_edition(api_key=None, fetch_fn=None, max_per_feed=8):
    """
    Build today's edition from current memory. Returns (edition, decision).
    fetch_fn lets tests inject sample articles instead of hitting the network.
    """
    state = store.load_state()
    memory = state["memory"]

    # 0. Deferred reasoning: if reactions have accumulated since the last produce,
    #    re-derive the reader-model and distil the world-state ONCE here (instead of
    #    on every click). This keeps reactions instant and batches the model work.
    if memory.get("pending_reasoning"):
        memory = pref.update_reader_model(memory, api_key=api_key)
        latest = state["editions"][0] if state.get("editions") else None
        if latest:
            state["world_state"] = ws.update_world_state(
                state["world_state"], latest, api_key=api_key
            )
        memory["pending_reasoning"] = False

    # 1. sources
    if fetch_fn is not None:
        articles = fetch_fn()
    else:
        articles, _report = sources.fetch_all(max_per_feed=max_per_feed)

    # 2. preference -> what to emphasise + what hypotheses to TEST this edition
    reader_model = pref.preference_summary_for_synthesis(memory)
    tests = pref.extract_tests(reader_model)
    pref_block = reader_model
    if tests:
        pref_block += "\n\nDELIBERATE TESTS TO INCLUDE THIS EDITION (probe these to confirm/revise hypotheses): " + "; ".join(tests)

    # 2b. suppression: stories the reader marked 👎/❌ should not reappear (AI judges
    #     "same story"). Build the list from the reaction log.
    suppressed = [
        f"{r['item'].get('headline','')} ({r['item'].get('category','')})"
        for r in memory.get("reaction_log", [])
        if r.get("reaction") in ("meh", "never")
    ]

    # 2c. dating: preserve each story's first-seen date across reappearances.
    first_seen_map = {}
    for ed in state.get("editions", []):
        for it in ed.get("items", []):
            lnk = it.get("link")
            if lnk and lnk not in first_seen_map:
                first_seen_map[lnk] = it.get("first_seen", ed.get("date"))

    # 3. synthesise the edition (lead + tagged items), memory-aware
    edition = synthesis.synthesise(
        articles,
        world_state=state["world_state"],
        preference_summary=pref_block,
        suppressed=suppressed,
        first_seen_map=first_seen_map,
        api_key=api_key,
    )

    # 4. agent decides whether to ask one binary question
    decision = q.decide_question(reader_model, memory.get("edition_count", 0), api_key=api_key)

    # 5. persist
    state["editions"] = [edition] + state.get("editions", [])
    state["pending_question"] = decision if decision.get("ask") else None
    memory["edition_count"] = memory.get("edition_count", 0) + 1
    store.save_state(state)

    return edition, decision


# ---------------- INGEST ----------------

def ingest_reactions(reactions, question_answer=None, api_key=None):
    """
    Record the reader's reactions + (optional) answer to the pending question.

    This is intentionally CHEAP and FAST: it only records signals to memory and
    returns. The expensive agent reasoning (re-deriving the reader-model and
    distilling the world-state) is deferred to the next produce_edition() call,
    where it runs ONCE over all accumulated reactions instead of on every click.

    reactions: list of (reaction_str, item_dict)
    question_answer: True/False/None  (answer to state['pending_question'])
    """
    state = store.load_state()
    memory = state["memory"]

    # record item reactions (pure local memory write, no model call)
    for (reaction, item) in reactions:
        pref.record_reaction(memory, reaction, item)

    # fold a question answer in as an extra signal line on the reader-model
    if question_answer is not None and state.get("pending_question"):
        signal = q.answer_as_reaction(state["pending_question"], bool(question_answer))
        if signal:
            memory["reader_model"] = (memory.get("reader_model", "") + "\n" + signal).strip()
        state["pending_question"] = None

    # mark that there are unprocessed reactions for the next produce to reason over
    memory["pending_reasoning"] = True

    store.save_state(state)
    return state


# ---------------- offline dry-run (mock the network) ----------------

if __name__ == "__main__":
    # Prove the full cycle wiring end-to-end with the flash calls + RSS MOCKED,
    # so we exercise produce -> ingest -> produce on real control flow & state.
    from sample_articles import SAMPLE
    import json as _json

    # fresh state for the test
    if os.path.exists(store.STATE_FILE):
        os.remove(store.STATE_FILE)

    # --- monkeypatch the three flash calls + the question call with fakes ---
    def fake_synth(prompt, api_key=None):
        # honour the structure: return valid edition JSON referencing real indices
        return _json.dumps({
            "lead": {"headline": "Resource walls rise as the tech race accelerates",
                     "brief": "China's rare-earth controls meet a battery breakthrough while the EU sets AI rules and the Fed holds."},
            "items": [
                {"i": 0, "headline": "Fed holds rates steady", "summary": "US rates unchanged amid mixed inflation signals."},
                {"i": 5, "headline": "Open model rivals closed labs", "summary": "A lab released open weights matching proprietary scores."},
                {"i": 3, "headline": "Solid-state battery breakthrough", "summary": "A new method may make solid-state batteries viable at scale."},
                {"i": 8, "headline": "Brain memory pathway found", "summary": "Researchers mapped a sleep-active route for long-term memory."},
            ],
        })
    synthesis._call_flash = fake_synth

    def fake_qdecide(prompt, api_key=None):
        return _json.dumps({"ask": False, "reason": "Too early; no high-impact uncertainty yet."})
    q._call_flash = fake_qdecide

    def fake_reason(prompt, api_key=None):
        return ("Markets: reader NEVERed two markets items — CONFIDENT they don't want markets. "
                "Tech: LOVEd an analysis piece — TENTATIVE they prefer tech depth.\n"
                "TESTS: include one non-markets business item to check if dislike is markets-specific")
    pref._call_flash = fake_reason

    def fake_ws(prompt, api_key=None):
        return ("Trade/resources: China's rare-earth controls escalate the standoff. "
                "Macro: Fed held again. Tech/policy: EU AI rules finalised.")
    ws._call_flash = fake_ws

    print("########## PRODUCE edition 1 ##########")
    edition, decision = produce_edition(fetch_fn=lambda: SAMPLE)
    print("LEAD:", edition["lead_headline"])
    print("ITEMS:")
    for it in edition["items"]:
        print(f"   • [{it['category']}/{it['source']}] {it['headline']}")
    print("QUESTION:", "(none)" if not decision.get("ask") else decision["question"])

    print("\n########## INGEST reactions ##########")
    reactions = [
        ("never", {"headline": "Fed holds rates steady", "source": "CNBC Intl", "region": "global", "category": "business", "source_type": "markets"}),
        ("love",  {"headline": "Open model rivals closed labs", "source": "Ars Technica", "region": "global", "category": "tech", "source_type": "analysis"}),
        ("love",  {"headline": "Solid-state battery breakthrough", "source": "Japan Times", "region": "asia", "category": "world", "source_type": "newspaper"}),
    ]
    state = ingest_reactions(reactions, question_answer=None)
    print("reactions logged:", len(state["memory"]["reaction_log"]))
    print("reader_model now:\n   ", state["memory"]["reader_model"].replace("\n", "\n    "))
    print("world_state now:\n   ", state["world_state"])
    print("tests extracted:", pref.extract_tests(state["memory"]["reader_model"]))

    print("\n########## PRODUCE edition 2 (memory-aware) ##########")
    # now the agent will ask a question (override fake to ask, since it has a theory)
    def fake_qdecide2(prompt, api_key=None):
        return _json.dumps({"ask": True,
                            "question": "You've passed on markets stories — keep them out?",
                            "on_yes": "Keep markets out.", "on_no": "Reintroduce selective markets."})
    q._call_flash = fake_qdecide2
    edition2, decision2 = produce_edition(fetch_fn=lambda: SAMPLE)
    print("QUESTION:", decision2["question"] if decision2.get("ask") else "(none)")
    print("edition_count:", store.load_state()["memory"]["edition_count"])
    print("editions stored:", len(store.load_state()["editions"]))

    print("\n########## INGEST: answer the question NO ##########")
    state = ingest_reactions([], question_answer=False)
    print("reader_model tail:\n   ", state["memory"]["reader_model"].splitlines()[-1])

    # cleanup test state
    os.remove(store.STATE_FILE)
    print("\nFull engine cycle OK: produce -> ingest -> produce -> answer. State flows end-to-end.")
    print("(All flash + RSS calls were mocked here; they run live on your machine / FC.)")
