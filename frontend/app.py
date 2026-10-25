"""
OmniSync XAI - operations dashboard (phase 1: app shell, system status, Inventory Surge, Identity Detective).

Every call to the backend goes through `frontend.services.live_bridge`; this file only draws and reacts.

Visual system: the "Ocean Depths" theme (deep navy, teal, seafoam, cream) set in DejaVu Sans. Semantic colours come
from the other theme files: Fern Green for supporting evidence, Warm Sand for caution, Burnt Orange for problems.
The one deliberately bold element is the identity confidence ring; everything else stays flat and quiet.
"""

import html
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# `streamlit run frontend/app.py` puts only the frontend folder on sys.path, so add the repository root once.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from frontend.services.live_bridge import (  # noqa: E402
    approve_churn_action,
    clear_all_caches,
    error_message,
    execute_checkout,
    get_churn_alerts,
    get_identity_investigation,
    get_inventory,
    get_return_risk,
    trigger_surge,
)
from frontend.services.status_probe import get_system_health  # noqa: E402

st.set_page_config(page_title="OmniSync XAI", page_icon="🛰️", layout="wide", initial_sidebar_state="expanded")

DEMO_CUSTOMER_ID = "DEMO-EVE-001"
RETURN_DEMO_CUSTOMERS = (
    ("Customer A", "Unlucky VIP", "Heavy returns, but the reasons matter."),
    ("Customer B", "Serial Wardrober", "Heavy returns, and the reasons are different."),
)
SURGE_ROUNDS = 3
SURGE_SHOPPERS_PER_ROUND = 15
SERVICES = (("postgres_oltp", "PostgreSQL"), ("redis_cache", "Redis"), ("exasol_olap", "Exasol"))

# --------------------------------------------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------------------------------------------
THEME_CSS = (Path(__file__).resolve().parent / "theme.css").read_text(encoding="utf-8")

st.markdown(f"<style>{THEME_CSS}</style>", unsafe_allow_html=True)


# --------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------
def esc(value: Any) -> str:
    """HTML-escape anything that came from the backend before it is placed in markup."""
    return html.escape("" if value is None else str(value))


def fmt(value: Any) -> str:
    """Whole number with thousands separators; n/a when the value is unknown."""
    if value is None:
        return "n/a"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "n/a"


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


GUIDE_STEPS = (
    ("surge", "Inventory: press Simulate TikTok Surge"),
    ("blocked", "Inventory: place an order the safeguards refuse"),
    ("identity", "Identity: open the detective's evidence"),
    ("returns", "Returns: compare Customer A and Customer B"),
    ("approve", "Radar: approve a ticket, then approve it again"),
)


def log_event(text: str, guide_step: Optional[str] = None) -> None:
    """Remember what the person just did (shown in the sidebar) and tick the matching demo-guide step."""
    events = st.session_state.setdefault("activity_log", [])
    events.append(f"{datetime.now().strftime('%H:%M:%S')}  {text}")
    del events[:-30]
    if guide_step:
        st.session_state[f"guide_{guide_step}"] = True


def eye(title: str, body: str) -> None:
    """A small eye button that opens a plain-language note about the thing next to it."""
    with st.popover("👁", help=f"What is this? {title}"):
        st.markdown(f"**{title}**\n\n{body}")


def heading(text: str, tip_title: str, tip_body: str, level: int = 3) -> None:
    """A heading with an eye button beside it."""
    title, button = st.columns([14, 1] if level <= 3 else [5, 1], vertical_alignment="center")
    title.markdown(f"{'#' * level} {text}")
    with button:
        eye(tip_title, tip_body)


# --------------------------------------------------------------------------------------------------------------
# Sidebar: brand and live service status
# --------------------------------------------------------------------------------------------------------------
def _freshness_text(health: Dict[str, Any]) -> str:
    fresh = health.get("data_freshness") or {}
    age = fresh.get("age_seconds")
    if age is None:
        return "No Exasol sync has reached PostgreSQL yet. Check that Celery Beat is running."
    minutes = int(age // 60)
    when = "just now" if minutes < 1 else f"{minutes} min ago"
    if fresh.get("stale"):
        return f"Last Exasol sync was {when}. Syncs should run every 5 minutes, so check Celery Beat."
    return f"Last Exasol sync {when}."


def _service_rows(health: Dict[str, Any]) -> str:
    rows = []
    services = health.get("services") or {}
    for key, label in SERVICES:
        service = services.get(key) or {}
        up = service.get("status") == "UP"
        latency = service.get("latency_ms")
        detail = f"{latency:.0f} ms" if up and isinstance(latency, (int, float)) else "no answer"
        state = "up" if up else "down"
        rows.append(
            f'<div class="osx-svc"><span class="osx-dot {state}" aria-hidden="true">{"🟢" if up else "🔴"}</span>'
            f'<span class="osx-svc-name">{label}</span><span class="osx-svc-state {state}">{state.upper()}</span>'
            f'<span class="osx-svc-ms">{detail}</span></div>'
        )
    return "".join(rows)


def status_panel() -> None:
    """Service lights. Runs on its own timer, so the lights stay current without reloading the page."""
    health = get_system_health()
    st.markdown('<div class="osx-section">System status</div>' + _service_rows(health), unsafe_allow_html=True)
    if health.get("error"):
        note = "Cannot reach the backend. Check that the API container is running, then reload this page."
    else:
        note = _freshness_text(health)
    st.markdown(f'<div class="osx-note">{esc(note)}</div>', unsafe_allow_html=True)
    eye(
        "System status",
        "Three lights for the three databases behind this page.\n\n"
        "- **PostgreSQL** holds orders and stock (the live shop).\n"
        "- **Redis** counts shopping carts in the last 5 minutes.\n"
        "- **Exasol** is the analytics brain: demand forecasts, identity matching, return risk and churn scores.\n\n"
        "Green means it answered; the time is how long it took. The lights refresh every 5 seconds.",
    )


live_status_panel = st.fragment(run_every="5s")(status_panel) if hasattr(st, "fragment") else status_panel

with st.sidebar:
    st.markdown(
        '<div class="osx-brand">OmniSync XAI</div><div class="osx-tag">Explainable retail operations</div>',
        unsafe_allow_html=True,
    )
    live_status_panel()
    if st.button("Refresh now", key="refresh_now", help="Drop cached answers and read everything again."):
        clear_all_caches()
        get_system_health.clear()
        st.rerun()


# --------------------------------------------------------------------------------------------------------------
# Tab 1: Inventory Surge
# --------------------------------------------------------------------------------------------------------------
def inventory_table(items: List[Dict[str, Any]]) -> str:
    """Per-SKU table. The bar splits physical stock into what can be sold and what the model is holding back."""
    rows = []
    for item in items:
        stock = int_or_zero(item.get("stock"))
        held = min(int_or_zero(item.get("virtual_safety_buffer")), stock)
        available = int_or_zero(item.get("available_stock", stock - held))
        held_pct = (held / stock * 100) if stock else 0.0
        label = f"{available} of {stock} available, {held} held back"
        rows.append(
            "<tr>"
            f'<td><div class="osx-item">{esc(item.get("name") or item.get("item_id"))}</div>'
            f'<div class="osx-sku">{esc(item.get("item_id"))}</div></td>'
            f'<td class="num">{fmt(stock)}</td><td class="num hold">{fmt(held)}</td>'
            f'<td class="num avail">{fmt(available)}</td>'
            f'<td><div class="osx-bar" role="img" aria-label="{esc(label)}">'
            f'<span class="a" style="width:{100 - held_pct:.1f}%"></span>'
            f'<span class="h" style="width:{held_pct:.1f}%"></span></div></td>'
            f'<td class="num">{fmt(item.get("active_carts_5m"))}</td>'
            f'<td class="num">v{esc(item.get("version"))}</td></tr>'
        )
    head = (
        "<thead><tr><th>Item</th><th class='num'>Physical stock</th><th class='num'>Reserved holds</th>"
        "<th class='num'>Available</th><th>Stock split</th><th class='num'>Active carts</th>"
        "<th class='num'>Version</th></tr></thead>"
    )
    return f'<div class="osx-table-wrap"><table class="osx-table">{head}<tbody>{"".join(rows)}</tbody></table></div>'


def sku_label(item: Dict[str, Any]) -> str:
    return f'{item.get("name") or item.get("item_id")} ({item.get("item_id")})'


def classify_checkout(result: Dict[str, Any]) -> str:
    """Turn the bridge's checkout result into one of: success, buffer, occ, missing, invalid, down, other."""
    if result.get("success") is not False and not result.get("error"):
        return "success"
    detail = str(result.get("error") or result.get("detail") or "").lower()
    status = result.get("status")
    if status == "CONFLICT":
        return "buffer" if ("high demand" in detail or "reserved" in detail) else "occ"
    return {"NOT_FOUND": "missing", "VALIDATION_ERROR": "invalid", "UNREACHABLE": "down"}.get(status, "other")


def show_checkout_result(flash: Dict[str, Any]) -> None:
    """Explain the outcome of the last order attempt in plain language."""
    result, kind, snap, qty = flash["result"], flash["kind"], flash["snapshot"], flash["quantity"]
    if kind == "success":
        order = result.get("order_id") or "your order"
        st.success(f"Order {order} placed for {qty}. {fmt(result.get('remaining_stock'))} left in stock.")
    elif kind == "buffer":
        stock, held = int_or_zero(snap.get("stock")), int_or_zero(snap.get("virtual_safety_buffer"))
        reason = (
            f"{held} of the {stock} in stock are reserved for the predicted surge."
            if held
            else f"only {stock} in stock."
        )
        st.error(
            f"Blocked by the virtual safety buffer. You asked for {qty}, but only {max(0, stock - held)} "
            f"can be sold now: {reason}"
        )
    elif kind == "occ":
        st.error(
            "Blocked by the optimistic lock. Another order changed this item first, so the version you read is out "
            "of date. Submit again to retry with the latest version."
        )
    elif kind == "missing":
        st.error("That item is not in the catalog. Pick one from the list.")
    elif kind == "invalid":
        st.error(f"The order was not valid: {error_message(result)}")
    elif kind == "down":
        st.error("Cannot reach the backend. Check that the API container is running, then try again.")
    else:
        st.error(f"The order failed: {error_message(result)}")


def _trigger_surge_burst(sku: str) -> Dict[str, Any]:
    """Call the surge bridge while supporting the app's existing count keyword."""
    try:
        return trigger_surge(sku, count=SURGE_SHOPPERS_PER_ROUND)
    except TypeError as exc:
        if "count" not in str(exc):
            raise
        return trigger_surge(sku, bots=SURGE_SHOPPERS_PER_ROUND)


def run_surge(sku: str, name: str, carts_before: Optional[int]) -> None:
    """Send three bursts of simulated shoppers, remember the outcome and redraw so the cart spike is visible."""
    outcomes = []
    bar = st.progress(0, text=f"Sending simulated shoppers to {name}...")
    for done in range(SURGE_ROUNDS):
        outcomes.append(_trigger_surge_burst(sku))
        bar.progress((done + 1) / SURGE_ROUNDS, text=f"Wave {done + 1} of {SURGE_ROUNDS} reached {name}'s cart")
    sent = sum(int_or_zero(o.get("succeeded", o.get("successful", 0))) for o in outcomes)
    if sent:
        log_event(f"Surge: {sent} simulated shoppers sent to {name}", "surge")
    st.session_state["surge_flash"] = {
        "sku": sku,
        "name": name,
        "sent": sent,
        "attempted": sum(int_or_zero(o.get("requested", o.get("attempted"))) for o in outcomes),
        "before": carts_before,
        "error": None if sent else error_message(outcomes[-1]),
    }
    st.rerun()


def show_surge_result(items_by_sku: Dict[str, Dict[str, Any]]) -> None:
    flash = st.session_state.pop("surge_flash", None)
    if not flash:
        return
    if flash["error"]:
        st.error(f"The surge did not start. {flash['error']}")
        return
    after = (items_by_sku.get(flash["sku"]) or {}).get("active_carts_5m")
    st.success(
        f"Sent {flash['sent']} of {flash['attempted']} simulated shoppers to {flash['name']}. "
        f"Active carts went from {fmt(flash['before'])} to {fmt(after)}."
    )


def inventory_view() -> None:
    """Numbers, table and legend. Runs as a fragment so the Live switch can refresh it without touching the forms."""
    inventory = get_inventory()
    items: List[Dict[str, Any]] = inventory.get("items") or []

    if inventory.get("error"):
        st.error(
            "Cannot load inventory. Check that the API container is running and that the database has been "
            f"bootstrapped (`python -m scripts.bootstrap`). Details: {inventory['error']}"
        )
        return
    if not items:
        st.info("No inventory yet. Run `python -m scripts.bootstrap` to add the demo items.")
        return

    physical = sum(int_or_zero(i.get("stock")) for i in items)
    held = sum(min(int_or_zero(i.get("virtual_safety_buffer")), int_or_zero(i.get("stock"))) for i in items)
    available = sum(int_or_zero(i.get("available_stock")) for i in items)
    carts = inventory.get("total_active_carts_5m") if inventory.get("redis_available") else None
    previous = st.session_state.get("carts_previous")
    st.session_state["carts_previous"] = carts

    metrics = st.columns(4)
    metrics[0].metric("Physical stock", fmt(physical))
    metrics[1].metric("Reserved holds", fmt(held))
    metrics[2].metric("Available to sell", fmt(available))
    delta = carts - previous if carts is not None and previous is not None and carts != previous else None
    metrics[3].metric("Active carts (5 min)", fmt(carts), delta=delta, delta_color="off")
    if not inventory.get("redis_available"):
        st.warning(
            "Live cart counts are unavailable because Redis is not answering. Stock and holds are still correct."
        )

    mode = st.segmented_control(
        "View", ["Table", "Chart"], default="Table", key="inventory_mode", help="Switch between numbers and bars."
    )
    if mode == "Chart":
        chart = pd.DataFrame(
            {
                "Available": [int_or_zero(i.get("available_stock")) for i in items],
                "Held back": [
                    min(int_or_zero(i.get("virtual_safety_buffer")), int_or_zero(i.get("stock"))) for i in items
                ],
            },
            index=[str(i.get("name") or i.get("item_id")) for i in items],
        )
        st.bar_chart(chart, color=["#2d8b8b", "#e76f51"], horizontal=True)
    else:
        st.markdown(inventory_table(items), unsafe_allow_html=True)
    st.markdown(
        '<div class="osx-legend"><b class="a">Teal</b> is stock that can be sold. <b class="h">Orange</b> is held '
        "back for the predicted surge.</div>",
        unsafe_allow_html=True,
    )
    eye(
        "Reading the numbers and the table",
        "- **Physical stock**: units on the shelf right now.\n"
        "- **Reserved holds**: units the model is keeping back because it expects a rush in the next 15 minutes.\n"
        "- **Available to sell**: stock minus holds. This is all a shopper can actually buy.\n"
        "- **Active carts**: shoppers who added the item in the last 5 minutes (from Redis). The small arrow "
        "shows the change since the last refresh.\n"
        "- **Version**: a counter that goes up with every sale. It powers the optimistic lock (see Quick checkout).\n"
        "- **Stock split bar**: teal is sellable, orange is held back.",
    )


def inventory_tab() -> None:
    heading(
        "Inventory surge",
        "Inventory surge",
        "Shows what happens to stock when a product suddenly goes viral. The model forecasts demand and reserves "
        "stock ahead of time, so a rush cannot sell more than you really have.",
    )
    st.markdown(
        '<p class="osx-lede">The model predicts demand for the next 15 minutes and reserves stock ahead of it. '
        "Reserved holds are that stock. Checkout can only sell what is left over.</p>",
        unsafe_allow_html=True,
    )
    live_col, tip_col = st.columns([14, 1], vertical_alignment="center")
    live = live_col.toggle("Live updates (refresh every 3 seconds)", key="live_inventory")
    with tip_col:
        eye(
            "Live updates",
            "Turn this on, then press the surge button and watch the cart counts and holds change on their own. "
            "Turn it off to freeze the numbers.",
        )
    st.fragment(run_every="3s" if live else None)(inventory_view)()

    inventory = get_inventory()
    items: List[Dict[str, Any]] = inventory.get("items") or []
    if inventory.get("error") or not items:
        return

    by_sku = {str(i.get("item_id")): i for i in items}
    show_surge_result(by_sku)

    left, right = st.columns(2, gap="large")
    with left:
        heading(
            "Simulate a surge",
            "Simulate a surge",
            "Sends three waves of 15 fake shoppers who each add the chosen item to a cart, like a TikTok video "
            "going viral. Watch Active carts jump and, once Exasol is running, Reserved holds grow.",
            level=4,
        )
        st.caption(f"Sends {SURGE_ROUNDS * SURGE_SHOPPERS_PER_ROUND} simulated shoppers to one item's cart.")
        target = st.selectbox(
            "Item to surge", list(by_sku), format_func=lambda s: sku_label(by_sku[s]), key="surge_item"
        )
        if st.button("🚨 Simulate TikTok Surge", key="surge"):
            run_surge(target, str(by_sku[target].get("name") or target), by_sku[target].get("active_carts_5m"))

    with right:
        heading(
            "Quick checkout",
            "Quick checkout",
            "Tries to buy an item and shows which safeguard answered.\n\n"
            "- **Virtual safety buffer**: refuses to sell stock the model has reserved for the surge.\n"
            "- **Optimistic lock**: every item has a version number. If two buyers read the same version, only the "
            "first sale wins and the second is told to retry. Tick the box to fake an out-of-date version and see it.\n"
            "- **Stock check**: never sells more than exists.",
            level=4,
        )
        st.caption("Try to buy an item and see which safeguard answers.")
        with st.form("quick_checkout"):
            sku = st.selectbox("Item", list(by_sku), format_func=lambda s: sku_label(by_sku[s]), key="checkout_item")
            quantity = st.number_input("Quantity", min_value=1, max_value=10_000, value=1, step=1)
            stale = st.checkbox("Use an out-of-date version (shows the optimistic lock)")
            submitted = st.form_submit_button("Place order")
        if submitted:
            snapshot = dict(by_sku[sku])
            payload: Dict[str, Any] = {
                "item_id": sku,
                "quantity": int(quantity),
                "user_session_id": f"dashboard-{uuid.uuid4().hex[:8]}",
            }
            if stale:
                payload["expected_version"] = max(0, int_or_zero(snapshot.get("version")) - 1)
            result = execute_checkout(payload)
            kind = classify_checkout(result)
            log_event(
                f"Checkout of {int(quantity)} x {sku}: {kind}",
                "blocked" if kind in ("buffer", "occ") else None,
            )
            st.session_state["checkout_flash"] = {
                "result": result,
                "kind": kind,
                "snapshot": snapshot,
                "quantity": int(quantity),
            }
            if kind == "success":
                st.rerun()
        if st.session_state.get("checkout_flash"):
            show_checkout_result(st.session_state["checkout_flash"])


# --------------------------------------------------------------------------------------------------------------
# Tab 2: Identity Detective
# --------------------------------------------------------------------------------------------------------------
DECISION_LABELS = {
    "STRONG_CANDIDATE_PENDING_REVIEW": "Strong candidate, waiting for review",
    "MERGED": "Merged into one customer",
    "REJECTED": "Rejected as different people",
}


def confidence_style(score: int) -> Dict[str, str]:
    """Ring colour and one-line verdict for a 0-100 confidence score."""
    if score >= 85:
        return {"color": "var(--seafoam)", "verdict": "Very likely the same person"}
    if score >= 60:
        return {"color": "var(--sand)", "verdict": "Possibly the same person"}
    return {"color": "var(--burnt)", "verdict": "Probably different people"}


def confidence_ring(score: int) -> str:
    style = confidence_style(score)
    return (
        f'<div class="osx-ring" style="--p:{score};--ring:{style["color"]}" role="img" '
        f'aria-label="Match confidence {score} percent"><div class="osx-ring-inner">'
        f'<div class="osx-ring-num">{score}%</div><div class="osx-ring-label">match confidence</div></div></div>'
    )


def evidence_panel(title: str, lines: List[str], tone: str, empty_text: str) -> str:
    body = "".join(f"<li>{esc(line)}</li>" for line in lines) or f'<li class="none">{esc(empty_text)}</li>'
    heading = f'<div class="osx-evi-title" role="heading" aria-level="4">{esc(title)} ({len(lines)})</div>'
    return f'<div class="osx-evi {tone}">{heading}<ul>{body}</ul></div>'


def _use_example_customer() -> None:
    picked = st.session_state.get("example_customer")
    if picked:
        st.session_state["investigation_customer"] = picked


def identity_tab() -> None:
    heading(
        "Identity detective",
        "Identity detective",
        "Shoppers often browse as anonymous guests on one device and buy as themselves on another. The detective "
        "scores how likely two records are the same person and lists the clues for and against. It never merges "
        "on a hunch: a person reviews strong candidates first.",
    )
    st.markdown(
        '<p class="osx-lede">When separate records might belong to one person, the detective shows how sure it is '
        "and why. Nothing is merged on a hunch: every link lists what supports it and what argues against it.</p>",
        unsafe_allow_html=True,
    )
    st.session_state.setdefault("investigation_customer", DEMO_CUSTOMER_ID)
    st.pills(
        "Try an example",
        [DEMO_CUSTOMER_ID, "UNKNOWN-001"],
        key="example_customer",
        on_change=_use_example_customer,
        help="One click fills the box: the demo shopper, or an ID that does not exist.",
    )
    customer_id = st.text_input("Customer ID", key="investigation_customer").strip()
    if not customer_id:
        st.info("Enter a customer ID to see the investigation.")
        return

    result = get_identity_investigation(customer_id)
    if result.get("error"):
        if "not found" in str(result["error"]).lower():
            st.info(
                f"No investigation exists for {customer_id}. Check the ID, or run `python -m scripts.bootstrap` "
                "for the demo data."
            )
        else:
            st.error(
                f"Cannot load the investigation. Check that the API container is running. Details: {result['error']}"
            )
        return

    score = max(0, min(100, int_or_zero(result.get("confidence_score"))))
    status = str(result.get("decision_status") or "")
    status_text = DECISION_LABELS.get(status) or status.replace("_", " ").capitalize() or "No decision yet"
    ring, summary = st.columns([1, 2], gap="large")
    ring.markdown(confidence_ring(score), unsafe_allow_html=True)
    with ring:
        eye(
            "Match confidence",
            "How sure the detective is that the anonymous activity belongs to this customer.\n\n"
            "- **85% and up**: very likely the same person.\n- **60 to 84%**: possible, needs a closer look.\n"
            "- **Below 60%**: probably different people.\n\nPromo-code leaks alone can never push a match over the line.",
        )
    summary.markdown(
        f'<div class="osx-verdict">{esc(confidence_style(score)["verdict"])}</div>'
        f'<p class="osx-lede">Investigating {esc(customer_id)}.</p>'
        f'<span class="osx-status">{esc(status_text)}</span>',
        unsafe_allow_html=True,
    )

    heading(
        "Why the detective thinks so",
        "Evidence",
        "**Supporting evidence** are clues that link the records (same cart, same promo code, similar timing). "
        "**Counter-evidence** are clues that argue against it (different language, no shared email). The score "
        "weighs both.",
        level=4,
    )
    supporting = [str(line) for line in result.get("supporting_evidence") or []]
    counter = [str(line) for line in result.get("counter_evidence") or []]
    good, bad = st.columns(2, gap="large")
    good.markdown(
        evidence_panel("Supporting evidence", supporting, "good", "No supporting evidence was recorded."),
        unsafe_allow_html=True,
    )
    bad.markdown(
        evidence_panel("Counter-evidence", counter, "warn", "No counter-evidence was found."), unsafe_allow_html=True
    )


# --------------------------------------------------------------------------------------------------------------
# Tab 3: Context Returns
# --------------------------------------------------------------------------------------------------------------
def level_color(level: int) -> str:
    return "var(--seafoam)" if level <= 2 else ("var(--sand)" if level <= 4 else "var(--burnt)")


def pct(value: Any) -> str:
    """A 0-1 rate as a whole percentage; n/a when unknown."""
    return f"{float(value) * 100:.0f}%" if isinstance(value, (int, float)) else "n/a"


def raw_rate_level(rate: float) -> int:
    """The level a plain return rate would give, using the PRD bands (under 10, 25, 40, up to 60 percent, above)."""
    for level, upper in ((1, 0.10), (2, 0.25), (3, 0.40)):
        if rate < upper:
            return level
    return 4 if rate <= 0.60 else 5


def level_scale(level: int) -> str:
    cells = "".join(
        f'<span class="on" style="background:{level_color(n)}">{n}</span>' if n == level else f"<span>{n}</span>"
        for n in range(1, 6)
    )
    return f'<div class="osx-scale" role="img" aria-label="Risk level {level} of 5">{cells}</div>'


def return_verdict(level: int, raw_level: Optional[int]) -> Tuple[str, str]:
    """Badge text and tone: was the shopper forgiven, left alone, slowed down or penalised?"""
    if level <= 2 and raw_level is not None and raw_level >= 4:
        return "Forgiven by context", "good"
    if level >= 5:
        return "Penalty applied", "bad"
    if level >= 3:
        return "Friction applied", "warn"
    return "No penalty", "good"


def return_column(title: str, role: str, intro: str, user_id: str) -> None:
    st.markdown(f'<div class="osx-who">{esc(title)} ({esc(role)})</div>', unsafe_allow_html=True)
    st.caption(intro)
    risk = get_return_risk(user_id)
    if risk.get("error"):  # checked first: the bridge's fallback says "level 1", which would look like a verdict
        st.error(f"Cannot load return risk for {user_id}. Check that the API container is running. {risk['error']}")
        return
    if not risk.get("found"):
        st.info(f"No return history for {user_id}. Run `python -m scripts.bootstrap` for the demo data.")
        return

    level = max(1, min(5, int_or_zero(risk.get("risk_level"))))
    rate = risk.get("return_rate")
    raw_level = raw_rate_level(float(rate)) if isinstance(rate, (int, float)) else None
    verdict, tone = return_verdict(level, raw_level)

    rate_box, level_box = st.columns(2)
    rate_box.metric("Raw return rate", pct(rate), help="Returns divided by purchases over the last 6 months.")
    level_box.metric("Risk level", f"Level {level}", help="The level after the model looks at why items came back.")
    st.markdown(level_scale(level), unsafe_allow_html=True)
    eye(
        "Raw rate vs risk level",
        "The **raw return rate** is simply returns divided by purchases. The **risk level** (1 to 5) is what the "
        "model concludes after asking *why* each item came back. Level 1 gets free returns; level 5 gets a "
        "restocking fee, no cash on delivery and a human review.",
    )
    if raw_level is not None:
        st.markdown(
            f'<div class="osx-compare">By raw rate alone this shopper would be <b>Level {raw_level}</b>. '
            f"With context they are <b>Level {level}</b>.</div>",
            unsafe_allow_html=True,
        )
    st.markdown(f'<span class="osx-badge {tone}">{esc(verdict)}</span>', unsafe_allow_html=True)

    explanation = risk.get("xai_explanation") or "No explanation was recorded for this score."
    st.markdown(
        f'<div class="osx-xai {tone}"><div class="osx-xai-title" role="heading" aria-level="4">Why this level</div>'
        f'<p class="osx-xai-text">{esc(explanation)}</p></div>',
        unsafe_allow_html=True,
    )
    eye(
        "Why this level",
        "The model's own explanation in plain words: how many returns were the shopper's choice and how many were "
        "the store's or supplier's fault. Nothing here is hidden in a black box.",
    )
    if risk.get("human_review_required"):
        st.markdown(
            '<div class="osx-review"><b>Human review required.</b> A person must check this account before any '
            "penalty is enforced.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("No human review needed.")
    if risk.get("staleness_warning"):
        st.warning("This score is more than 24 hours old. It may not include the latest returns.")

    policy = risk.get("policy") or {}
    cod = "available" if policy.get("cash_on_delivery_allowed", True) else "not available"
    with st.expander("What this level means for the shopper"):
        st.markdown(
            f"**{policy.get('name', 'Unknown policy')}**\n\n{policy.get('customer_message', '')}\n\n"
            f"- Restocking fee: ${float(policy.get('restocking_fee_usd') or 0):.2f}\n"
            f"- Cash on delivery: {cod}\n"
            f"- Keep-it discount: {int_or_zero(policy.get('keep_it_discount_pct'))}%"
        )
    with st.expander("The numbers behind it"):
        st.markdown(
            f"- Purchases in the last 6 months: {fmt(risk.get('purchases_6m'))}\n"
            f"- Returns in the last 6 months: {fmt(risk.get('returns_6m'))}\n"
            f"- Score calculated: {risk.get('computed_at') or 'not recorded'}\n"
            f"- Customer record: {risk.get('golden_customer_id') or 'not linked yet'}"
        )


def returns_tab() -> None:
    heading(
        "Context-aware returns",
        "Context-aware returns",
        "Compares two shoppers with similarly high return rates. One was unlucky (a defective supplier batch), the "
        "other abuses the policy (wearing items, then returning them). The model treats them differently and shows "
        "why. Type another shopper's ID below to check them too.",
    )
    st.markdown(
        '<p class="osx-lede">A plain return rate treats every return the same. This model also asks why the item '
        "came back, so a run of defective-batch returns does not punish the shopper, while repeated wardrobing "
        "does.</p>",
        unsafe_allow_html=True,
    )
    other = st.text_input(
        "Check another shopper (optional)",
        key="return_lookup",
        placeholder="Type a shopper ID, for example Customer A",
        help="Adds a third column for that shopper.",
    ).strip()
    people = list(RETURN_DEMO_CUSTOMERS)
    if other and other not in {p[0] for p in people}:
        people.append((other, "Your lookup", "Looked up on request."))
    columns = st.columns(len(people), gap="large")
    for column, (user_id, role, intro) in zip(columns, people):
        with column:
            return_column(user_id, role, intro, user_id)
    with st.expander("How the model weighs a return"):
        st.markdown(
            "- Defective batch, store error, wrong item shipped: counts as **0**. The shopper is not at fault.\n"
            "- Changed mind, wrong size: counts as **1**.\n"
            "- Suspected wardrobing or abuse: counts as **3**.\n\n"
            "When half or more of a shopper's returns (or at least three) were the supplier's fault, they are "
            "not penalised, however high the raw rate looks."
        )


# --------------------------------------------------------------------------------------------------------------
# Tab 4: NLP Ticket Radar
# --------------------------------------------------------------------------------------------------------------
URGENCY = {
    "CRITICAL": ("Critical", "var(--burnt)", 3),
    "HIGH": ("High", "var(--burnt)", 2),
    "MEDIUM": ("Medium", "var(--sand)", 1),
    "LOW": ("Low", "var(--seafoam)", 0),
}
STATUS_LABELS = {
    "PENDING_REVIEW": ("Waiting for approval", "pending"),
    "ACTION_EXECUTED": ("Action executed", "done"),
    "DISPATCHED": ("Dispatched", "done"),
}
ACTION_LABELS = {
    "AUTO_RETURN_LABEL": "Send a prepaid return label automatically",
    "ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT": (
        "Escalate to a human concierge and ship an overnight replacement"
    ),
}


def humanize(code: Any) -> str:
    """ESCALATE_TO_HUMAN or 'ACTION TRIGGERED: Concierge Replacement' as a readable sentence."""
    text = str(code or "").replace("ACTION TRIGGERED:", "").replace("_", " ").strip()
    return (text[:1].upper() + text[1:].lower()) if text else "Not specified"


def sentiment_label(score: float) -> str:
    """The API sends a churn score, not a sentiment. The score is the model's probability of negative sentiment."""
    if score >= 0.85:
        return "Very negative"
    if score >= 0.60:
        return "Negative"
    if score >= 0.40:
        return "Mixed"
    return "Mostly neutral"


def ago(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "time unknown"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - stamp).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def ticket_card(alert: Dict[str, Any]) -> str:
    score = float(alert.get("churn_score") or 0)
    urgency_label, tone, _ = URGENCY.get(str(alert.get("urgency")), ("Not rated", "var(--teal)", -1))
    status_text, status_kind = STATUS_LABELS.get(str(alert.get("status")), (humanize(alert.get("status")), ""))
    who = alert.get("customer_email") or alert.get("golden_customer_id") or "Unknown customer"
    action = ACTION_LABELS.get(str(alert.get("action_triggered"))) or humanize(alert.get("action_triggered"))
    explanation = alert.get("xai_explanation") or "No explanation was recorded for this recommendation."
    return (
        f'<div class="osx-ticket" style="--tone:{tone}">'
        f'<div class="osx-ticket-head"><span class="osx-ticket-id">{esc(alert.get("ticket_id"))}</span>'
        f'<span class="osx-ticket-who">{esc(who)}, {esc(alert.get("ltv_tier") or "no tier")}, '
        f'{esc(ago(alert.get("created_at")))}'
        f'<span class="osx-chip {status_kind}">{esc(status_text)}</span></span></div>'
        '<div class="osx-facts">'
        f'<div><div class="osx-fact-label">Sentiment</div>'
        f'<div class="osx-fact-value">{esc(sentiment_label(score))}</div>'
        '<div class="osx-fact-note">from the churn score</div></div>'
        f'<div><div class="osx-fact-label">Urgency</div><div class="osx-fact-value" style="color:{tone}">'
        f"{esc(urgency_label)}</div></div>"
        f'<div><div class="osx-fact-label">Root cause</div><div class="osx-fact-value">'
        f'{esc(humanize(alert.get("root_cause")))}</div></div>'
        f'<div><div class="osx-fact-label">Churn score</div><div class="osx-fact-value">{score:.2f}</div>'
        f'<div class="osx-meter"><span style="width:{min(100, score * 100):.0f}%;background:{tone}"></span></div></div>'
        "</div>"
        f'<div class="osx-action"><div class="osx-action-label">Recommended action</div>'
        f'<div class="osx-action-value">{esc(action)}</div>'
        f'<div class="osx-xai"><div class="osx-xai-title" role="heading" aria-level="4">Why the AI chose this</div>'
        f'<p class="osx-xai-text">{esc(explanation)}</p></div></div></div>'
    )


def show_approval_result() -> None:
    """One-time banner for the last approval. A repeat approval proves the action is idempotent."""
    flash = st.session_state.pop("approval_flash", None)
    if not flash:
        return
    result, ticket = flash["result"], flash["ticket"]
    if result.get("success") is False or result.get("error"):
        st.error(f"Could not approve {ticket}. {error_message(result)}")
    elif "already executed" in str(result.get("message") or "").lower():
        st.success(f"{ticket} was already executed, so nothing ran a second time. Approving again is safe.")
        st.toast("Already executed. No duplicate action was sent.", icon="🛡️")
    else:
        st.success(f"Action for {ticket} approved and executed by {flash['user']}.")
        st.toast("Action executed.", icon="✅")


def radar_tab() -> None:
    heading(
        "Ticket radar",
        "Ticket radar",
        "An AI model reads customer-support tickets and scores how likely a top customer is to leave (the churn "
        "score). For risky ones it recommends an action and explains why. **Nothing runs until a person presses "
        "Approve.**",
    )
    st.markdown(
        '<p class="osx-lede">The radar reads support tickets from top customers and shows what it found, what it '
        "recommends and why. The model recommends; a person approves before anything runs.</p>",
        unsafe_allow_html=True,
    )
    response = get_churn_alerts()
    if response.get("error"):
        st.error(
            "Cannot load alerts. Check that the API container is running and that the database has been "
            f"bootstrapped (`python -m scripts.bootstrap`). Details: {response['error']}"
        )
        return
    alerts: List[Dict[str, Any]] = response.get("alerts") or []
    if not alerts:
        st.info("No tickets need attention. New alerts appear here when a top customer's ticket looks at risk.")
        return

    waiting = sum(1 for a in alerts if a.get("status") == "PENDING_REVIEW")
    executed = sum(1 for a in alerts if a.get("status") == "ACTION_EXECUTED")
    critical = sum(1 for a in alerts if a.get("urgency") == "CRITICAL")
    metrics = st.columns(4)
    metrics[0].metric("Waiting for approval", fmt(waiting))
    metrics[1].metric("Executed", fmt(executed))
    metrics[2].metric("Critical urgency", fmt(critical))
    metrics[3].metric(f"Alerts in the last {response.get('since_hours', 24)} h", fmt(response.get("count_in_window")))

    eye(
        "The four numbers",
        "- **Waiting for approval**: recommendations nobody has approved yet.\n"
        "- **Executed**: actions already carried out.\n- **Critical urgency**: tickets the model rates most urgent.\n"
        "- **Alerts in the last N hours**: everything the radar raised recently.",
    )
    approver_col, filter_col = st.columns(2, gap="large")
    approver = approver_col.text_input("Approving as", value="supervisor", key="approver").strip() or "supervisor"
    show_filter = filter_col.pills(
        "Show",
        ["All", "Waiting", "Executed", "Critical"],
        default="All",
        key="radar_filter",
        help="Narrow the list of tickets below.",
    )
    search_col, sort_col = st.columns(2, gap="large")
    query = (
        search_col.text_input("Search tickets", key="radar_search", placeholder="Ticket ID or customer email")
        .strip()
        .lower()
    )
    order = sort_col.selectbox("Sort by", ["Needs attention first", "Highest churn score", "Newest"], key="radar_sort")
    show_approval_result()

    def triage(alert: Dict[str, Any]):
        if order == "Highest churn score":
            return (-float(alert.get("churn_score") or 0),)
        if order == "Newest":
            try:
                stamp = datetime.fromisoformat(str(alert.get("created_at")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                stamp = 0.0
            return (-stamp,)
        rank = URGENCY.get(str(alert.get("urgency")), ("", "", -1))[2]
        return (alert.get("status") == "ACTION_EXECUTED", -rank, -float(alert.get("churn_score") or 0))

    def visible(alert: Dict[str, Any]) -> bool:
        if query and query not in f"{alert.get('ticket_id')} {alert.get('customer_email')}".lower():
            return False
        if show_filter == "Waiting":
            return alert.get("status") == "PENDING_REVIEW"
        if show_filter == "Executed":
            return alert.get("status") == "ACTION_EXECUTED"
        if show_filter == "Critical":
            return alert.get("urgency") == "CRITICAL"
        return True

    shown = [a for a in alerts if visible(a)]
    if not shown:
        st.info("No tickets match this filter.")
    for alert in sorted(shown, key=triage):
        ticket_id = str(alert.get("ticket_id"))
        st.markdown(ticket_card(alert), unsafe_allow_html=True)
        button_col, tip_col = st.columns([8, 1], vertical_alignment="center")
        if button_col.button("Approve & Execute Action", key=f"approve_{ticket_id}"):
            result = approve_churn_action(ticket_id, approver)
            if not (result.get("success") is False or result.get("error")):
                log_event(f"{approver} approved {ticket_id}", "approve")
            st.session_state["approval_flash"] = {"ticket": ticket_id, "result": result, "user": approver}
            st.rerun()
        with tip_col:
            eye(
                "Approve & Execute Action",
                "Carries out the AI's recommended action for this ticket and records who approved it. It is safe "
                "to press twice: the system remembers the first approval and does nothing the second time.",
            )
        if alert.get("status") == "ACTION_EXECUTED":
            st.markdown(
                '<div class="osx-gov">Already executed. Pressing the button again is safe: nothing runs twice.</div>',
                unsafe_allow_html=True,
            )
        with st.expander("Audit trail"):
            st.markdown(
                f"- Customer record: {alert.get('golden_customer_id')}\n"
                f"- Created: {alert.get('created_at')}\n"
                f"- Status: {alert.get('status')}\n"
                f"- Action code: {alert.get('action_triggered')}\n"
                f"- Approved by: {alert.get('approved_by') or 'nobody yet'}\n"
                f"- Executed at: {alert.get('executed_at') or 'not executed'}"
            )


# --------------------------------------------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------------------------------------------
def ticker_html() -> str:
    """A scrolling strip of live facts: stock and carts per item, tickets waiting, services that are down."""
    ticks: List[str] = []
    for item in get_inventory().get("items") or []:
        held = int_or_zero(item.get("virtual_safety_buffer"))
        tone = "bad" if held else ""
        ticks.append(
            f'<span class="osx-tick {tone}">{esc(item.get("name") or item.get("item_id"))}'
            f"<b>{fmt(item.get('available_stock'))} left, {fmt(item.get('active_carts_5m'))} carts</b></span>"
        )
    alerts = get_churn_alerts().get("alerts") or []
    waiting = sum(1 for a in alerts if a.get("status") == "PENDING_REVIEW")
    ticks.append(f'<span class="osx-tick {"warn" if waiting else ""}">Tickets waiting<b>{fmt(waiting)}</b></span>')
    services = get_system_health().get("services") or {}
    down = [label for key, label in SERVICES if (services.get(key) or {}).get("status") != "UP"]
    ticks.append(
        f'<span class="osx-tick {"bad" if down else ""}">Services down<b>{esc(", ".join(down) if down else "none")}</b></span>'
    )
    track = "".join(ticks)
    return f'<div class="osx-ticker"><div class="osx-ticker-track">{track}{track}{track}{track}</div></div>'


HOW_IT_WORKS = (
    ("1", "Protect stock", "Forecasts a rush and holds stock back, so checkout never oversells."),
    ("2", "Know the shopper", "Matches anonymous visits to one customer and shows every clue."),
    ("3", "Judge returns fairly", "Asks why items came back before penalising anyone."),
    ("4", "Save top customers", "Reads support tickets and recommends a fix a person approves."),
)


def hero() -> None:
    """Landing block: headline, calls to action, data-source note and the four-step explainer."""
    st.markdown(ticker_html(), unsafe_allow_html=True)
    health = get_system_health()
    down = [label for key, label in SERVICES if ((health.get("services") or {}).get(key) or {}).get("status") != "UP"]
    if health.get("error"):
        note, tone = "The backend is not answering, so the numbers below may be empty.", "warn"
    elif down:
        note = (
            f"Live data from the API. {', '.join(down)} is offline, so anything it computes (demand holds, "
            "identity scores, return risk, churn scores) shows its last saved value."
        )
        tone = "warn"
    else:
        note, tone = "Live data from the API. Every service is answering.", ""
    steps = "".join(
        f'<div class="osx-step"><div class="osx-step-n">Step {n}</div><div class="osx-step-t">{esc(t)}</div>'
        f'<div class="osx-step-d">{esc(d)}</div></div>'
        for n, t, d in HOW_IT_WORKS
    )
    st.markdown(
        '<div class="osx-eyebrow">EXPLAINABLE RETAIL OPERATIONS · LIVE FROM POSTGRESQL, REDIS AND EXASOL</div>'
        '<div class="osx-hero-title">Which shoppers and stock need <em>your attention</em> right now?</div>'
        '<div class="osx-hero-sub">OmniSync watches stock, identities, returns and support tickets, scores each '
        "one, and tells you why. Click any section to see exactly how it decided.</div>"
        '<div class="osx-cta-row"><a class="osx-cta primary" href="#osx-workspace">Open the live demo</a>'
        '<a class="osx-cta ghost" href="#osx-how">How does it work?</a></div>'
        f'<div class="osx-banner {tone}">{esc(note)}</div>'
        f'<div id="osx-how"></div><div class="osx-steps">{steps}</div><div id="osx-workspace"></div>',
        unsafe_allow_html=True,
    )


hero()

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


# --------------------------------------------------------------------------------------------------------------
# Sidebar, second half: demo guide and activity log. It is drawn last so it already knows what happened this run.
# --------------------------------------------------------------------------------------------------------------
with st.sidebar:
    st.markdown('<div class="osx-section">Demo guide</div>', unsafe_allow_html=True)
    finished = sum(1 for key, _ in GUIDE_STEPS if st.session_state.get(f"guide_{key}"))
    st.progress(finished / len(GUIDE_STEPS), text=f"{finished} of {len(GUIDE_STEPS)} steps done")
    for key, label in GUIDE_STEPS:
        st.checkbox(label, key=f"guide_{key}")
    with st.expander("Activity log"):
        events = st.session_state.get("activity_log") or []
        if events:
            st.markdown("\n".join(f"- `{line}`" for line in reversed(events[-10:])))
        else:
            st.caption("Nothing yet. Actions you take on this page are listed here.")
