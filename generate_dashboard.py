#!/usr/bin/env python3
"""
Generates dashboard.html — a self-contained interactive web dashboard
that reads from tsm_data.json and embeds all data as inline JSON.

No server, no external dependencies. Open the file directly in any browser.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Resolve paths relative to this file so the script works from any CWD
SCRIPT_DIR   = Path(__file__).parent
DATA_FILE    = Path.home() / "tsm_data.json"
NAMES_FILE   = Path.home() / "item_names.json"
DASHBOARD    = SCRIPT_DIR / "dashboard.html"

PRIMARY_REALM    = "Malfurion"
AH_CUT           = 0.05
MIN_SPREAD_PCT   = 20.0

# ---------------------------------------------------------------------------
# Analysis helpers (self-contained — no imports from agent/arbitrage)
# ---------------------------------------------------------------------------

def load_item_names() -> dict[str, str]:
    if NAMES_FILE.exists():
        try:
            return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def item_name(iid: int, cache: dict) -> str:
    return cache.get(str(iid), f"Item {iid}")


def build_profit_stats(records: list[dict]) -> list[dict]:
    buys  = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Buys"  and r.get("source") == "Auction"]
    sales = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Sales" and r.get("source") == "Auction"]

    buy_acc  = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})
    sell_acc = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})

    for r in buys:
        d = buy_acc[r["item_id"]]
        d["gold"] += r["price_gold"]; d["qty"] += r["quantity"]; d["txns"] += 1

    for r in sales:
        d = sell_acc[r["item_id"]]
        d["gold"] += r["price_gold"]; d["qty"] += r["quantity"]; d["txns"] += 1

    out = []
    for iid in set(buy_acc) & set(sell_acc):
        b, s = buy_acc[iid], sell_acc[iid]
        avg_buy  = b["gold"] / b["qty"]
        avg_sell = s["gold"] / s["qty"]
        profit   = avg_sell - avg_buy
        margin   = (profit / avg_buy * 100) if avg_buy else 0.0
        out.append({
            "item_id": iid, "avg_buy": avg_buy, "avg_sell": avg_sell,
            "profit_per_item": profit, "margin_pct": margin,
            "buy_txns": b["txns"], "sell_txns": s["txns"],
            "total_volume": b["txns"] + s["txns"],
        })

    out.sort(key=lambda x: x["profit_per_item"], reverse=True)
    return out


def build_arbitrage(records: list[dict]) -> list[dict]:
    raw: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in records:
        if r.get("source") != "Auction": continue
        dtype = r.get("type")
        if dtype not in ("Sales", "Buys"): continue
        iid  = r.get("item_id")
        qty  = r.get("quantity") or 1
        gold = r.get("price_gold")
        if not iid or not gold: continue
        raw[iid][r["realm"]][dtype].append((gold / qty, qty))

    realm_prices = {}
    for iid, realms in raw.items():
        realm_prices[iid] = {}
        for realm, sources in realms.items():
            chosen, label = (sources["Sales"], "Sales") if "Sales" in sources \
                            else (sources["Buys"], "Buys")
            total_qty = sum(q for _, q in chosen)
            realm_prices[iid][realm] = {
                "avg_price": sum(p*q for p,q in chosen) / total_qty,
                "txns": len(chosen), "source": label,
            }

    opps = []
    for iid, realms in realm_prices.items():
        if len(realms) < 2: continue
        best = None
        for buy_realm, bd in realms.items():
            for sell_realm, sd in realms.items():
                if buy_realm == sell_realm: continue
                sell_net   = sd["avg_price"] * (1 - AH_CUT)
                profit     = sell_net - bd["avg_price"]
                spread_pct = (profit / bd["avg_price"] * 100) if bd["avg_price"] else 0
                if spread_pct < MIN_SPREAD_PCT: continue
                if best is None or profit > best["profit_per_item"]:
                    best = {
                        "item_id": iid,
                        "buy_realm": buy_realm, "buy_price": bd["avg_price"],
                        "buy_txns": bd["txns"],  "buy_source": bd["source"],
                        "sell_realm": sell_realm, "sell_net": sell_net,
                        "sell_gross": sd["avg_price"],
                        "sell_txns": sd["txns"],  "sell_source": sd["source"],
                        "profit_per_item": profit, "spread_pct": spread_pct,
                        "total_txns": bd["txns"] + sd["txns"],
                    }
        if best: opps.append(best)

    opps.sort(key=lambda x: x["profit_per_item"], reverse=True)
    return opps


def build_repricing(records: list[dict]) -> list[dict]:
    ce: dict = defaultdict(lambda: {"cancels": 0, "expirations": 0, "cq": 0, "eq": 0})
    for r in records:
        if r.get("realm") != PRIMARY_REALM: continue
        if r.get("type") == "Cancelled":
            ce[r["item_id"]]["cancels"]    += 1
            ce[r["item_id"]]["cq"]         += r.get("quantity", 0)
        elif r.get("type") == "Expired":
            ce[r["item_id"]]["expirations"] += 1
            ce[r["item_id"]]["eq"]          += r.get("quantity", 0)

    sells  = defaultdict(int)
    bprices = defaultdict(list)
    for r in records:
        if r.get("realm") != PRIMARY_REALM: continue
        if r.get("type") == "Sales"  and r.get("source") == "Auction":
            sells[r["item_id"]] += 1
        if r.get("type") == "Buys"   and r.get("source") == "Auction":
            qty  = r.get("quantity") or 1
            bprices[r["item_id"]].append(r["price_gold"] / qty)

    out = []
    for iid, d in ce.items():
        failures = d["cancels"] + d["expirations"]
        successes = sells.get(iid, 0)
        total     = failures + successes
        fail_rate = (failures / total * 100) if total else 100.0
        bp        = bprices.get(iid)
        out.append({
            "item_id": iid,
            "cancels": d["cancels"], "expirations": d["expirations"],
            "failed_qty": d["cq"] + d["eq"],
            "sell_successes": successes, "total_listings": total,
            "failure_rate": fail_rate,
            "avg_buy_price": (sum(bp) / len(bp)) if bp else None,
        })

    out.sort(key=lambda x: (x["cancels"] + x["expirations"], x["failure_rate"]), reverse=True)
    return out


PROFESSION_TOOL_KEYWORDS = {
    "knife", "needle", "hammer", "chisel", "awl", "apron", "gloves", "hat",
    "goggles", "wrench", "focuser", "backpack", "chapeau", "bifocals",
    "rolling pin", "satchel", "cover", "visor", "multitool", "snippers",
    "clampers", "cutters", "toolset", "screwdriver", "wrench", "tongs",
    "cap", "loupes", "quill", "rod", "shears", "pick", "pickaxe", "sickle",
}


def _is_profession_item(item_name: str) -> bool:
    """Return True if the item name contains any profession tool keyword."""
    lower = item_name.lower()
    return any(kw in lower for kw in PROFESSION_TOOL_KEYWORDS)


def build_stop_buying(profit_stats: list[dict], names: dict) -> list[dict]:
    """
    Items with negative margin — losing gold on every transaction.
    Profession tools and accessories are filtered out; these are typically
    one-off crafting purchases, not repeatable commodity trades.
    """
    stop = []
    for s in profit_stats:
        if s["profit_per_item"] >= 0:
            continue
        name = names.get(str(s["item_id"]), f"Item {s['item_id']}")
        if _is_profession_item(name):
            continue
        loss_per_item   = abs(s["profit_per_item"])
        total_gold_lost = loss_per_item * s["buy_txns"]
        stop.append({
            **s,
            "loss_per_item":   loss_per_item,
            "loss_pct":        abs(s["margin_pct"]),
            "total_gold_lost": total_gold_lost,
        })
    stop.sort(key=lambda x: x["total_gold_lost"], reverse=True)
    return stop


# ---------------------------------------------------------------------------
# Build dashboard data payload
# ---------------------------------------------------------------------------

def build_data() -> dict:
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    records = raw["records"]
    names   = load_item_names()

    profit_stats      = build_profit_stats(records)
    arbitrage         = build_arbitrage(records)
    repricing         = build_repricing(records)
    stop_buying_stats = build_stop_buying(profit_stats, names)

    enrich = lambda s: {**s, "item_name": item_name(s["item_id"], names)}

    profit_rows      = [enrich(s) for s in profit_stats]
    arb_rows         = [enrich(o) for o in arbitrage]
    reprice_rows     = [enrich(r) for r in repricing]
    stop_buying_rows = [enrich(s) for s in stop_buying_stats]

    profitable  = [s for s in profit_rows if s["profit_per_item"] > 0]
    best_flip   = profit_rows[0]      if profit_rows      else None
    best_arb    = arb_rows[0]         if arb_rows         else None
    worst_sb    = stop_buying_rows[0] if stop_buying_rows else None
    total_pot   = sum(max(0, s["profit_per_item"]) for s in profit_rows)
    total_lost  = sum(s["total_gold_lost"] for s in stop_buying_rows)

    return {
        "meta": {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "realm": PRIMARY_REALM,
            "total_records": len(records),
            "source_file": str(DATA_FILE),
        },
        "summary": {
            "total_profitable":    len(profitable),
            "best_flip_name":      best_flip["item_name"] if best_flip else "—",
            "best_flip_profit":    best_flip["profit_per_item"] if best_flip else 0,
            "best_arb_name":       best_arb["item_name"] if best_arb else "—",
            "best_arb_profit":     best_arb["profit_per_item"] if best_arb else 0,
            "total_potential":     total_pot,
            "arb_count":           len(arb_rows),
            "reprice_count":       len(reprice_rows),
            "stop_buying_count":   len(stop_buying_rows),
            "total_gold_lost":     total_lost,
            "worst_sb_name":       worst_sb["item_name"] if worst_sb else "—",
            "worst_sb_loss":       worst_sb["total_gold_lost"] if worst_sb else 0,
        },
        "profit":      profit_rows,
        "arbitrage":   arb_rows,
        "repricing":   reprice_rows,
        "stop_buying": stop_buying_rows,
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TSM Auction Dashboard</title>
<style>
:root {
  --bg:        #0d1117;
  --surface:   #161b22;
  --surface2:  #21262d;
  --border:    #30363d;
  --text:      #e6edf3;
  --muted:     #8b949e;
  --gold:      #f6c90e;
  --gold-dim:  #9a7d0a;
  --green:     #3fb950;
  --green-bg:  rgba(63,185,80,.12);
  --yellow:    #d29922;
  --yellow-bg: rgba(210,153,34,.12);
  --red:       #f85149;
  --red-bg:    rgba(248,81,73,.12);
  --blue:      #58a6ff;
  --radius:    8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 14px/1.5 'Segoe UI', system-ui, sans-serif; }

/* ---- Header ---- */
.header {
  background: linear-gradient(135deg, #1a1200 0%, #161b22 60%);
  border-bottom: 1px solid var(--gold-dim);
  padding: 18px 28px;
  display: flex; align-items: center; justify-content: space-between;
}
.header h1 { font-size: 1.4rem; color: var(--gold); letter-spacing: .04em; }
.header h1 span { color: var(--muted); font-weight: 400; font-size: 1rem; margin-left: 10px; }
.header .meta { text-align: right; color: var(--muted); font-size: .8rem; line-height: 1.8; }
.header .meta strong { color: var(--text); }

/* ---- Summary cards ---- */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px; padding: 20px 28px;
  border-bottom: 1px solid var(--border);
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
}
.card .label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.card .value { font-size: 1.3rem; font-weight: 700; color: var(--gold); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card .sub   { font-size: .78rem; color: var(--muted); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ---- Tabs ---- */
.tabs { display: flex; gap: 0; padding: 0 28px; border-bottom: 1px solid var(--border); background: var(--surface); }
.tab {
  padding: 12px 22px; cursor: pointer; border-bottom: 2px solid transparent;
  color: var(--muted); font-weight: 500; transition: color .15s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--gold); border-bottom-color: var(--gold); }

/* ---- Table container ---- */
.panel { display: none; padding: 24px 28px; }
.panel.active { display: block; }

.table-wrap { overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: .83rem; }
thead th {
  background: var(--surface2);
  padding: 10px 14px; text-align: left;
  color: var(--muted); font-weight: 600;
  white-space: nowrap; cursor: pointer;
  user-select: none; position: sticky; top: 0;
  border-bottom: 1px solid var(--border);
}
thead th:hover { color: var(--text); }
thead th.sorted { color: var(--gold); }
thead th .sort-icon { margin-left: 4px; opacity: .7; }

tbody tr { border-bottom: 1px solid var(--border); transition: background .1s; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: var(--surface2) !important; }
tbody td { padding: 9px 14px; vertical-align: middle; white-space: nowrap; }

/* Row colour bands */
.row-green  { background: var(--green-bg); }
.row-yellow { background: var(--yellow-bg); }
.row-red    { background: var(--red-bg); }

/* Value colours */
.pos    { color: var(--green); font-weight: 600; }
.neg    { color: var(--red);   font-weight: 600; }
.warn   { color: var(--yellow);}
.hi-pct { color: var(--gold);  font-weight: 700; }
.muted  { color: var(--muted); }
.realm-tag {
  display: inline-block; padding: 1px 7px;
  border-radius: 4px; font-size: .75rem; font-weight: 600;
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--blue);
}

/* ---- Danger cards (stop-buying) ---- */
.card-danger { border-color: rgba(248,81,73,.4); background: rgba(248,81,73,.06); }
.card-danger .value { color: var(--red); }
.card-danger .label { color: rgba(248,81,73,.8); }

/* ---- Loss value cells ---- */
.loss { color: var(--red); font-weight: 600; }

/* ---- Footer ---- */
.footer { text-align: center; padding: 18px; color: var(--muted); font-size: .75rem; border-top: 1px solid var(--border); }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>⚔ TSM Auction Dashboard <span id="realm-name"></span></h1>
  </div>
  <div class="meta">
    <div>Last updated: <strong id="last-updated"></strong></div>
    <div>Source records: <strong id="total-records"></strong></div>
  </div>
</div>

<div class="cards" id="cards"></div>

<div class="tabs">
  <div class="tab active" data-panel="profit">Profit Opportunities</div>
  <div class="tab" data-panel="arbitrage">Cross-Realm Arbitrage</div>
  <div class="tab" data-panel="repricing">Repricing Concerns</div>
  <div class="tab" data-panel="stop-buying" style="color:var(--red)">⛔ Stop Buying</div>
</div>

<div id="profit" class="panel active"></div>
<div id="arbitrage" class="panel"></div>
<div id="repricing" class="panel"></div>
<div id="stop-buying" class="panel"></div>

<div class="footer">Generated from <span id="source-file"></span></div>

<script>
const DATA = __DATA_JSON__;

// ---- Utilities ----
const g = id => document.getElementById(id);
const fmt_g  = v => v == null ? '—' : (Math.abs(v) < 10 ? v.toFixed(2) : Math.round(v).toLocaleString()) + 'g';
const fmt_pct = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%';

function rowClass(val, type) {
  if (type === 'margin') {
    if (val >= 50)  return 'row-green';
    if (val >= 20)  return 'row-yellow';
    if (val < 0)    return 'row-red';
    return '';
  }
  if (type === 'fail') {
    if (val >= 80)  return 'row-red';
    if (val >= 50)  return 'row-yellow';
    return '';
  }
  if (type === 'spread') {
    if (val >= 100) return 'row-green';
    if (val >= 20)  return 'row-yellow';
    return '';
  }
  return '';
}

function cellClass(val, type) {
  if (type === 'profit') return val > 0 ? 'pos' : val < 0 ? 'neg' : '';
  if (type === 'margin') {
    if (val >= 50) return 'hi-pct';
    if (val >= 20) return 'pos';
    if (val < 0)   return 'neg';
    return 'muted';
  }
  if (type === 'fail') {
    if (val >= 80) return 'neg';
    if (val >= 50) return 'warn';
    return '';
  }
  return '';
}

// ---- Sortable table factory ----
function makeTable(containerId, columns, rows, colorKey, colorType) {
  const container = g(containerId);
  let sortCol = 0, sortAsc = false;

  function render() {
    const sorted = [...rows].sort((a, b) => {
      const col = columns[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      const cmp = col.num
        ? (va - vb)
        : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });

    const thead = columns.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>') : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      return `<th${cls} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    const tbody = sorted.map(row => {
      const colorVal = row[colorKey];
      const rc = rowClass(colorVal, colorType);
      const cells = columns.map(c => {
        let raw = row[c.key];
        let display = raw;
        let cc = '';
        if (c.format === 'gold')   { display = fmt_g(raw);   cc = cellClass(raw, 'profit'); }
        if (c.format === 'pct')    { display = fmt_pct(raw);  cc = cellClass(raw, c.cctype || 'margin'); }
        if (c.format === 'int')    { display = raw ?? '—'; }
        if (c.format === 'realm')  { display = raw ? `<span class="realm-tag">${raw}</span>` : '—'; }
        if (c.format === 'src')    { display = raw ? `<span class="muted">${raw}</span>` : ''; }
        if (c.format === 'fail')   { display = fmt_pct(raw);  cc = cellClass(raw, 'fail'); }
        if (c.format === 'opt_g')  { display = raw != null ? fmt_g(raw) : '<span class="muted">—</span>'; }
        return `<td${cc ? ` class="${cc}"` : ''}>${display}</td>`;
      }).join('');
      return `<tr class="${rc}">${cells}</tr>`;
    }).join('');

    container.innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead>
      <tbody>${tbody}</tbody>
    </table></div>`;

    container.querySelectorAll('thead th').forEach(th => {
      th.addEventListener('click', () => {
        const ci = +th.dataset.col;
        if (ci === sortCol) sortAsc = !sortAsc;
        else { sortCol = ci; sortAsc = false; }
        render();
      });
    });
  }
  render();
}

// ---- Populate header ----
function initMeta() {
  const m = DATA.meta;
  g('realm-name').textContent = m.realm;
  const dt = new Date(m.generated_at);
  g('last-updated').textContent = dt.toLocaleString();
  g('total-records').textContent = m.total_records.toLocaleString();
  g('source-file').textContent = m.source_file;
}

// ---- Summary cards ----
function initCards() {
  const s = DATA.summary;
  const cards = [
    { label: 'Profitable Items',    value: s.total_profitable,     sub: `on ${DATA.meta.realm}`,      danger: false },
    { label: 'Best Single Flip',    value: fmt_g(s.best_flip_profit), sub: s.best_flip_name,          danger: false },
    { label: 'Best Arbitrage',      value: fmt_g(s.best_arb_profit),  sub: s.best_arb_name,           danger: false },
    { label: 'Total Potential',     value: fmt_g(s.total_potential),  sub: 'if all flips executed',   danger: false },
    { label: 'Arbitrage Opps',      value: s.arb_count,               sub: 'spread > 20% after AH cut', danger: false },
    { label: 'Repricing Concerns',  value: s.reprice_count,           sub: 'cancelled or expired',    danger: false },
    { label: '⛔ Stop Buying Items', value: s.stop_buying_count,       sub: 'losing gold per flip',    danger: true  },
  ];
  g('cards').innerHTML = cards.map(c => {
    const cls = c.danger ? 'card card-danger' : 'card';
    const val = typeof c.value === 'number' && !String(c.value).includes('g')
      ? c.value.toLocaleString() : c.value;
    return `<div class="${cls}">
      <div class="label">${c.label}</div>
      <div class="value">${val}</div>
      <div class="sub">${c.sub}</div>
    </div>`;
  }).join('');
}

// ---- Tabs ----
function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      g(tab.dataset.panel).classList.add('active');
    });
  });
}

// ---- Profit table ----
function initProfit() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false },
    { key: 'avg_buy',         label: 'Avg Buy',       num: true,  format: 'gold' },
    { key: 'avg_sell',        label: 'Avg Sell',      num: true,  format: 'gold' },
    { key: 'profit_per_item', label: 'Profit / Item', num: true,  format: 'gold' },
    { key: 'margin_pct',      label: 'Margin %',      num: true,  format: 'pct' },
    { key: 'buy_txns',        label: 'Buy Txns',      num: true,  format: 'int' },
    { key: 'sell_txns',       label: 'Sell Txns',     num: true,  format: 'int' },
  ];
  makeTable('profit', cols, DATA.profit, 'margin_pct', 'margin');
}

// ---- Arbitrage table ----
function initArbitrage() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false },
    { key: 'buy_realm',       label: 'Buy Realm',     num: false, format: 'realm' },
    { key: 'buy_price',       label: 'Buy Price',     num: true,  format: 'gold' },
    { key: 'buy_source',      label: 'Src',           num: false, format: 'src' },
    { key: 'sell_realm',      label: 'Sell Realm',    num: false, format: 'realm' },
    { key: 'sell_net',        label: 'Sell Net',      num: true,  format: 'gold' },
    { key: 'sell_source',     label: 'Src',           num: false, format: 'src' },
    { key: 'profit_per_item', label: 'Profit / Item', num: true,  format: 'gold' },
    { key: 'spread_pct',      label: 'Spread %',      num: true,  format: 'pct', cctype: 'spread' },
    { key: 'total_txns',      label: 'Txns',          num: true,  format: 'int' },
  ];
  makeTable('arbitrage', cols, DATA.arbitrage, 'spread_pct', 'spread');
}

// ---- Repricing table ----
function initRepricing() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false },
    { key: 'cancels',         label: 'Cancels',       num: true,  format: 'int' },
    { key: 'expirations',     label: 'Expired',       num: true,  format: 'int' },
    { key: 'failed_qty',      label: 'Failed Qty',    num: true,  format: 'int' },
    { key: 'sell_successes',  label: 'Sold OK',       num: true,  format: 'int' },
    { key: 'failure_rate',    label: 'Fail Rate',     num: true,  format: 'fail' },
    { key: 'avg_buy_price',   label: 'Avg Buy',       num: true,  format: 'opt_g' },
  ];
  makeTable('repricing', cols, DATA.repricing, 'failure_rate', 'fail');
}

// ---- Stop Buying table ----
function initStopBuying() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false },
    { key: 'avg_buy',         label: 'Avg Buy',       num: true,  format: 'gold' },
    { key: 'avg_sell',        label: 'Avg Sell',      num: true,  format: 'gold' },
    { key: 'loss_per_item',   label: 'Loss / Item',   num: true,  format: 'loss_g' },
    { key: 'loss_pct',        label: 'Loss %',        num: true,  format: 'loss_pct' },
    { key: 'buy_txns',        label: 'Buy Txns',      num: true,  format: 'int' },
    { key: 'total_gold_lost', label: 'Total Lost',    num: true,  format: 'loss_g' },
  ];
  // Extend makeTable's format handling inline via a wrapper
  const container = g('stop-buying');
  let sortCol = 6, sortAsc = false; // default: worst total lost first

  function render() {
    const rows = DATA.stop_buying;
    const sorted = [...rows].sort((a, b) => {
      const col = cols[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1; if (vb == null) return -1;
      const cmp = col.num ? (va - vb) : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });

    const thead = cols.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol
        ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>')
        : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      return `<th${cls} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    const tbody = sorted.map(row => {
      const cells = cols.map(c => {
        const raw = row[c.key];
        if (c.format === 'loss_g')  return `<td class="loss">${raw != null ? fmt_g(raw) : '—'}</td>`;
        if (c.format === 'loss_pct') return `<td class="loss">-${raw != null ? raw.toFixed(1)+'%' : '—'}</td>`;
        if (c.format === 'gold')    return `<td>${raw != null ? fmt_g(raw) : '—'}</td>`;
        if (c.format === 'int')     return `<td>${raw ?? '—'}</td>`;
        return `<td>${raw ?? '—'}</td>`;
      }).join('');
      return `<tr class="row-red">${cells}</tr>`;
    }).join('');

    const empty = rows.length === 0
      ? '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--green)">✓ No loss-making items — great job!</td></tr>'
      : tbody;

    container.innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead>
      <tbody>${empty}</tbody>
    </table></div>`;

    container.querySelectorAll('thead th').forEach(th => {
      th.addEventListener('click', () => {
        const ci = +th.dataset.col;
        if (ci === sortCol) sortAsc = !sortAsc;
        else { sortCol = ci; sortAsc = false; }
        render();
      });
    });
  }
  render();
}

// ---- Boot ----
initMeta();
initCards();
initTabs();
initProfit();
initArbitrage();
initRepricing();
initStopBuying();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading {DATA_FILE}...")
    data = build_data()

    print(f"  Profit rows:      {len(data['profit'])}")
    print(f"  Stop Buying rows: {len(data['stop_buying'])}")
    print(f"  Arbitrage rows:   {len(data['arbitrage'])}")
    print(f"  Repricing rows:   {len(data['repricing'])}")

    # Embed data — JSON is valid JS; escape </script> to prevent tag injection
    json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    json_str = json_str.replace('</script>', r'<\/script>')

    html = HTML_TEMPLATE.replace('__DATA_JSON__', json_str)
    DASHBOARD.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {DASHBOARD}")


if __name__ == "__main__":
    main()
