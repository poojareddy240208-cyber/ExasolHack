# flake8: noqa: E501
"""OmniSync XAI Control — the operator console for explainable retail decisions.

The frontend is intentionally thin: all data and mutations are supplied by
``frontend.services.live_bridge``.  This module owns the visual language,
empty states, and the small amount of interaction state needed by Streamlit.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import streamlit as st

# ``streamlit run frontend/app.py`` puts only ``frontend`` on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from frontend.services.live_bridge import (  # noqa: E402
    approve_churn_action,
    get_churn_alerts,
    get_identity_investigation,
    get_inventory,
    get_return_risk,
    get_system_health,
    trigger_surge,
)

st.set_page_config(
    page_title="OmniSync XAI Control",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# -------------------------------------------------------------------------------------------------
# Editorial monochrome design system
# -------------------------------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Manrope:wght@400;500;600;700;800&display=swap');

:root {
  --canvas: #F7F7F5;
  --surface: #FFFFFF;
  --ink: #171717;
  --muted: #73736f;
  --faint: #A6A6A0;
  --line: #E7E7E2;
  --line-strong: #D4D4CE;
  --charcoal: #20201E;
  --charcoal-soft: #2E2E2B;
  --green: #167A4A;
  --green-bg: rgba(22, 122, 74, .12);
  --yellow: #936A00;
  --yellow-bg: rgba(214, 163, 0, .14);
  --red: #B3261E;
  --red-bg: rgba(179, 38, 30, .12);
  --blue: #2556A8;
  --blue-bg: rgba(37, 86, 168, .10);
}

html, body, .stApp, button, input, textarea, select,
[class*="st-"]:not([data-testid="stIconMaterial"]) {
  font-family: 'Manrope', 'Segoe UI', sans-serif;
}
.stApp { background: var(--canvas); color: var(--ink); }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { background: var(--charcoal); border-right: 0; }
[data-testid="stSidebar"] * { color: #F5F5F0; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #B9B9B1; }
.block-container { max-width: 1480px; padding: 2.2rem 3.2rem 4rem; }
h1, h2, h3, h4 { color: var(--ink); letter-spacing: -.04em; font-weight: 800; }
h1 { font-size: clamp(2.2rem, 4vw, 4.5rem); line-height: .98; }
h2 { font-size: 1.65rem; }
h3 { font-size: 1.2rem; }
p, li, label { color: var(--ink); }
small, .stCaption, [data-testid="stCaptionContainer"] { color: var(--muted) !important; }
:focus-visible { outline: 2px solid var(--blue) !important; outline-offset: 3px; }

/* App header */
.os-header { display: flex; justify-content: space-between; gap: 2rem; align-items: flex-end;
  border-bottom: 1px solid var(--line-strong); padding-bottom: 1.55rem; margin-bottom: 1.8rem; }
.os-kicker { color: var(--muted); font: 500 .72rem 'DM Mono', monospace; letter-spacing: .08em;
  text-transform: uppercase; margin-bottom: .65rem; }
.os-title { font-size: clamp(2.3rem, 5vw, 4.8rem); line-height: .92; letter-spacing: -.075em; font-weight: 800; }
.os-header-copy { max-width: 650px; color: var(--muted); font-size: .98rem; line-height: 1.65; text-align: right; }
.os-header-copy b { color: var(--ink); }
.os-section-intro { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; margin: .4rem 0 1.4rem; }
.os-section-intro h2 { margin: 0; }
.os-section-intro p { color: var(--muted); font-size: .9rem; max-width: 62ch; margin: 0; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 1.8rem; border-bottom: 1px solid var(--line-strong); }
.stTabs [data-baseweb="tab"] { color: var(--muted); padding: .8rem .05rem .95rem; font-weight: 700; font-size: .85rem; }
.stTabs [aria-selected="true"] { color: var(--ink); }
.stTabs [data-baseweb="tab-highlight"] { background: var(--ink); height: 3px; }
.stTabs [data-baseweb="tab-border"] { background: transparent; }

/* Flat surfaces and metric hierarchy */
.os-card { background: var(--surface); border: 1px solid var(--line); padding: 1.35rem 1.45rem; }
.os-card-dark { background: var(--charcoal); color: #fff; border: 1px solid var(--charcoal); padding: 1.35rem 1.45rem; }
.os-card-dark * { color: #fff; }
.os-eyebrow { color: var(--muted); font: 500 .68rem 'DM Mono', monospace; letter-spacing: .04em; text-transform: uppercase; }
.os-micro { color: var(--muted); font-size: .78rem; line-height: 1.5; }
.os-rule { height: 1px; background: var(--line); margin: 1.35rem 0; }
[data-testid="stMetric"] { padding: .3rem 1.25rem .4rem 0; border-right: 1px solid var(--line); }
[data-testid="stMetricLabel"] p { color: var(--muted); font-size: .74rem; font-weight: 700; }
[data-testid="stMetricValue"] { color: var(--ink); font-size: 2rem; font-weight: 800; letter-spacing: -.06em; font-variant-numeric: tabular-nums; }
[data-testid="stMetricDelta"] { font-size: .72rem; }

/* Buttons and controls */
.stButton > button, .stFormSubmitButton > button { background: var(--charcoal); color: white; border: 1px solid var(--charcoal);
  border-radius: 0; min-height: 2.75rem; font-weight: 700; transition: transform .18s ease, background .18s ease; }
.stButton > button:hover, .stFormSubmitButton > button:hover { background: #41413D; border-color: #41413D; color: white; transform: translateY(-1px); }
.os-surge + div button, .st-key-trigger_surge button { background: var(--red); border-color: var(--red); }
.st-key-trigger_surge button:hover { background: #8F1E19; border-color: #8F1E19; }
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"],
.stTextInput input, .stNumberInput input { background: white !important; color: var(--ink) !important; border-color: var(--line-strong) !important; border-radius: 0 !important; }
.stTextInput label, .stSelectbox label { color: var(--muted) !important; font-size: .76rem !important; font-weight: 700 !important; }

/* Alerts / badges */
[data-testid="stAlert"] { border-radius: 0; border: 1px solid var(--line); border-left: 3px solid var(--blue); background: var(--blue-bg); }
[data-testid="stAlert"] p { color: var(--ink); }
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]) { border-left-color: var(--green); background: var(--green-bg); }
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]) { border-left-color: var(--red); background: var(--red-bg); }
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]) { border-left-color: var(--yellow); background: var(--yellow-bg); }
.os-badge { display: inline-flex; align-items: center; gap: .4rem; border-radius: 999px; padding: .34rem .72rem;
  font-size: .72rem; font-weight: 800; line-height: 1; border: 1px solid transparent; }
.os-badge::before { content: ''; width: .38rem; height: .38rem; border-radius: 50%; background: currentColor; }
.os-badge.green { color: var(--green); background: var(--green-bg); }
.os-badge.yellow { color: var(--yellow); background: var(--yellow-bg); }
.os-badge.red { color: var(--red); background: var(--red-bg); }
.os-badge.blue { color: var(--blue); background: var(--blue-bg); }

/* Sidebar */
.os-brand { color: #FFF !important; font-size: 1.18rem; font-weight: 800; letter-spacing: -.04em; }
.os-sidebar-kicker { color: #7EE0A8 !important; font: 500 .68rem 'DM Mono', monospace; letter-spacing: .08em; text-transform: uppercase; margin: .5rem 0 .35rem; }
.os-sidebar-copy { color: #9C9C95 !important; font-size: .78rem; line-height: 1.5; margin-bottom: 1.5rem; }
.os-sidebar-heading { color: #FFF !important; font-size: .82rem; font-weight: 800; margin: 1.8rem 0 .55rem; }
.os-health { display: grid; grid-template-columns: 1rem 1fr auto; gap: .6rem; align-items: center; padding: .72rem 0; border-bottom: 1px solid rgba(255,255,255,.11); }
.os-health-dot { width: .55rem; height: .55rem; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 4px rgba(22,122,74,.18), 0 0 13px rgba(88,224,151,.7); animation: pulse 2.4s ease-in-out infinite; }
.os-health-dot.down { background: var(--red); box-shadow: 0 0 0 4px rgba(179,38,30,.2), 0 0 13px rgba(240,95,87,.7); }
.os-health-name { color: #F4F4EE; font-size: .8rem; font-weight: 700; }
.os-health-state { color: #7EE0A8; font: 500 .64rem 'DM Mono', monospace; }
.os-health-state.down { color: #FF9D96; }
.os-health-latency { grid-column: 2 / 4; color: #888880 !important; font: .67rem 'DM Mono', monospace; }
.os-fresh { color: #85857D !important; font-size: .7rem; line-height: 1.5; margin-top: .9rem; }
@keyframes pulse { 50% { opacity: .55; transform: scale(.88); } }

/* Inventory */
.os-product-head { display: flex; justify-content: space-between; gap: 1rem; align-items: start; }
.os-product-name { font-size: 1.55rem; font-weight: 800; letter-spacing: -.055em; }
.os-product-sku { color: var(--muted); font: .7rem 'DM Mono', monospace; margin-top: .25rem; }
.os-metric-strip { display: grid; grid-template-columns: repeat(3, 1fr); margin: 1.6rem 0 1.2rem; }
.os-metric-block { border-top: 2px solid var(--ink); padding: .7rem 1rem .35rem 0; margin-right: 1rem; }
.os-metric-block + .os-metric-block { padding-left: 1rem; border-left: 1px solid var(--line); }
.os-metric-label { color: var(--muted); font-size: .7rem; font-weight: 700; }
.os-metric-number { color: var(--ink); font-size: clamp(2rem, 4vw, 3.6rem); font-weight: 800; letter-spacing: -.08em; line-height: 1; margin-top: .35rem; font-variant-numeric: tabular-nums; }
.os-metric-number.accent { color: var(--red); }
.os-stock-bar { height: .65rem; display: flex; background: #EEEDE8; margin: .8rem 0 .35rem; overflow: hidden; }
.os-stock-sale { background: var(--charcoal); transition: width .45s ease; }
.os-stock-held { background: #D6A300; transition: width .45s ease; }
.os-stock-legend { display: flex; justify-content: space-between; color: var(--muted); font-size: .68rem; }
.os-xai { border-left: 3px solid var(--blue); background: var(--blue-bg); padding: 1rem 1.1rem; }
.os-xai.green { border-left-color: var(--green); background: var(--green-bg); }
.os-xai.red { border-left-color: var(--red); background: var(--red-bg); }
.os-xai-title { color: var(--muted); font: 500 .67rem 'DM Mono', monospace; letter-spacing: .03em; text-transform: uppercase; margin-bottom: .45rem; }
.os-xai-copy { font-size: .94rem; line-height: 1.55; margin: 0; font-weight: 600; }

/* Identity */
.os-case { background: var(--charcoal); color: #fff; padding: 1.6rem; position: relative; overflow: hidden; }
.os-case::after { content: 'EVIDENCE'; position: absolute; right: -1.2rem; bottom: -.9rem; color: rgba(255,255,255,.045); font-size: 5rem; font-weight: 800; letter-spacing: -.1em; }
.os-case * { color: #fff; }
.os-case-label { color: #9B9B93 !important; font: .68rem 'DM Mono', monospace; text-transform: uppercase; }
.os-confidence { font-size: clamp(4rem, 9vw, 8rem); font-weight: 800; letter-spacing: -.12em; line-height: .9; margin: .7rem 0 .45rem; }
.os-confidence small { color: #B4B4AC !important; font-size: 1rem; letter-spacing: -.02em; }
.os-decision { display: inline-block; background: #F1F1EC; color: var(--ink) !important; padding: .55rem .75rem; font-size: .72rem; font-weight: 800; }
.os-evidence { background: var(--surface); border: 1px solid var(--line); padding: 1.15rem 1.25rem; min-height: 11rem; }
.os-evidence-title { display: flex; justify-content: space-between; gap: 1rem; border-bottom: 1px solid var(--line); padding-bottom: .75rem; margin-bottom: .85rem; font-weight: 800; }
.os-evidence-count { color: var(--muted); font: .72rem 'DM Mono', monospace; }
.os-evidence-list { display: grid; gap: .55rem; margin: 0; padding: 0; list-style: none; }
.os-evidence-list li { border-left: 2px solid var(--green); background: var(--green-bg); padding: .55rem .65rem; font-size: .8rem; line-height: 1.45; }
.os-evidence.counter .os-evidence-list li { border-left-color: var(--yellow); background: var(--yellow-bg); }
.os-empty { color: var(--muted); font-size: .8rem; padding: .6rem 0; }

/* Returns */
.os-return-card { border-top: 3px solid var(--ink); background: var(--surface); border-left: 1px solid var(--line); border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); padding: 1.4rem; height: 100%; }
.os-return-card.vip { border-top-color: var(--green); }
.os-return-card.wardrobe { border-top-color: var(--red); }
.os-return-name { font-size: 1.3rem; font-weight: 800; letter-spacing: -.05em; }
.os-return-role { color: var(--muted); font-size: .76rem; margin: .25rem 0 1.15rem; }
.os-return-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.os-return-stat { border-top: 1px solid var(--line); padding-top: .55rem; }
.os-return-value { font-size: 1.75rem; font-weight: 800; letter-spacing: -.07em; }
.os-return-label { color: var(--muted); font-size: .69rem; font-weight: 700; }
.os-review { margin-top: .9rem; border: 1px solid var(--red); background: var(--red-bg); color: var(--red); padding: .7rem .8rem; font-size: .76rem; line-height: 1.4; font-weight: 700; }

/* Ticket radar */
.os-ticket-summary { display: flex; gap: .55rem; flex-wrap: wrap; align-items: center; margin: .5rem 0; }
.os-ticket-id { font: 500 .76rem 'DM Mono', monospace; color: var(--muted); }
.os-ticket-customer { font-size: 1rem; font-weight: 800; letter-spacing: -.03em; }
.stExpander { border: 1px solid var(--line) !important; border-radius: 0 !important; background: var(--surface); margin-bottom: .7rem; }
.stExpander details summary p { font-weight: 700; }
.os-detail-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: .7rem; margin: .8rem 0 1rem; }
.os-detail { border-top: 1px solid var(--line); padding-top: .55rem; }
.os-detail-label { color: var(--muted); font: .66rem 'DM Mono', monospace; text-transform: uppercase; }
.os-detail-value { font-weight: 800; margin-top: .3rem; font-size: .9rem; }
.os-action-card { background: var(--charcoal); color: #fff; padding: 1rem 1.15rem; }
.os-action-card * { color: #fff; }
.os-action-label { color: #A7A79F !important; font: .67rem 'DM Mono', monospace; text-transform: uppercase; }
.os-action-copy { font-size: 1rem; font-weight: 800; margin: .25rem 0 .8rem; }
.os-action-why { border-top: 1px solid rgba(255,255,255,.16); padding-top: .7rem; color: #D0D0C8 !important; font-size: .8rem; line-height: 1.5; }
.os-executed { color: var(--green); background: var(--green-bg); padding: .65rem .8rem; font-size: .75rem; font-weight: 700; margin-top: .7rem; }

@media (max-width: 820px) {
  .block-container { padding-left: 1.2rem; padding-right: 1.2rem; }
  .os-header { display: block; }
  .os-header-copy { text-align: left; margin-top: 1rem; }
  .os-metric-strip { grid-template-columns: 1fr; }
  .os-metric-block + .os-metric-block { padding-left: 0; border-left: 0; margin-top: .7rem; }
  .os-detail-grid { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; }
}
</style>
"""

st.markdown(CSS, unsafe_allow_html=True)


# -------------------------------------------------------------------------------------------------
# Safe formatting helpers
# -------------------------------------------------------------------------------------------------
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def error_text(value: Any) -> str:
    result = as_mapping(value)
    marker = result.get("_error")
    if isinstance(marker, Mapping):
        return str(
            marker.get("message")
            or marker.get("detail")
            or "The service returned an error."
        )
    if marker:
        return str(marker)
    plain = result.get("error")
    if isinstance(plain, Mapping):
        return str(
            plain.get("message")
            or plain.get("detail")
            or "The service returned an error."
        )
    return str(plain) if plain else ""


def has_error(value: Any) -> bool:
    return bool(error_text(value))


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def count(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "n/a"


def percent(value: Any) -> str:
    if value is None:
        return "n/a"
    raw = number(value, -1)
    if raw < 0:
        return "n/a"
    return f"{raw * 100:.0f}%" if raw <= 1 else f"{raw:.0f}%"


def humanize(value: Any, fallback: str = "Not specified") -> str:
    text = str(value or "").replace("ACTION TRIGGERED:", "").replace("_", " ").strip()
    return text[:1].upper() + text[1:].lower() if text else fallback


def action_ok(result: Any) -> bool:
    data = as_mapping(result)
    return (
        not has_error(data) and data.get("ok", data.get("success", True)) is not False
    )


def freshness_text(health: Mapping[str, Any]) -> str:
    freshness = as_mapping(health.get("data_freshness"))
    age = freshness.get("age_seconds")
    if age is None:
        return "No analytics sync is currently available."
    minutes = max(0, integer(number(age) // 60))
    if freshness.get("stale"):
        return f"Last Exasol sync was {minutes} min ago · review the scheduler."
    return (
        "Last Exasol sync just now."
        if minutes < 1
        else f"Last Exasol sync {minutes} min ago."
    )


# -------------------------------------------------------------------------------------------------
# Sidebar
# -------------------------------------------------------------------------------------------------
SERVICES = (
    ("postgres_oltp", "PostgreSQL"),
    ("redis_cache", "Redis"),
    ("exasol_olap", "Exasol"),
)


def sidebar_health(health: Mapping[str, Any]) -> None:
    services = as_mapping(health.get("services"))
    rows: list[str] = []
    for key, label in SERVICES:
        service = as_mapping(services.get(key))
        is_up = str(service.get("status", "DOWN")).upper() == "UP"
        state = "UP" if is_up else "DOWN"
        latency = service.get("latency_ms")
        detail = (
            f"{number(latency):.0f} ms response"
            if is_up and latency is not None
            else "No response"
        )
        rows.append(
            f'<div class="os-health"><span class="os-health-dot {"" if is_up else "down"}"></span>'
            f'<span class="os-health-name">{label}</span><span class="os-health-state {"" if is_up else "down"}">{state}</span>'
            f'<span class="os-health-latency">{detail}</span></div>'
        )
    st.markdown("".join(rows), unsafe_allow_html=True)
    st.markdown(
        f'<div class="os-fresh">{esc(freshness_text(health))}</div>',
        unsafe_allow_html=True,
    )
    if has_error(health) or str(health.get("status", "")).upper() == "DEGRADED":
        st.warning(
            "Live services are degraded. Check the API container and refresh the console."
        )


with st.sidebar:
    st.markdown(
        '<div class="os-brand">OmniSync XAI Control</div>', unsafe_allow_html=True
    )
    st.markdown(
        '<div class="os-sidebar-kicker">Retail operations / command layer</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="os-sidebar-copy">A governed view of the decisions protecting revenue, identity, and customer trust.</div>',
        unsafe_allow_html=True,
    )
    if st.button("↻  Refresh Data", key="refresh_data", use_container_width=True):
        # The bridge owns its read caches; clearing Streamlit's data cache is the
        # supported way to make a manual refresh deterministic from the UI.
        st.cache_data.clear()
        st.rerun()
    st.markdown(
        '<div class="os-sidebar-heading">System health</div>', unsafe_allow_html=True
    )
    sidebar_health(get_system_health() or {})


# -------------------------------------------------------------------------------------------------
# Shared UI fragments
# -------------------------------------------------------------------------------------------------
def section_header(title: str, description: str) -> None:
    st.markdown(
        f'<div class="os-section-intro"><h2>{esc(title)}</h2><p>{esc(description)}</p></div>',
        unsafe_allow_html=True,
    )


def empty_response(message: str, *, warning: bool = False) -> None:
    (st.warning if warning else st.info)(message)


def inventory_item(items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not items:
        return {}
    for item in items:
        if str(item.get("name", "")).lower() == "rtx 4090":
            return item
    return items[0]


# -------------------------------------------------------------------------------------------------
# Tab 1 — Inventory Surge
# -------------------------------------------------------------------------------------------------
def inventory_tab() -> None:
    section_header(
        "Inventory Surge",
        "Predictive twin · Exasol demand signal meets PostgreSQL stock and optimistic concurrency control.",
    )
    inventory = as_mapping(get_inventory() or {})
    if has_error(inventory):
        st.error(f"Inventory feed unavailable: {error_text(inventory)}")
        return
    raw_items = inventory.get("items") or []
    items = [as_mapping(item) for item in raw_items if isinstance(item, Mapping)]
    if not items:
        empty_response(
            "No inventory is available yet. Bootstrap the demo data, then refresh the console.",
            warning=True,
        )
        return

    items_by_id = {str(item.get("item_id")): item for item in items}
    options = list(items_by_id)
    default_id = str(inventory_item(items).get("item_id") or options[0])
    selected_id = st.selectbox(
        "Focus SKU",
        options,
        index=options.index(default_id) if default_id in options else 0,
        format_func=lambda key: f'{items_by_id[key].get("name") or key}  ·  {key}',
        key="inventory_sku",
    )
    item = items_by_id[selected_id]
    physical = integer(item.get("stock"))
    held = integer(item.get("virtual_safety_buffer", item.get("active_reserved_holds")))
    available = integer(item.get("available_stock"), max(physical - held, 0))
    held = min(max(held, 0), max(physical, 0))
    held_pct = (held / physical * 100) if physical else 0
    available_pct = max(0, 100 - held_pct)

    flash = st.session_state.pop("surge_flash", None)
    if flash:
        if flash.get("ok"):
            st.success(
                f'Surge triggered for {flash.get("name", "the selected SKU")}. Inventory has been refreshed.'
            )
            st.toast("Viral surge simulation started", icon="🚨")
        else:
            st.error(
                f'Surge could not start: {flash.get("message", "the service returned no detail")}'
            )

    st.markdown(
        f'<div class="os-card"><div class="os-product-head"><div><div class="os-product-name">{esc(item.get("name") or selected_id)}</div>'
        f'<div class="os-product-sku">SKU / {esc(selected_id)} · OCC version {esc(item.get("version", "—"))}</div></div>'
        f'<span class="os-badge {"red" if held else "green"}">{"Surge protection active" if held else "Normal availability"}</span></div>'
        f'<div class="os-metric-strip"><div class="os-metric-block"><div class="os-metric-label">Physical stock</div><div class="os-metric-number">{count(physical)}</div></div>'
        f'<div class="os-metric-block"><div class="os-metric-label">Active reserved holds</div><div class="os-metric-number accent">{count(held)}</div></div>'
        f'<div class="os-metric-block"><div class="os-metric-label">Available stock</div><div class="os-metric-number">{count(available)}</div></div></div>'
        f'<div class="os-stock-bar" role="img" aria-label="{esc(available)} available and {esc(held)} reserved">'
        f'<span class="os-stock-sale" style="width:{available_pct:.1f}%"></span><span class="os-stock-held" style="width:{held_pct:.1f}%"></span></div>'
        f'<div class="os-stock-legend"><span>Available to sell · {available_pct:.0f}%</span><span>Held for forecast · {held_pct:.0f}%</span></div></div>',
        unsafe_allow_html=True,
    )

    action_col, context_col = st.columns([1, 1.65], gap="large")
    with action_col:
        st.markdown(
            '<div class="os-eyebrow">Live simulation</div>', unsafe_allow_html=True
        )
        st.markdown("#### Stress the prediction")
        st.caption(
            "Send a shopper into the SKU to watch OCC and the virtual safety buffer respond."
        )
        if st.button(
            "🚨 Trigger Viral Surge", key="trigger_surge", use_container_width=True
        ):
            with st.spinner("Opening a high-demand window…"):
                result = trigger_surge(selected_id)
            st.session_state["surge_flash"] = {
                "ok": action_ok(result),
                "name": item.get("name") or selected_id,
                "message": error_text(result)
                or as_mapping(result).get("message")
                or "Unknown response",
            }
            st.rerun()
    with context_col:
        st.markdown(
            '<div class="os-xai"><div class="os-xai-title">Exasol forecast alert</div>'
            '<p class="os-xai-copy">Action Taken: Units mathematically reserved to prevent overselling. New cart additions are locked via OCC.</p></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="os-micro" style="margin-top:.8rem">Live carts in the last 5 minutes: <b>{esc(count(item.get("active_carts_5m")))}</b>'
            f" · predicted demand window: <b>15 min</b></div>",
            unsafe_allow_html=True,
        )


# -------------------------------------------------------------------------------------------------
# Tab 2 — Identity Detective
# -------------------------------------------------------------------------------------------------
def evidence_box(title: str, evidence: Sequence[Any], tone: str, empty: str) -> str:
    lines = list(evidence or [])
    body = (
        "".join(f"<li>{esc(line)}</li>" for line in lines)
        or f'<div class="os-empty">{esc(empty)}</div>'
    )
    return (
        f'<div class="os-evidence {"counter" if tone == "counter" else ""}">'
        f'<div class="os-evidence-title"><span>{esc(title)}</span><span class="os-evidence-count">{len(lines):02d} signals</span></div>'
        f'<ul class="os-evidence-list">{body}</ul></div>'
    )


def identity_tab() -> None:
    section_header(
        "Identity Detective",
        "Evidence graph · a customer link is a case to investigate, not a shortcut to assume.",
    )
    customer_id = st.text_input(
        "Case file", value="DEMO-EVE-001", key="identity_case"
    ).strip()
    if not customer_id:
        empty_response("Enter a customer ID to open an investigation.")
        return
    result = as_mapping(get_identity_investigation(customer_id) or {})
    if has_error(result):
        if not result.get("found", True):
            empty_response(f"No investigation exists for {customer_id}.")
        else:
            st.error(f"Evidence graph unavailable: {error_text(result)}")
        return
    if result.get("found") is False:
        empty_response(f"No investigation exists for {customer_id}.")
        return

    score = max(0, min(100, integer(result.get("confidence_score"))))
    status = humanize(result.get("decision_status"), "No decision yet")
    supporting = result.get("supporting_evidence") or []
    counter = result.get("counter_evidence") or []
    st.markdown(
        f'<div class="os-case"><div class="os-case-label">Case file / {esc(customer_id)}</div>'
        f'<div class="os-confidence">{score}<small>% match confidence</small></div>'
        f'<div class="os-decision">{esc(status)}</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown("#### The evidence behind the score")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown(
            evidence_box(
                "Supporting evidence",
                supporting,
                "supporting",
                "No supporting evidence was recorded.",
            ),
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            evidence_box(
                "Counter-evidence", counter, "counter", "No counter-evidence was found."
            ),
            unsafe_allow_html=True,
        )
    st.markdown(
        '<div class="os-xai" style="margin-top:1rem"><div class="os-xai-title">Governance note</div>'
        '<p class="os-xai-copy">The decision is explainable by design: operators can see both the signals that increase confidence and the evidence that should keep a review open.</p></div>',
        unsafe_allow_html=True,
    )


# -------------------------------------------------------------------------------------------------
# Tab 3 — Context-aware returns
# -------------------------------------------------------------------------------------------------
def return_card(title: str, role: str, user_id: str, tone: str) -> None:
    # Keep the requested compact ids as the primary lookup. Older seeded
    # environments use the display names, so a not-found response gets one
    # compatibility lookup without changing the primary call.
    try:
        risk = as_mapping(get_return_risk(user_id) or {})
    except Exception as exc:  # a malformed adapter should not blank the dashboard
        risk = {"_error": {"message": str(exc)}}
    if not risk.get("found", True) and user_id in {"cust_a", "cust_b"}:
        legacy_id = "Customer A" if user_id == "cust_a" else "Customer B"
        try:
            legacy_risk = as_mapping(get_return_risk(legacy_id) or {})
        except Exception:
            legacy_risk = {}
        if legacy_risk:
            risk = legacy_risk
    if has_error(risk):
        st.error(f"Return risk unavailable for {user_id}: {error_text(risk)}")
        return
    if risk.get("found") is False:
        empty_response(f"No return history is available for {title}.")
        return
    level = max(1, min(5, integer(risk.get("risk_level"), 1)))
    rate = percent(risk.get("return_rate"))
    policy = as_mapping(risk.get("policy"))
    explanation = (
        risk.get("xai_explanation") or "No explanation was recorded for this score."
    )
    badge_tone = "green" if level <= 2 else "red" if level >= 5 else "yellow"
    st.markdown(
        f'<div class="os-return-card {tone}"><div class="os-return-name">{esc(title)}</div><div class="os-return-role">{esc(role)} · {esc(user_id)}</div>'
        f'<div class="os-return-grid"><div class="os-return-stat"><div class="os-return-label">Raw return rate</div><div class="os-return-value">{rate}</div></div>'
        f'<div class="os-return-stat"><div class="os-return-label">Contextual risk</div><div class="os-return-value">Level {level}</div></div></div>'
        f'<div style="margin:1rem 0 .8rem"><span class="os-badge {badge_tone}">{"Free returns" if level == 1 else "Human review" if risk.get("human_review_required") else "Policy guard"}</span></div>'
        f'<div class="os-xai {"green" if level <= 2 else "red"}"><div class="os-xai-title">Why the model decided this</div><p class="os-xai-copy">{esc(explanation)}</p></div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    if risk.get("human_review_required"):
        st.markdown(
            '<div class="os-review">Human review required before a penalty is enforced.</div>',
            unsafe_allow_html=True,
        )
    with st.expander("View policy details"):
        st.markdown(
            f"**{esc(policy.get('name', 'Policy not available'))}**\n\n{esc(policy.get('customer_message', ''))}\n\n"
            f"Restocking fee: ${number(policy.get('restocking_fee_usd')):.2f} · "
            f"Cash on delivery: {'available' if policy.get('cash_on_delivery_allowed', True) else 'not available'}"
        )


def returns_tab() -> None:
    section_header(
        "Context Returns",
        "Fair AI · the same raw number can lead to a different policy when the reason for each return is visible.",
    )
    columns = st.columns(2, gap="large")
    with columns[0]:
        return_card("Customer A", "Unlucky VIP", "cust_a", "vip")
    with columns[1]:
        return_card("Customer B", "Serial Wardrober", "cust_b", "wardrobe")
    st.markdown(
        '<div class="os-xai" style="margin-top:1.4rem"><div class="os-xai-title">Fairness principle</div>'
        '<p class="os-xai-copy">Supplier defects are evidence about the product, not the customer. Suspected wardrobing is evidence about behavior. Context keeps both cases from being flattened into a single raw-rate penalty.</p></div>',
        unsafe_allow_html=True,
    )


# -------------------------------------------------------------------------------------------------
# Tab 4 — NLP Ticket Radar
# -------------------------------------------------------------------------------------------------
URGENCY_TONE = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "green"}


def sentiment_display(alert: Mapping[str, Any]) -> str:
    raw = alert.get("sentiment", alert.get("churn_score"))
    if raw is None:
        return "Not scored"
    value = number(raw)
    label = (
        "Very negative"
        if value >= 0.85
        else (
            "Negative"
            if value >= 0.6
            else "Mixed" if value >= 0.4 else "Mostly neutral"
        )
    )
    return f"{label} · {value:.2f}"


def ticket_summary(alert: Mapping[str, Any]) -> str:
    ticket_id = esc(alert.get("ticket_id") or "Unassigned ticket")
    customer = esc(
        alert.get("customer_email")
        or alert.get("golden_customer_id")
        or "Unknown customer"
    )
    urgency = str(alert.get("urgency") or "UNRATED").upper()
    tone = URGENCY_TONE.get(urgency, "blue")
    return f'{ticket_id}  ·  {customer}  ·  <span class="os-badge {tone}">{esc(humanize(urgency))} urgency</span>'


def radar_tab() -> None:
    section_header(
        "NLP Ticket Radar",
        "Autonomous recovery · deep NLP routes the issue, while a human remains in the approval loop.",
    )
    approval_flash = st.session_state.pop("approval_flash", None)
    if approval_flash:
        if approval_flash.get("ok"):
            st.success(
                f'Action Executed for {approval_flash.get("ticket", "the ticket")}. '
                "The approval is recorded and idempotent."
            )
            st.toast("Action Executed", icon="✅")
        else:
            st.error(
                f'Approval failed: {approval_flash.get("message", "the service returned no detail")}'
            )

    response = as_mapping(get_churn_alerts() or {})
    if has_error(response):
        st.error(f"Ticket radar unavailable: {error_text(response)}")
        return
    alerts = [
        as_mapping(alert)
        for alert in (response.get("alerts") or [])
        if isinstance(alert, Mapping)
    ]
    if not alerts:
        empty_response(
            "No tickets need attention. New VIP risk signals will appear here as they arrive."
        )
        return

    waiting = sum(1 for alert in alerts if str(alert.get("status")) == "PENDING_REVIEW")
    critical = sum(1 for alert in alerts if str(alert.get("urgency")) == "CRITICAL")
    metrics = st.columns(3)
    metrics[0].metric("Awaiting approval", count(waiting))
    metrics[1].metric("Critical urgency", count(critical))
    metrics[2].metric(
        "Signals in window", count(response.get("count_in_window", len(alerts)))
    )

    def sort_key(alert: Mapping[str, Any]) -> tuple[int, float]:
        rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(
            str(alert.get("urgency")), 0
        )
        return (-rank, -number(alert.get("churn_score", alert.get("sentiment"))))

    for alert in sorted(alerts, key=sort_key):
        ticket_id = str(alert.get("ticket_id") or "ticket")
        with st.expander(ticket_summary(alert), expanded=True):
            urgency = str(alert.get("urgency") or "UNRATED").upper()
            tone = URGENCY_TONE.get(urgency, "blue")
            detail_cols = st.columns(3, gap="medium")
            with detail_cols[0]:
                st.markdown(
                    f'<div class="os-detail"><div class="os-detail-label">Sentiment</div>'
                    f'<div class="os-detail-value">{esc(sentiment_display(alert))}</div></div>',
                    unsafe_allow_html=True,
                )
            with detail_cols[1]:
                st.markdown(
                    f'<div class="os-detail"><div class="os-detail-label">Urgency</div>'
                    f'<div class="os-detail-value"><span class="os-badge {tone}">{esc(humanize(urgency))}</span></div></div>',
                    unsafe_allow_html=True,
                )
            with detail_cols[2]:
                st.markdown(
                    f'<div class="os-detail"><div class="os-detail-label">Root cause</div>'
                    f'<div class="os-detail-value">{esc(humanize(alert.get("root_cause")))}</div></div>',
                    unsafe_allow_html=True,
                )
            action = alert.get("recommended_action") or alert.get("action_triggered")
            explanation = (
                alert.get("xai_explanation")
                or "No rationale was recorded for this recommendation."
            )
            st.markdown(
                f'<div class="os-action-card"><div class="os-action-label">AI recommendation</div><div class="os-action-copy">{esc(humanize(action))}</div>'
                f'<div class="os-action-why">{esc(explanation)}</div></div>',
                unsafe_allow_html=True,
            )
            status = str(alert.get("status") or "").upper()
            if status == "ACTION_EXECUTED":
                st.markdown(
                    '<div class="os-executed">Action executed · idempotency guard active. Re-approval will not run it twice.</div>',
                    unsafe_allow_html=True,
                )
            else:
                if st.button(
                    "Approve & Execute Action",
                    key=f"approve_{ticket_id}",
                    use_container_width=True,
                ):
                    with st.spinner("Executing the approved recovery…"):
                        result = approve_churn_action(ticket_id, "Demo_Judge")
                    st.session_state["approval_flash"] = {
                        "ok": action_ok(result),
                        "ticket": ticket_id,
                        "message": error_text(result)
                        or as_mapping(result).get("message")
                        or "Unknown response",
                    }
                    st.rerun()


# -------------------------------------------------------------------------------------------------
# Canvas
# -------------------------------------------------------------------------------------------------
st.markdown(
    '<div class="os-header"><div><div class="os-kicker">OmniSync / explainable operations</div><div class="os-title">Control the signal.<br>Protect the experience.</div></div>'
    '<div class="os-header-copy"><b>Real-time retail intelligence</b><br>One editorial workspace for inventory protection, identity evidence, fair returns, and human-governed recovery.</div></div>',
    unsafe_allow_html=True,
)

tab_inventory, tab_identity, tab_returns, tab_radar = st.tabs(
    ["Inventory Surge", "Identity Detective", "Context Returns", "NLP Ticket Radar"]
)
with tab_inventory:
    inventory_tab()
with tab_identity:
    identity_tab()
with tab_returns:
    returns_tab()
with tab_radar:
    radar_tab()
