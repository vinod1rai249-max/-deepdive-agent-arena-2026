"""Streamlit UI (BLUEPRINT.md §14). Reads directly from the same SQLite
DB the CLI/API write to — works in replay mode with no API server
running (BLUEPRINT.md §13 "Replay mode"). Tabs: Report, Rejected &
Unverified, Contradictions, Trail, Cost & Latency.

Run with: streamlit run ui/streamlit_app.py
"""

from __future__ import annotations

import asyncio
import html
import sys
from pathlib import Path

# Streamlit's script runner puts this file's own directory (ui/) at the
# front of sys.path, not the project root — so `app`/`core`/`store` (all
# expected to resolve from the project root, same as every other
# entrypoint in this codebase) aren't importable unless we add it back
# explicitly. Must run before the project-root imports below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from app.runner import resynthesize_run, start_run  # noqa: E402
from core.config import get_settings  # noqa: E402
from store.repo import Repo  # noqa: E402

st.set_page_config(page_title="DeepDive", page_icon="🔎", layout="wide")

_TIER_LABELS = {1: "Primary", 2: "Major outlet", 3: "General", 4: "Low confidence"}
_STATUS_BADGE_CLASS = {"complete": "dd-ok", "failed": "dd-bad", "in_progress": "dd-info"}
_OUTCOME_BADGE_CLASS = {"rejected": "dd-bad", "unverified": "dd-warn", "partial": "dd-info"}

_CSS = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
.block-container {padding-top: 1.6rem; max-width: 1100px;}

.dd-hero {
    background: linear-gradient(135deg, #4F46E5 0%, #7C3AED 100%);
    border-radius: 16px;
    padding: 26px 30px;
    margin-bottom: 22px;
    color: white;
}
.dd-hero h1 {color: white !important; font-size: 1.7rem; margin: 0 0 4px 0; font-weight: 700;}
.dd-hero p {color: rgba(255,255,255,0.88); margin: 0; font-size: 0.92rem;}

.dd-question-card {
    background: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 18px;
}
.dd-q-label {font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: #64748B; font-weight: 700; margin-bottom: 4px;}
.dd-q-text {font-size: 1.1rem; font-weight: 600; color: #0F172A;}

div[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 12px 14px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.dd-section-card {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 14px;
    padding: 20px 24px;
    margin-bottom: 16px;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
}
.dd-section-title {
    font-size: 1.05rem;
    font-weight: 700;
    color: #0F172A;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 2px solid #EEF2FF;
}
.dd-claim {margin: 0 0 9px 0; line-height: 1.65; color: #1E293B; font-size: 0.96rem;}
.dd-cite {color: #6366F1; font-weight: 700; font-size: 0.78em; margin-left: 2px;}
.dd-gap-note {color: #94A3B8; font-style: italic; font-size: 0.88rem; margin: 6px 0;}

.dd-badge {
    display: inline-block;
    padding: 2px 11px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.02em;
}
.dd-ok   {background: #DCFCE7; color: #166534;}
.dd-bad  {background: #FEE2E2; color: #991B1B;}
.dd-warn {background: #FEF3C7; color: #92400E;}
.dd-info {background: #DBEAFE; color: #1E40AF;}
.dd-neutral {background: #F1F5F9; color: #475569;}

section[data-testid="stSidebar"] {background: #F8FAFC; border-right: 1px solid #E2E8F0;}
</style>
"""


@st.cache_resource
def _get_repo() -> Repo:
    settings = get_settings()
    repo = Repo(settings.database_url)
    repo.create_all()
    return repo


def _tier_badge(tier: int) -> str:
    cls = {1: "dd-ok", 2: "dd-info", 3: "dd-neutral", 4: "dd-warn"}.get(tier, "dd-neutral")
    return f'<span class="dd-badge {cls}">Tier {tier} · {_TIER_LABELS.get(tier, "Unknown")}</span>'


def _status_badge(status: str) -> str:
    cls = _STATUS_BADGE_CLASS.get(status, "dd-neutral")
    return f'<span class="dd-badge {cls}">{html.escape(status)}</span>'


def _outcome_badge(outcome: str) -> str:
    cls = _OUTCOME_BADGE_CLASS.get(outcome, "dd-neutral")
    return f'<span class="dd-badge {cls}">{html.escape(outcome)}</span>'


def main() -> None:
    settings = get_settings()
    repo = _get_repo()

    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown(
        """
        <div class="dd-hero">
          <h1>🔎 DeepDive</h1>
          <p>Self-verifying research analyst — every sentence traces back to a source, checked against it.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown("#### 🆕 New research run")
        question = st.text_area("Question", max_chars=500, key="question_input", label_visibility="collapsed", placeholder="Ask a research question...")
        if st.button("Run", disabled=not question, width="stretch", type="primary"):
            with st.spinner("Researching — this can take a minute or more..."):
                run_id = asyncio.run(start_run(settings, repo, question))
            st.session_state["run_id"] = run_id
            st.rerun()

        st.markdown("#### 📂 Or view an existing run")
        manual_run_id = st.text_input("Run ID", key="manual_run_id", label_visibility="collapsed", placeholder="Paste a run ID...")
        if manual_run_id:
            st.session_state["run_id"] = manual_run_id

    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("Start a new run or enter an existing run ID in the sidebar.")
        return

    run = repo.get_run(run_id)
    if run is None:
        st.error(f"No run found with id `{run_id}`")
        return

    trail = repo.get_trail(run_id)
    report = repo.get_report(run_id)

    st.markdown(
        f"""
        <div class="dd-question-card">
          <div class="dd-q-label">Research question</div>
          <div class="dd-q-text">{html.escape(run.question)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown("**Status**")
        st.markdown(_status_badge(trail["status"]), unsafe_allow_html=True)
    with col2:
        st.markdown("**Stop reason**")
        st.markdown(f"`{trail['stop_reason'] or '—'}`")
    with col3:
        st.metric("Iterations", trail["iterations"] if trail["iterations"] is not None else "—")
    with col4:
        st.metric("Est. cost", f"${trail['llm_calls']['total_cost_usd']:.4f}")

    tab_report, tab_rejected, tab_contradictions, tab_trail, tab_cost = st.tabs(
        ["📄 Report", "🚫 Rejected & Unverified", "⚖️ Contradictions", "🧭 Trail", "💰 Cost & Latency"]
    )

    with tab_report:
        _render_report_tab(report)

    with tab_rejected:
        _render_rejected_tab(trail, repo, settings, run_id)

    with tab_contradictions:
        _render_contradictions_tab(trail)

    with tab_trail:
        _render_trail_tab(trail)

    with tab_cost:
        _render_cost_tab(trail)


def _render_report_tab(report: list[dict]) -> None:
    """Grouped by section (not one expander per sentence): each
    sub-question's findings read as a short, bulleted brief with
    numbered citation markers, and a single consolidated source list
    per section instead of repeating the same source card under every
    sentence that uses it."""
    if not report:
        st.warning("No sentences passed verification and audit yet.")
        return

    sections: dict[str, list[dict]] = {}
    order: list[str] = []
    for sentence in report:
        name = sentence["section"]
        if name not in sections:
            sections[name] = []
            order.append(name)
        sections[name].append(sentence)

    for name in order:
        sentences = sections[name]
        real = [s for s in sentences if s["citations"]]
        gaps = [s for s in sentences if not s["citations"]]

        parts = [f'<div class="dd-section-card"><div class="dd-section-title">{html.escape(name)}</div>']

        source_list: list[dict] = []
        if real:
            source_index: dict[str, int] = {}
            for s in real:
                for c in s["citations"]:
                    if c["url"] not in source_index:
                        source_index[c["url"]] = len(source_list) + 1
                        source_list.append(c)

            for s in real:
                marks = "".join(f'<span class="dd-cite">[{source_index[c["url"]]}]</span>' for c in s["citations"])
                parts.append(f'<p class="dd-claim">• {html.escape(s["text"])} {marks}</p>')

        for s in gaps:
            parts.append(f'<p class="dd-gap-note">ℹ️ {html.escape(s["text"])}</p>')

        parts.append("</div>")
        st.markdown("".join(parts), unsafe_allow_html=True)

        if source_list:
            with st.expander(f"📚 Sources ({len(source_list)})"):
                for i, c in enumerate(source_list, start=1):
                    st.markdown(
                        f"{i}. [{c['title'] or c['url']}]({c['url']})  {_tier_badge(c['tier'])}  `{c['domain']}`",
                        unsafe_allow_html=True,
                    )


def _render_rejected_tab(trail: dict, repo, settings, run_id: str) -> None:
    """Includes the human-in-the-loop override (BLUEPRINT.md §4.1a): a
    reviewer can promote a claim the AI rejected, or explicitly confirm
    its verdict, with a required reason. This never mutates the original
    AI verdict shown above it — the override is a separate, additive
    record, so both judgments stay visible."""
    claims_by_id = {c["id"]: c for c in trail["claims"]}
    reviews_by_claim_id: dict[str, list[dict]] = {}
    for r in trail.get("human_reviews", []):
        reviews_by_claim_id.setdefault(r["claim_id"], []).append(r)

    rejected = [v for v in trail["verdicts"] if v["outcome"] in ("rejected", "unverified", "partial")]
    if not rejected:
        st.success("No claims were rejected, left unverified, or only partially supported.")
        return

    stage_options = sorted({v["stage_failed"] for v in rejected if v["stage_failed"]})
    stage_filter = st.multiselect("Filter by failed stage", stage_options)
    for v in rejected:
        if stage_filter and v["stage_failed"] not in stage_filter:
            continue
        claim = claims_by_id.get(v["claim_id"], {})
        claim_id = v["claim_id"]
        current_status = claim.get("status", v["outcome"])

        with st.expander(f"{claim.get('text', '(claim not found)')}"):
            st.markdown(_outcome_badge(current_status), unsafe_allow_html=True)
            st.write(f"**AI verdict:** {v['outcome']}  (stage failed: `{v['stage_failed']}`)")
            st.write(f"**Entailment:** {v['entailment']}")
            st.write(f"**Evidence span the model quoted:** {v['evidence_span']}")
            st.write(f"**Reason:** {v['reason']}")

            for review in reviews_by_claim_id.get(claim_id, []):
                st.info(
                    f"**Human review:** {review['previous_status']} → {review['new_status']} "
                    f"— \"{review['reason']}\" ({review['reviewed_at']})"
                )

            st.divider()
            reason = st.text_input("Reason for your decision", key=f"reason_{claim_id}")
            col1, col2 = st.columns(2)
            if col1.button("✅ Promote to verified", key=f"promote_{claim_id}", disabled=not reason, width="stretch"):
                repo.override_claim_status(run_id, claim_id, "verified", reason)
                with st.spinner("Re-synthesizing the report with this claim included..."):
                    asyncio.run(resynthesize_run(settings, repo, run_id))
                st.rerun()
            if col2.button("👍 Confirm AI's verdict", key=f"confirm_{claim_id}", disabled=not reason, width="stretch"):
                repo.override_claim_status(run_id, claim_id, v["outcome"], reason)
                st.rerun()


def _render_contradictions_tab(trail: dict) -> None:
    contradictions = trail.get("contradictions", [])
    claims_by_id = {c["id"]: c for c in trail["claims"]}
    if not contradictions:
        st.info("No contradictions detected between verified sources.")
        return
    for c in contradictions:
        claim_a = claims_by_id.get(c["claim_a"], {}).get("text", "(unknown)")
        claim_b = claims_by_id.get(c["claim_b"], {}).get("text", "(unknown)")
        with st.expander(f"⚖️ {c['explanation']}"):
            st.markdown(f"- {claim_a}")
            st.markdown(f"- {claim_b}")


def _render_trail_tab(trail: dict) -> None:
    st.markdown("#### Sub-questions & coverage")
    for sq in trail["sub_questions"]:
        st.markdown(f"**{sq['text']}**")
        coverage = min(max(sq["coverage"], 0.0), 1.0)
        st.progress(coverage, text=f"{coverage * 100:.0f}% coverage")
        st.caption("Needs: " + "; ".join(sq["acceptance_criteria"]))
        st.markdown("")

    st.markdown("#### Searches")
    if not trail["searches"]:
        st.caption("No searches were recorded (no search provider configured?).")
    else:
        st.dataframe(
            [
                {"iteration": s["iteration"], "query": s["query"], "provider": s["provider"], "results": s["result_count"]}
                for s in trail["searches"]
            ],
            width="stretch",
            hide_index=True,
        )

    st.markdown("#### All claims")
    st.dataframe(trail["claims"], width="stretch", hide_index=True)


def _render_cost_tab(trail: dict) -> None:
    calls = trail["llm_calls"]
    col1, col2, col3 = st.columns(3)
    col1.metric("Total Mesh calls", calls["count"])
    col2.metric("Total cost", f"${calls['total_cost_usd']:.4f}")
    col3.metric("Tokens (in / out)", f"{calls['total_input_tokens']:,} / {calls['total_output_tokens']:,}")

    st.markdown("#### Calls by node")
    by_node = calls.get("by_node", {})
    if by_node:
        st.dataframe(
            [{"node": k, "calls": v} for k, v in sorted(by_node.items(), key=lambda kv: -kv[1])],
            width="stretch",
            hide_index=True,
        )
    st.caption("Cost estimated from llm/pricing.yaml — confirm against Mesh's real billing before trusting it for anything load-bearing.")


main()
