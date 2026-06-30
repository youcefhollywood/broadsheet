# v: bsh-app-06
"""
Broadsheet — Function Compute Web Function entry (Flask).

Wraps the engine (orchestrator) behind a small HTTP server on port 9000.

Routes:
  GET  /                -> the newspaper page (latest edition + reactions UI)
  GET  /health          -> liveness check
  GET  /edition         -> latest edition as JSON
  POST /produce         -> run PRODUCE (the twice-daily timer hits this; also manual)
  POST /ingest          -> apply reactions / question answer (called by the page)

State persists in OSS (state_store -> OSS bucket (configured via env)).
The Qwen key comes from the DASHSCOPE_API_KEY env var (set on the function).
"""

import os
import json
from flask import Flask, request, Response

import orchestrator as orch
import state_store as store

app = Flask(__name__)

PORT = int(os.getenv("FC_SERVER_PORT", "9000"))


# ---------------- health ----------------

@app.route("/health")
def health():
    return {"ok": True}


# ---------------- produce (timer + manual) ----------------

@app.route("/produce", methods=["POST", "GET"])
def produce():
    """
    Run one PRODUCE cycle: fetch sources, synthesise an edition, decide whether to
    ask a question, persist. The twice-daily timer trigger calls this. Safe to call
    manually too. Returns the edition summary.
    """
    try:
        edition, decision = orch.produce_edition()
        return {
            "ok": True,
            "lead": edition.get("lead_headline"),
            "items": len(edition.get("items", [])),
            "asked_question": bool(decision.get("ask")),
            "question": decision.get("question") if decision.get("ask") else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


# ---------------- ingest reactions ----------------

@app.route("/ingest", methods=["POST"])
def ingest():
    """
    Apply the reader's reactions and/or an answer to the pending question, then let
    the agent re-reason its memory. Body:
      { "reactions": [ ["love", {item...}], ... ], "question_answer": true|false|null }
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        reactions = body.get("reactions", [])
        # normalise to list of (reaction, item) tuples
        norm = [(r[0], r[1]) for r in reactions if isinstance(r, (list, tuple)) and len(r) == 2]
        qa = body.get("question_answer", None)
        orch.ingest_reactions(norm, question_answer=qa)
        return {"ok": True, "applied": len(norm)}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


# ---------------- latest edition JSON ----------------

@app.route("/edition")
def edition():
    state = store.load_state()
    eds = state.get("editions", [])
    if not eds:
        return {"ok": True, "edition": None, "note": "no edition yet — call /produce"}
    return {"ok": True, "edition": eds[0], "pending_question": state.get("pending_question")}


# ---------------- the newspaper page ----------------

@app.route("/invoke", methods=["POST", "GET"])
def invoke():
    """
    The Alibaba async Time Trigger fires by POSTing to /invoke (seen in the
    function logs as 'POST /invoke'). Run one produce cycle here so the
    twice-daily timer actually generates an edition.
    """
    try:
        edition, decision = orch.produce_edition()
        return Response(
            json.dumps({"ok": True, "produced_by": "timer",
                        "lead": edition.get("lead_headline", ""),
                        "items": len(edition.get("items", [])),
                        "asked_question": bool(decision.get("ask"))}),
            mimetype="application/json",
        )
    except Exception as e:
        return Response(
            json.dumps({"ok": False, "produced_by": "timer", "error": str(e)}),
            mimetype="application/json",
            status=500,
        )


@app.route("/")
def page():
    state = store.load_state()
    eds = state.get("editions", [])
    pending = state.get("pending_question")
    html = render_page(eds[0] if eds else None, pending, state.get("memory", {}))
    resp = Response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


def render_page(edition, pending, memory):
    if not edition:
        return ("<html><body style=\"font-family:Georgia,serif;max-width:680px;"
                "margin:60px auto;padding:0 20px\"><h1>Broadsheet</h1>"
                "<p>No edition yet. The paper is produced twice daily. "
                "You can trigger one now by POSTing to <code>/produce</code>.</p>"
                "</body></html>")

    lead_h = edition.get("lead_headline", "")
    lead_b = edition.get("lead_brief", "")
    date = edition.get("date", "")
    count = memory.get("edition_count", 0)

    items = edition.get("items", [])
    # first item rides with the lead column area is full-width lead; the rest flow in columns
    col_items = items  # all items flow through the 3 columns under the lead

    # Map each previously-reacted headline -> its emoji, so markers survive a refresh.
    # (Last reaction wins if the reader changed their mind.)
    emoji_for = {"love": "&#10084;", "meh": "&#128164;", "never": "&#10006;"}
    prior = {}
    for r in memory.get("reaction_log", []):
        hl = r.get("item", {}).get("headline", "")
        if hl:
            prior[hl] = emoji_for.get(r.get("reaction", ""), "")

    cards = []
    for it in col_items:
        cat = it.get("category", "")
        src = it.get("source", "")
        h = it.get("headline", "")
        s = it.get("summary", "")
        link = it.get("link", "")
        key = (link or h).replace('"', "&quot;")
        hh = h.replace('"', "&quot;")
        reacted_emoji = prior.get(h, "")
        reacted_cls = " reacted" if reacted_emoji else ""
        cards.append(f"""
      <article class="story{reacted_cls}" data-key="{key}"
           data-headline="{hh}" data-category="{cat}"
           data-source="{src}" data-region="{it.get('region','')}"
           data-stype="{it.get('source_type','')}">
        <div class="kicker">{cat} &middot; {src}</div>
        <h3 class="hl">{h}</h3>
        <p class="dek">{s}</p>
        <span class="marker" aria-hidden="true">{reacted_emoji}</span>
        <div class="react">
          <button onclick="react(this,'love')" data-emoji="&#10084;" title="More like this">&#10084;</button>
          <button onclick="react(this,'meh')" data-emoji="&#128164;" title="Less of this">&#128164;</button>
          <button onclick="react(this,'never')" data-emoji="&#10006;" title="Not this">&#10006;</button>
        </div>
      </article>""")

    q_html = ""
    if pending and pending.get("ask"):
        q_html = f"""
      <aside class="question">
        <span class="qlabel">From the desk</span>
        <p>{pending.get('question','')}</p>
        <div class="qbtns"><button onclick="answer(true)">Yes</button>
          <button onclick="answer(false)">No</button></div>
      </aside>"""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Broadsheet &mdash; {date}</title>
<style>
  :root{{ --ink:#16140f; --paper:#f7f4ec; --rule:#16140f; --hair:#cdc6b6; --muted:#6b6354; --accent:#7a1f12; }}
  *{{box-sizing:border-box}}
  body{{font-family:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;
    background:var(--paper);color:var(--ink);margin:0;
    padding:28px clamp(14px,4vw,52px);line-height:1.46;
    background-image:radial-gradient(rgba(0,0,0,0.018) 1px,transparent 1px);background-size:3px 3px;}}
  .sheet{{max-width:1080px;margin:0 auto}}
  /* ---- masthead ---- */
  .nameplate{{text-align:center}}
  .nameplate .eyebrow{{font-size:10px;letter-spacing:.42em;text-transform:uppercase;
    color:var(--muted);margin-bottom:4px}}
  .nameplate h1{{font-family:'Playfair Display','Times New Roman',serif;
    font-weight:800;font-size:clamp(44px,8vw,84px);line-height:.92;margin:2px 0 6px;
    letter-spacing:-.5px}}
  .rule-thick{{border:0;border-top:3px solid var(--rule);margin:6px 0 0}}
  .rule-hair{{border:0;border-top:1px solid var(--rule);margin:3px 0 0}}
  .nameplate .tagline{{font-family:'Playfair Display',Georgia,serif;font-style:italic;
    font-size:clamp(14px,1.6vw,17px);color:var(--accent);margin:4px 0 2px;letter-spacing:.01em}}
  .dateline{{display:flex;justify-content:space-between;align-items:center;
    font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);
    padding:6px 2px 0}}
  /* ---- lead ---- */
  .lead{{border-bottom:2px solid var(--rule);padding:18px 0 20px;margin-bottom:0}}
  .lead h2{{font-family:'Playfair Display',Georgia,serif;font-weight:800;
    font-size:clamp(28px,4.4vw,46px);line-height:1.04;margin:0 0 12px;letter-spacing:-.4px}}
  .lead .lede{{font-size:clamp(15px,1.5vw,18px);column-count:2;column-gap:34px;
    column-rule:1px solid var(--hair);margin:0;text-align:justify;hyphens:auto}}
  .lead .lede::first-letter{{float:left;font-family:'Playfair Display',serif;font-weight:800;
    font-size:62px;line-height:.74;padding:6px 8px 0 0;color:var(--accent)}}
  /* ---- question strip ---- */
  .question{{border:1px solid var(--rule);background:#efe9da;padding:12px 16px;margin:18px 0;
    display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
  .question .qlabel{{font-size:9px;letter-spacing:.24em;text-transform:uppercase;
    color:var(--accent);border:1px solid var(--accent);padding:3px 7px;white-space:nowrap}}
  .question p{{margin:0;font-size:17px;font-style:italic;flex:1;min-width:200px}}
  .qbtns button{{font-family:inherit;font-size:13px;padding:5px 16px;margin-left:6px;
    border:1px solid var(--rule);background:var(--paper);cursor:pointer}}
  .qbtns button:hover{{background:var(--ink);color:var(--paper)}}
  /* ---- 3-column body ---- */
  .columns{{column-count:3;column-gap:30px;column-rule:1px solid var(--hair);
    padding-top:18px;margin-top:0}}
  @media(max-width:860px){{.columns{{column-count:2}} .lead .lede{{column-count:1}}}}
  @media(max-width:560px){{.columns{{column-count:1}}}}
  .story{{break-inside:avoid;padding:0 0 15px;margin-bottom:15px;
    border-bottom:1px solid var(--hair);position:relative;
    transition:background-color .2s ease, box-shadow .2s ease;border-radius:2px}}
  .story.saving{{animation:pulseSave 1s ease-in-out infinite}}
  @keyframes pulseSave{{
    0%{{background-color:transparent;box-shadow:0 0 0 0 rgba(214,178,46,0)}}
    50%{{background-color:rgba(240,206,70,0.28);box-shadow:0 0 0 6px rgba(240,206,70,0.12)}}
    100%{{background-color:transparent;box-shadow:0 0 0 0 rgba(214,178,46,0)}}
  }}
  .story.reacted{{border-left:3px solid var(--accent);padding-left:9px}}
  .story .kicker{{font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--accent);margin-bottom:3px;font-weight:600}}
  .story .hl{{font-family:'Playfair Display',Georgia,serif;font-weight:700;
    font-size:19px;line-height:1.12;margin:0 0 5px}}
  .story .dek{{font-size:13.5px;line-height:1.42;margin:0;color:#26221b;text-align:justify;hyphens:auto}}
  /* persistent marker: shows your saved reaction in the corner, always visible */
  .marker{{position:absolute;top:0;right:0;font-size:14px;line-height:1;
    width:24px;height:24px;display:none;align-items:center;justify-content:center;
    background:var(--accent);color:var(--paper);border-radius:0;z-index:1}}
  .story.reacted .marker{{display:flex}}
  /* on hover, hide the marker and reveal the buttons so you can change your choice */
  .story:hover .marker{{display:none}}
  /* reactions: hidden until hover */
  .react{{position:absolute;top:0;right:0;display:flex;gap:3px;opacity:0;
    transition:opacity .14s ease;pointer-events:none;z-index:2}}
  .story:hover .react{{opacity:1;pointer-events:auto}}
  .react button{{font-family:inherit;font-size:13px;line-height:1;width:26px;height:26px;
    display:flex;align-items:center;justify-content:center;
    border:1px solid var(--rule);background:var(--paper);cursor:pointer;padding:0;border-radius:0}}
  .react button:hover{{background:var(--ink);color:var(--paper)}}
  .react button.chosen{{background:var(--accent);color:var(--paper);border-color:var(--accent)}}
  .react button:disabled{{cursor:default}}
  /* footer */
  .colophon{{text-align:center;font-size:10px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--muted);border-top:3px double var(--rule);margin-top:18px;padding-top:12px}}
  .toast{{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);
    color:var(--paper);padding:9px 16px;font-size:13px;opacity:0;transition:.2s;
    font-family:'Helvetica Neue',Arial,sans-serif;letter-spacing:.02em;z-index:9}}
  .toast.show{{opacity:1}}
  @media(prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style></head><body>
  <div class="sheet">
    <header class="nameplate">
      <div class="eyebrow">Your Daily Intelligence</div>
      <h1>Broadsheet</h1>
      <div class="tagline">A living paper that learns from what you react to</div>
      <hr class="rule-thick"><hr class="rule-hair">
      <div class="dateline"><span>No. {count}</span><span>{date}</span><span>Synthesised by Qwen</span></div>
    </header>
    <section class="lead">
      <h2>{lead_h}</h2>
      <p class="lede">{lead_b}</p>
    </section>
    {q_html}
    <main class="columns">
      {''.join(cards)}
    </main>
    <div class="colophon">Edition No. {count} &mdash; react to teach tomorrow's paper</div>
  </div>
  <div class="toast" id="toast"></div>
<script>
function toast(m){{var t=document.getElementById('toast');t.textContent=m;t.classList.add('show');
  setTimeout(function(){{t.classList.remove('show')}},1600);}}
function react(btn,r){{
  var el=btn.closest('.story');
  var btns=el.querySelectorAll('.react button');
  var marker=el.querySelector('.marker');
  el.classList.add('saving');
  el.classList.remove('reacted');
  for(var i=0;i<btns.length;i++){{btns[i].disabled=true;btns[i].classList.remove('chosen');}}
  var emoji=btn.getAttribute('data-emoji');
  var item={{headline:el.dataset.headline,category:el.dataset.category,source:el.dataset.source,
    region:el.dataset.region,source_type:el.dataset.stype,link:el.dataset.key}};
  fetch('/ingest',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{reactions:[[r,item]]}})}})
    .then(function(res){{ if(!res.ok) throw new Error();
      el.classList.remove('saving');
      marker.innerHTML=emoji;
      el.classList.add('reacted');
      for(var i=0;i<btns.length;i++){{btns[i].disabled=false;}}
      btn.classList.add('chosen');
      toast('Noted \u2014 hover again to change it');}})
    .catch(function(){{
      el.classList.remove('saving');
      for(var i=0;i<btns.length;i++){{btns[i].disabled=false;}}
      toast('Save failed \u2014 try again');}});
}}
function answer(yes){{
  var q=document.querySelector('.question');
  var qb=q.querySelectorAll('button');
  for(var i=0;i<qb.length;i++){{qb[i].disabled=true;qb[i].style.opacity='0.4';}}
  q.insertAdjacentHTML('beforeend','<span style="font-size:12px;color:var(--muted);margin-left:6px">saving\u2026</span>');
  fetch('/ingest',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{question_answer:yes}})}})
    .then(function(res){{ if(!res.ok) throw new Error();
      toast('Thanks \u2014 memory updated');
      setTimeout(function(){{if(q)q.style.display='none';}},600);}})
    .catch(function(){{for(var i=0;i<qb.length;i++){{qb[i].disabled=false;qb[i].style.opacity='1';}}}});
}}
</script>
</body></html>"""


if __name__ == "__main__":
    # Use Python's built-in WSGI server (no gunicorn dependency). Fine for this
    # low-traffic app (a paper produced twice daily + occasional page views).
    from wsgiref.simple_server import make_server
    print(f"broadsheet listening on 0.0.0.0:{PORT}")
    make_server("0.0.0.0", PORT, app).serve_forever()
