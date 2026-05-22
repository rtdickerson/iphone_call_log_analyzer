#!/usr/bin/env python3
"""iPhone call log analyzer — import CSVs and query call history."""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


DB_DEFAULT = Path.home() / ".call_log.db"


# ── database ──────────────────────────────────────────────────────────────────

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id        INTEGER PRIMARY KEY,
            call_type TEXT,
            date      TEXT,
            duration  TEXT,
            contact   TEXT,
            location  TEXT,
            service   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date    ON calls(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contact ON calls(contact)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type    ON calls(call_type)")
    conn.commit()


# ── import ────────────────────────────────────────────────────────────────────

def cmd_import(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    expected = {"Call type", "Date", "Duration", "Contact", "Location", "Service"}
    inserted = skipped = 0

    for csv_path in args.file:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = expected - set(reader.fieldnames or [])
            if missing:
                print(f"ERROR {csv_path}: missing columns {missing}", file=sys.stderr)
                continue

            rows = [
                (
                    row["Call type"].strip(),
                    row["Date"].strip(),
                    row["Duration"].strip(),
                    row["Contact"].strip(),
                    row["Location"].strip(),
                    row["Service"].strip(),
                )
                for row in reader
            ]

        if args.replace:
            conn.executemany("""
                INSERT OR REPLACE INTO calls
                    (call_type, date, duration, contact, location, service)
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)
            inserted += len(rows)
        else:
            for row in rows:
                try:
                    conn.execute("""
                        INSERT INTO calls
                            (call_type, date, duration, contact, location, service)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, row)
                    inserted += 1
                except sqlite3.IntegrityError:
                    skipped += 1

        conn.commit()
        print(f"Imported {csv_path}: {len(rows)} rows")

    print(f"Total inserted: {inserted}  skipped: {skipped}")
    conn.close()


# ── stats ─────────────────────────────────────────────────────────────────────

def _duration_to_seconds(duration: str) -> int:
    """Parse 'H:MM:SS' or 'M:SS' or plain seconds to int."""
    parts = duration.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def cmd_stats(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    if not total:
        print("No records in database.")
        conn.close()
        return

    print(f"\n{'─'*40}")
    print(f"  Total calls : {total:,}")

    rows = conn.execute(
        "SELECT call_type, COUNT(*) AS n FROM calls GROUP BY call_type ORDER BY n DESC"
    ).fetchall()
    print(f"\n  By type:")
    for r in rows:
        print(f"    {r['call_type']:<20} {r['n']:>6,}")

    durations = [_duration_to_seconds(r[0]) for r in
                 conn.execute("SELECT duration FROM calls").fetchall()]
    total_secs = sum(durations)
    nonzero = [d for d in durations if d > 0]
    print(f"\n  Total talk time : {_fmt_duration(total_secs)}")
    if nonzero:
        print(f"  Avg call (>0s)  : {_fmt_duration(int(sum(nonzero)/len(nonzero)))}")
        print(f"  Longest call    : {_fmt_duration(max(nonzero))}")

    first, last = conn.execute("SELECT MIN(date), MAX(date) FROM calls").fetchone()
    print(f"\n  Date range : {first}  →  {last}")
    print(f"{'─'*40}\n")
    conn.close()


# ── contacts ──────────────────────────────────────────────────────────────────

def cmd_contacts(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    query = """
        SELECT contact, COUNT(*) AS calls,
               SUM(CASE WHEN call_type = 'Incoming' THEN 1 ELSE 0 END) AS incoming,
               SUM(CASE WHEN call_type = 'Outgoing' THEN 1 ELSE 0 END) AS outgoing,
               SUM(CASE WHEN call_type = 'Missed'   THEN 1 ELSE 0 END) AS missed
        FROM calls
        GROUP BY contact
        ORDER BY calls DESC
        LIMIT ?
    """
    rows = conn.execute(query, (args.limit,)).fetchall()

    if not rows:
        print("No records found.")
        conn.close()
        return

    print(f"\n{'Contact':<30} {'Calls':>6}  {'In':>5}  {'Out':>5}  {'Miss':>5}")
    print("─" * 60)
    for r in rows:
        print(f"{r['contact']:<30} {r['calls']:>6}  {r['incoming']:>5}  {r['outgoing']:>5}  {r['missed']:>5}")
    print()
    conn.close()


# ── history ───────────────────────────────────────────────────────────────────

def cmd_history(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    clauses, params = [], []

    if args.contact:
        clauses.append("contact LIKE ?")
        params.append(f"%{args.contact}%")
    if args.type:
        clauses.append("LOWER(call_type) = LOWER(?)")
        params.append(args.type)
    if args.since:
        clauses.append("date >= ?")
        params.append(args.since)
    if args.until:
        clauses.append("date <= ?")
        params.append(args.until)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(args.limit)

    rows = conn.execute(
        f"SELECT call_type, date, duration, contact, location, service "
        f"FROM calls {where} ORDER BY date DESC LIMIT ?",
        params
    ).fetchall()

    if not rows:
        print("No records match.")
        conn.close()
        return

    print(f"\n{'Type':<12} {'Date':<22} {'Duration':<10} {'Contact':<25} {'Location':<15} Service")
    print("─" * 100)
    for r in rows:
        print(
            f"{r['call_type']:<12} {r['date']:<22} {r['duration']:<10} "
            f"{r['contact']:<25} {r['location']:<15} {r['service']}"
        )
    print()
    conn.close()


# ── summary ───────────────────────────────────────────────────────────────────

def cmd_summary(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    rows = conn.execute("""
        SELECT
            strftime('%Y-%m', date) AS month,
            COUNT(*) AS calls,
            SUM(CASE WHEN call_type = 'Incoming' THEN 1 ELSE 0 END) AS incoming,
            SUM(CASE WHEN call_type = 'Outgoing' THEN 1 ELSE 0 END) AS outgoing,
            SUM(CASE WHEN call_type = 'Missed'   THEN 1 ELSE 0 END) AS missed
        FROM calls
        GROUP BY month
        ORDER BY month DESC
        LIMIT ?
    """, (args.months,)).fetchall()

    if not rows:
        print("No records found.")
        conn.close()
        return

    print(f"\n{'Month':<10} {'Total':>6}  {'In':>5}  {'Out':>5}  {'Miss':>5}")
    print("─" * 40)
    for r in rows:
        print(f"{r['month']:<10} {r['calls']:>6}  {r['incoming']:>5}  {r['outgoing']:>5}  {r['missed']:>5}")
    print()
    conn.close()


# ── weekly ────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _week_start(dt: datetime) -> datetime:
    """Return the Monday of the week containing dt, at midnight."""
    return (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def cmd_weekly(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    clauses, params = [], []
    if args.contact:
        clauses.append("contact LIKE ?")
        params.append(f"%{args.contact}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(
        f"SELECT call_type, date, duration FROM calls {where} ORDER BY date",
        params,
    ).fetchall()

    if not rows:
        print("No records found.", file=sys.stderr)
        conn.close()
        return

    # Collect all valid (week_start, call_type, seconds) tuples
    entries = []
    for r in rows:
        dt = _parse_date(r["date"])
        if dt is None:
            continue
        secs = _duration_to_seconds(r["duration"])
        entries.append((_week_start(dt), r["call_type"].strip().lower(), secs))

    if not entries:
        print("No parseable dates found.", file=sys.stderr)
        conn.close()
        return

    # Build a dense week grid from first to last week
    first_week = entries[0][0]
    last_week  = entries[-1][0]

    week = first_week
    buckets: dict[datetime, dict] = {}
    while week <= last_week:
        buckets[week] = {"in_calls": 0, "in_secs": 0, "out_calls": 0, "out_secs": 0}
        week += timedelta(weeks=1)

    for ws, ct, secs in entries:
        b = buckets[ws]
        if ct == "incoming":
            b["in_calls"] += 1
            b["in_secs"]  += secs
        elif ct == "outgoing":
            b["out_calls"] += 1
            b["out_secs"]  += secs
        # missed calls have no duration; omit from this report

    writer = csv.writer(sys.stdout)
    writer.writerow([
        "week_start", "week_end",
        "incoming_calls", "incoming_duration",
        "outgoing_calls", "outgoing_duration",
    ])
    for ws in sorted(buckets):
        b = buckets[ws]
        writer.writerow([
            ws.strftime("%Y-%m-%d"),
            (ws + timedelta(days=6)).strftime("%Y-%m-%d"),
            b["in_calls"],
            _fmt_duration(b["in_secs"]),
            b["out_calls"],
            _fmt_duration(b["out_secs"]),
        ])

    conn.close()


# ── weekly_callers ────────────────────────────────────────────────────────────

def cmd_weekly_callers(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    rows = conn.execute(
        "SELECT call_type, date, duration, contact FROM calls ORDER BY date"
    ).fetchall()

    if not rows:
        print("No records found.", file=sys.stderr)
        conn.close()
        return

    # bucket by (week_start, contact)
    buckets: dict[tuple, dict] = {}
    first_week = last_week = None

    for r in rows:
        dt = _parse_date(r["date"])
        if dt is None:
            continue
        ws = _week_start(dt)
        if first_week is None or ws < first_week:
            first_week = ws
        if last_week is None or ws > last_week:
            last_week = ws

        key = (ws, r["contact"])
        if key not in buckets:
            buckets[key] = {"in_calls": 0, "in_secs": 0, "out_calls": 0, "out_secs": 0}
        b = buckets[key]
        ct = r["call_type"].strip().lower()
        secs = _duration_to_seconds(r["duration"])
        if ct == "incoming":
            b["in_calls"] += 1
            b["in_secs"]  += secs
        elif ct == "outgoing":
            b["out_calls"] += 1
            b["out_secs"]  += secs

    if first_week is None:
        print("No parseable dates found.", file=sys.stderr)
        conn.close()
        return

    writer = csv.writer(sys.stdout)
    writer.writerow([
        "week_start", "week_end", "contact",
        "incoming_calls", "incoming_duration",
        "outgoing_calls", "outgoing_duration",
    ])

    week = first_week
    while week <= last_week:
        week_end = (week + timedelta(days=6)).strftime("%Y-%m-%d")
        week_str = week.strftime("%Y-%m-%d")
        # only emit contacts that had at least one incoming or outgoing call
        active = {k: v for k, v in buckets.items()
                  if k[0] == week and (v["in_calls"] or v["out_calls"])}
        for (_, contact), b in sorted(active.items(), key=lambda x: x[0][1]):
            writer.writerow([
                week_str, week_end, contact,
                b["in_calls"], _fmt_duration(b["in_secs"]),
                b["out_calls"], _fmt_duration(b["out_secs"]),
            ])
        week += timedelta(weeks=1)

    conn.close()


# ── graph ─────────────────────────────────────────────────────────────────────

def _mermaid_id(index: int) -> str:
    return f"c{index}"


def _mermaid_escape(text: str) -> str:
    return text.replace('"', "#quot;")


def cmd_graph(args) -> None:
    conn = get_conn(args.db)
    init_db(conn)

    rows = conn.execute("""
        SELECT contact,
               COUNT(*) AS total,
               SUM(CASE WHEN call_type = 'Incoming' THEN 1 ELSE 0 END) AS incoming,
               SUM(CASE WHEN call_type = 'Outgoing' THEN 1 ELSE 0 END) AS outgoing,
               SUM(CASE WHEN call_type = 'Missed'   THEN 1 ELSE 0 END) AS missed
        FROM calls
        GROUP BY contact
        ORDER BY total DESC
        LIMIT ?
    """, (args.limit,)).fetchall()

    if not rows:
        print("No records found.", file=sys.stderr)
        conn.close()
        return

    node_defs = []
    edge_defs = []
    link_styles = []

    for i, row in enumerate(rows):
        nid = _mermaid_id(i)
        label = _mermaid_escape(row["contact"])
        counts = f"in:{row['incoming']} out:{row['outgoing']} miss:{row['missed']}"
        node_defs.append(f'    {nid}["{label}\\n{counts}"]')

    for i, row in enumerate(rows):
        nid = _mermaid_id(i)
        total = row["total"]
        edge_defs.append(f'    iphone ---|"{total} calls"| {nid}')
        if i < 10:
            link_styles.append(f"    linkStyle {i} stroke:#333,stroke-width:3px")
        else:
            link_styles.append(f"    linkStyle {i} stroke:#ccc,stroke-width:1px")

    out = ["flowchart LR", '    iphone["iPhone"]']
    out.extend(node_defs)
    out.extend(edge_defs)
    out.extend(link_styles)

    print("\n".join(out))
    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="call_log",
        description="iPhone call log analyzer",
    )
    parser.add_argument(
        "--db", default=str(DB_DEFAULT), metavar="PATH",
        help=f"SQLite database path (default: {DB_DEFAULT})"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # import
    p_import = sub.add_parser("import", help="Import one or more CSV files")
    p_import.add_argument("file", nargs="+", type=Path, help="CSV file(s) to import")
    p_import.add_argument("--replace", action="store_true",
                          help="Replace duplicate rows instead of skipping")
    p_import.set_defaults(func=cmd_import)

    # stats
    p_stats = sub.add_parser("stats", help="Overall statistics")
    p_stats.set_defaults(func=cmd_stats)

    # contacts
    p_contacts = sub.add_parser("contacts", help="Top contacts by call count")
    p_contacts.add_argument("-n", "--limit", type=int, default=20, metavar="N",
                            help="Number of contacts to show (default: 20)")
    p_contacts.set_defaults(func=cmd_contacts)

    # history
    p_history = sub.add_parser("history", help="Browse call history")
    p_history.add_argument("--contact", metavar="NAME", help="Filter by contact name (partial match)")
    p_history.add_argument("--type", metavar="TYPE",
                           help="Filter by call type (Incoming/Outgoing/Missed)")
    p_history.add_argument("--since", metavar="DATE", help="Start date (YYYY-MM-DD)")
    p_history.add_argument("--until", metavar="DATE", help="End date (YYYY-MM-DD)")
    p_history.add_argument("-n", "--limit", type=int, default=50, metavar="N",
                           help="Max rows to show (default: 50)")
    p_history.set_defaults(func=cmd_history)

    # weekly_callers
    p_wc = sub.add_parser("weekly_callers",
                          help="Per-week active-contact breakdown (CSV output)")
    p_wc.set_defaults(func=cmd_weekly_callers)

    # graph
    p_graph = sub.add_parser("graph", help="Mermaid flowchart of iPhone ↔ caller connections")
    p_graph.add_argument("-n", "--limit", type=int, default=50, metavar="N",
                         help="Max contacts to include (default: 50)")
    p_graph.set_defaults(func=cmd_graph)

    # weekly
    p_weekly = sub.add_parser("weekly", help="Per-week incoming/outgoing report (CSV output)")
    p_weekly.add_argument("--contact", metavar="NAME",
                          help="Filter by contact name (partial match)")
    p_weekly.set_defaults(func=cmd_weekly)

    # summary
    p_summary = sub.add_parser("summary", help="Monthly call summary")
    p_summary.add_argument("-n", "--months", type=int, default=24, metavar="N",
                           help="Number of months to show (default: 24)")
    p_summary.set_defaults(func=cmd_summary)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
