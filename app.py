import time
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from collections import defaultdict

SCOREBOARD_URL = "https://api-web.nhle.com/v1/scoreboard/now"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
REFRESH_MS = 3000

FACEOFF_TYPE = "faceoff"
SHOT_TYPES = {"shot-on-goal", "goal"}

st.set_page_config(page_title="NHL Markets Dev Tool", layout="wide")


def init_state():
    defaults = {
        "games": [],
        "selected_game_label": None,
        "selected_game_id": None,
        "tracking": False,
        "previous_faceoff_count": None,
        "previous_live_period": None,
        "previous_faceoff_teams": {},
        "previous_sog_event_ids": set(),
        "warning_message": "STATUS: OK",
        "warning_type": "ok",
        "alert_shown_until": 0.0,
        "alert_log": [],
        "filter_recent": False,
        "color_mode": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def fetch_json(url: str) -> dict:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def extract_abbrev(value, fallback="UNK"):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        if value.get("default"):
            return value["default"]
        for v in value.values():
            if isinstance(v, str) and v:
                return v
    return fallback


def load_live_games():
    data = fetch_json(SCOREBOARD_URL)
    games = []
    for day in data.get("gamesByDate", []):
        for game in day.get("games", []):
            if game.get("gameState") not in {"LIVE", "CRIT"}:
                continue
            away = extract_abbrev(game.get("awayTeam", {}).get("abbrev"), "AWAY")
            home = extract_abbrev(game.get("homeTeam", {}).get("abbrev"), "HOME")
            game_id = game.get("id")
            games.append({
                "label": f"{away} @ {home} ({game_id})",
                "id": game_id,
                "away": away,
                "home": home,
            })
    return games


def parse_clock_to_seconds(clock_str: str):
    try:
        minutes, seconds = clock_str.split(":")
        return int(minutes) * 60 + int(seconds)
    except Exception:
        return None


def seconds_to_clock(total_seconds: int) -> str:
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def convert_to_time_remaining(clock_str: str, period: int | None, game_data=None) -> str:
    secs_elapsed = parse_clock_to_seconds(clock_str)
    if secs_elapsed is None:
        return clock_str

    period_len = 1200
    if period is not None and period > 3:
        game_type = str(game_data.get("gameType", "")).strip() if game_data else ""
        if game_type in {"2", "02"}:
            period_len = 300
        elif game_type not in {"3", "03"}:
            period_len = 1200 if secs_elapsed > 300 else 300

    return seconds_to_clock(max(0, period_len - secs_elapsed))


def build_team_lookup(game_data: dict) -> dict:
    lookup = {}
    for key, fallback in (("homeTeam", "HOME"), ("awayTeam", "AWAY")):
        team = game_data.get(key) or {}
        team_id = team.get("id")
        if team_id is not None:
            lookup[team_id] = extract_abbrev(team.get("abbrev"), fallback)
    return lookup


def get_home_away_abbrevs(game_data: dict):
    home = game_data.get("homeTeam") or {}
    away = game_data.get("awayTeam") or {}
    return (
        extract_abbrev(home.get("abbrev"), "HOME"),
        extract_abbrev(away.get("abbrev"), "AWAY"),
    )


def safe_team(play: dict, team_lookup: dict) -> str:
    details = play.get("details") or {}

    for team_id in (
        play.get("eventOwnerTeamId"),
        play.get("teamId"),
        details.get("eventOwnerTeamId"),
        details.get("teamId"),
    ):
        if team_id in team_lookup:
            return team_lookup[team_id]

    team_dict = play.get("team")
    for abbrev in (
        play.get("teamAbbrev"),
        team_dict.get("abbrev") if isinstance(team_dict, dict) else None,
        details.get("eventOwnerTeamAbbrev"),
        details.get("teamAbbrev"),
        details.get("winningTeamAbbrev"),
    ):
        parsed = extract_abbrev(abbrev, None)
        if parsed:
            return parsed

    return "UNK"


def label_home_away(team: str, home_abbrev: str, away_abbrev: str) -> str:
    if team == home_abbrev:
        return f"{team} (Home)"
    if team == away_abbrev:
        return f"{team} (Away)"
    return team


def parse_raw_events(game_data: dict) -> list[dict]:
    plays = game_data.get("plays") or []
    team_lookup = build_team_lookup(game_data)
    home_abbrev, away_abbrev = get_home_away_abbrevs(game_data)

    deduped = {}
    for play in plays:
        play_type = str(play.get("typeDescKey", "")).lower()
        if play_type not in SHOT_TYPES and play_type != FACEOFF_TYPE:
            continue

        if play_type == FACEOFF_TYPE:
            display_type = "FACEOFF"
        elif play_type == "shot-on-goal":
            display_type = "SOG"
        else:
            display_type = "GOAL"

        team = label_home_away(safe_team(play, team_lookup), home_abbrev, away_abbrev)
        period = (play.get("periodDescriptor") or {}).get("number")

        deduped[play.get("eventId")] = {
            "event_id": play.get("eventId"),
            "period": period,
            "time_in_period_raw": play.get("timeInPeriod", ""),
            "time_remaining": convert_to_time_remaining(
                play.get("timeInPeriod", ""), period, game_data
            ),
            "team": team,
            "raw_type": play_type,
            "display_type": display_type,
        }

    return list(deduped.values())


def add_period_local_numbers(events: list[dict]) -> list[dict]:
    faceoff_counts: dict[int, int] = defaultdict(int)
    sog_counts: dict[int, int] = defaultdict(int)
    numbered = []

    for event in events:
        period = event["period"]
        event_copy = {**event, "faceoff_number": None, "sog_number": None}

        if event["display_type"] == "FACEOFF":
            faceoff_counts[period] += 1
            event_copy["faceoff_number"] = faceoff_counts[period]
        elif event["display_type"] in {"SOG", "GOAL"}:
            sog_counts[period] += 1
            event_copy["sog_number"] = sog_counts[period]

        numbered.append(event_copy)

    return numbered


def get_game_state(game_id: int) -> dict:
    data = fetch_json(PBP_URL.format(game_id=game_id))
    events = add_period_local_numbers(parse_raw_events(data))

    faceoffs = [e for e in events if e["display_type"] == "FACEOFF"]
    sog_events = [e for e in events if e["display_type"] in {"SOG", "GOAL"}]

    by_period_faceoffs: dict[int, int] = defaultdict(int)
    by_period_sog: dict[int, int] = defaultdict(int)
    for e in faceoffs:
        by_period_faceoffs[e["period"]] += 1
    for e in sog_events:
        by_period_sog[e["period"]] += 1

    live_period = events[-1]["period"] if events else 1
    live_period_faceoffs = [e for e in faceoffs if e["period"] == live_period]
    home_abbrev, away_abbrev = get_home_away_abbrevs(data)

    clock = data.get("clock") or {}
    clock_secs = parse_clock_to_seconds(clock.get("timeRemaining", ""))
    in_intermission = bool(clock.get("inIntermission", False))

    return {
        "events": events,
        "faceoffs": faceoffs,
        "sog_events": sog_events,
        "by_period_faceoffs": dict(by_period_faceoffs),
        "by_period_sog": dict(by_period_sog),
        "faceoff_total": len(faceoffs),
        "sog_total": len(sog_events),
        "live_period": live_period,
        "live_period_faceoff_count": len(live_period_faceoffs),
        "last_faceoff": faceoffs[-1] if faceoffs else None,
        "home_abbrev": home_abbrev,
        "away_abbrev": away_abbrev,
        "home_label": f"{home_abbrev} (Home)",
        "away_label": f"{away_abbrev} (Away)",
        "clock_secs": clock_secs,
        "in_intermission": in_intermission,
    }


def bucket_for_sog(event: dict) -> str | None:
    buckets = [
        (1200, 1081), (1080, 961), (960, 841), (840, 721), (720, 601),
        (600, 481), (480, 361), (360, 241), (240, 121), (120, 1),
    ]
    secs = parse_clock_to_seconds(event["time_remaining"])
    if secs is None:
        return None
    for start, end in buckets:
        if end <= secs <= start:
            return bucket_label(start, end)
    return None


def bucket_label(start_sec: int, end_sec: int) -> str:
    return f"{seconds_to_clock(start_sec)}-{seconds_to_clock(end_sec)}"


def build_two_minute_buckets(period_events: list[dict], home_label: str, away_label: str, period_finished: bool = False, clock_secs: int | None = None) -> list[dict]:
    buckets = [
        (1200, 1081), (1080, 961), (960, 841), (840, 721), (720, 601),
        (600, 481), (480, 361), (360, 241), (240, 121), (120, 1),
    ]
    sogs = [e for e in period_events if e["display_type"] in {"SOG", "GOAL"}]

    if period_finished:
        current_secs = 0
    elif clock_secs is not None:
        current_secs = clock_secs
    else:
        clocks = [parse_clock_to_seconds(e["time_remaining"]) for e in period_events]
        current_secs = min((s for s in clocks if s is not None), default=None)

    results = []
    for start, end in buckets:
        hits = [
            e for e in sogs
            if (secs := parse_clock_to_seconds(e["time_remaining"])) is not None
            and end <= secs <= start
        ]
        complete = period_finished or (current_secs is not None and current_secs < end)

        home_hit = any(e["team"] == home_label for e in hits)
        away_hit = any(e["team"] == away_label for e in hits)

        results.append({
            "window": bucket_label(start, end),
            "home_result": "YES" if home_hit else ("NO" if complete else "—"),
            "away_result": "YES" if away_hit else ("NO" if complete else "—"),
        })
    return results



def build_first_sog_after_faceoff(period_faceoffs: list[dict], period_events: list[dict], all_events: list[dict] | None = None) -> list[dict]:
    results = []
    for faceoff in period_faceoffs:
        first_sog = None
        found_anchor = False
        for event in period_events:
            if event["event_id"] == faceoff["event_id"]:
                found_anchor = True
                continue
            if found_anchor and event["display_type"] in {"SOG", "GOAL"}:
                first_sog = event
                break

        if first_sog is None and all_events:
            found_anchor = False
            for event in all_events:
                if event["event_id"] == faceoff["event_id"]:
                    found_anchor = True
                    continue
                if found_anchor and event["display_type"] in {"SOG", "GOAL"}:
                    first_sog = event
                    break

        if first_sog:
            same_period = first_sog["period"] == faceoff["period"]
            shot_str = f"{first_sog['time_remaining']} {first_sog['team']}" if same_period else f"P{first_sog['period']} {first_sog['time_remaining']} {first_sog['team']}"
        else:
            shot_str = "NO"

        results.append({
            "Faceoff #": faceoff["faceoff_number"],
            "Faceoff Time": faceoff["time_remaining"],
            "Faceoff Team": faceoff["team"],
            "First Shot": shot_str,
        })
    return results


TEAM_COLORS = {
    "ANA": "#F47A38", "ARI": "#8C2633", "BOS": "#FFB81C", "BUF": "#003087",
    "CAR": "#CC0000", "CBJ": "#002654", "CGY": "#C8102E", "CHI": "#CF0A2C",
    "COL": "#6F263D", "DAL": "#006847", "DET": "#CE1126", "EDM": "#FF4C00",
    "FLA": "#C8102E", "LAK": "#111111", "MIN": "#154734", "MTL": "#AF1E2D",
    "NJD": "#CE1126", "NSH": "#FFB81C", "NYI": "#00539B", "NYR": "#0038A8",
    "OTT": "#C8102E", "PHI": "#F74902", "PIT": "#FCB514", "SEA": "#99D9D9",
    "SJS": "#006D75", "STL": "#002F87", "TBL": "#002868", "TOR": "#003E7E",
    "UTA": "#6CACE4", "VAN": "#00843D", "VGK": "#B4975A", "WSH": "#C8102E",
    "WPG": "#041E42",
}


def team_color_for(cell_value: str) -> str | None:
    for abbrev, color in TEAM_COLORS.items():
        if abbrev in cell_value:
            return color
    return None


def pill_text_color(bg_hex: str) -> str:
    h = bg_hex.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000" if luminance > 0.5 else "#fff"


def html_table(rows: list[dict], color_mode: bool = False) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    th = "".join(
        f'<th style="padding:6px 12px; text-align:left; border-bottom:2px solid var(--secondary-background-color); '
        f'font-size:13px; color:var(--text-color); font-weight:700; white-space:nowrap;">{h}</th>'
        for h in headers
    )
    body = ""
    for i, row in enumerate(rows):
        bg = "rgba(128,128,128,0.04)" if i % 2 == 0 else "rgba(128,128,128,0.12)"
        tds = ""
        for h in headers:
            val = row[h]
            if color_mode:
                if val == "YES":
                    display = f'<span style="background-color:#00cc44; color:#000; padding:2px 10px; border-radius:12px; font-weight:700; font-size:12px;">YES</span>'
                elif val == "NO":
                    display = f'<span style="background-color:#cc2200; color:#fff; padding:2px 10px; border-radius:12px; font-weight:700; font-size:12px;">NO</span>'
                else:
                    team_color = team_color_for(str(val))
                    if team_color:
                        text_color = pill_text_color(team_color)
                        display = f'<span style="background-color:{team_color}; color:{text_color}; padding:2px 10px; border-radius:12px; font-weight:700; font-size:12px;">{val}</span>'
                    else:
                        display = val
            else:
                display = val
            tds += f'<td style="padding:6px 12px; font-size:13px; white-space:nowrap; color:var(--text-color); font-weight:600;">{display}</td>'
        body += f'<tr style="background-color:{bg};">{tds}</tr>'
    return (
        f'<div style="overflow-x:auto; width:100%;">'
        f'<table style="width:100%; border-collapse:collapse;">'
        f'<thead><tr>{th}</tr></thead>'
        f'<tbody>{body}</tbody>'
        f'</table></div>'
    )


_WARNING_STYLES = {
    "alert": ("background-color:#3a1600", "color:#ffd966", "border:2px solid #ff9900"),
    "ok": ("background-color:#132117", "color:#66ff99", "border:2px solid #2e6b45"),
}


def warning_box(message: str, warning_type: str):
    style = "; ".join(_WARNING_STYLES.get(warning_type, _WARNING_STYLES["ok"]))
    st.markdown(
        f'<div style="margin-top:10px; margin-bottom:18px; padding:16px; border-radius:10px;'
        f' font-size:26px; font-weight:700; {style}">{message}</div>',
        unsafe_allow_html=True,
    )


# --- App ---

init_state()

st.markdown(
    """
    <style>
    [data-testid="column"] {
        min-width: 320px;
        flex: 1 1 320px;
    }
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap;
    }
    .ag-row-selected .ag-cell {
        background-color: rgba(255, 153, 0, 0.2) !important;
    }
    .ag-cell-focus {
        border-color: transparent !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Sidebar: controls
with st.sidebar:
    st.title("NHL Markets")
    st.caption("Dev / review tool only. Do not result off this tool.")
    st.divider()

    if st.button("Load Live Games", use_container_width=True):
        try:
            games = load_live_games()
            st.session_state.games = games
            if not games:
                st.session_state.selected_game_label = None
                st.session_state.selected_game_id = None
                st.session_state.tracking = False
                st.info("No live games found.")
            else:
                labels = [g["label"] for g in games]
                if st.session_state.selected_game_label not in labels:
                    st.session_state.selected_game_label = labels[0]
                    st.session_state.selected_game_id = games[0]["id"]
                st.success(f"Loaded {len(games)} game(s).")
        except Exception as e:
            st.error(f"Error: {e}")

    game_labels = [g["label"] for g in st.session_state.games]
    selected_label = st.selectbox(
        "Game",
        options=game_labels,
        index=game_labels.index(st.session_state.selected_game_label)
        if st.session_state.selected_game_label in game_labels
        else None,
        placeholder="Load games first",
    )
    if selected_label:
        st.session_state.selected_game_label = selected_label
        for game in st.session_state.games:
            if game["label"] == selected_label:
                st.session_state.selected_game_id = game["id"]
                break

    st.divider()
    manual_id = st.text_input("Or enter a Game ID manually", placeholder="e.g. 2024030411")
    if st.button("Load Manual Game ID", use_container_width=True):
        if manual_id.strip().isdigit():
            st.session_state.selected_game_id = int(manual_id.strip())
            st.session_state.selected_game_label = f"Manual ({manual_id.strip()})"
            st.success(f"Game ID {manual_id.strip()} loaded.")
        else:
            st.error("Enter a numeric game ID.")

    st.divider()
    if st.button("Track Selected Game", use_container_width=True, type="primary"):
        if st.session_state.selected_game_id is None:
            st.warning("Load and select a game first.")
        else:
            st.session_state.tracking = True
            st.session_state.previous_faceoff_count = None
            st.session_state.previous_live_period = None
            st.session_state.previous_faceoff_teams = {}
            st.session_state.previous_sog_event_ids = {}
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"

    label = "Newest First: ON" if st.session_state.filter_recent else "Newest First: OFF"
    if st.button(label, use_container_width=True):
        st.session_state.filter_recent = not st.session_state.filter_recent

    color_label = "Color Mode: ON" if st.session_state.color_mode else "Color Mode: OFF"
    if st.button(color_label, use_container_width=True):
        st.session_state.color_mode = not st.session_state.color_mode

# Main area
if st.session_state.tracking:
    st_autorefresh(interval=REFRESH_MS, key="market_dev_refresh")

    tab_main, tab_log = st.tabs(["Live", "Alert Log"])

    with tab_log:
        log = st.session_state.alert_log
        if log:
            if st.button("Clear Log", key="clear_log"):
                st.session_state.alert_log = []
                st.rerun()
            for entry in reversed(log):
                color = "#ff9900" if entry["Type"] == "alert" else "#66ff99"
                st.markdown(
                    f'<div style="padding:10px 14px; margin-bottom:6px; border-radius:8px; '
                    f'background-color:var(--secondary-background-color); border-left:4px solid {color}; '
                    f'font-size:15px; color:var(--text-color);">'
                    f'<span style="font-weight:700; color:{color};">P{entry["Period"]}</span>'
                    f'&nbsp;&nbsp;{entry["Alert"]}</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("No alerts recorded yet.")

    with tab_main:
        try:
            state = get_game_state(st.session_state.selected_game_id)

            live_period = state["live_period"]
            live_period_faceoff_count = state["live_period_faceoff_count"]
            previous_faceoff_count = st.session_state.previous_faceoff_count
            previous_live_period = st.session_state.previous_live_period

            current_faceoff_teams = {
                e["event_id"]: (e["period"], e["faceoff_number"], e["team"])
                for e in state["faceoffs"]
            }
            current_sog_ids = {e["event_id"]: e for e in state["sog_events"]}

            prev_faceoff_teams = st.session_state.previous_faceoff_teams
            prev_sog_ids = st.session_state.previous_sog_event_ids

            alerts = []

            if previous_live_period == live_period:
                if previous_faceoff_count is not None:
                    delta = live_period_faceoff_count - previous_faceoff_count
                    if delta < 0:
                        alerts.append(f"FACEOFF COUNT DECREASE: {previous_faceoff_count} → {live_period_faceoff_count}")
                    elif delta > 1:
                        alerts.append(f"MULTIPLE FACEOFFS ADDED: +{delta}")

                for eid, (period, fo_num, team) in current_faceoff_teams.items():
                    if eid in prev_faceoff_teams:
                        _, _, prev_team = prev_faceoff_teams[eid]
                        if prev_team != team:
                            alerts.append(f"FACEOFF TEAM CHANGED: P{period} Faceoff #{fo_num} — {prev_team} → {team}")

                for eid in prev_sog_ids:
                    if eid not in current_sog_ids:
                        prev_event = prev_sog_ids[eid] if isinstance(prev_sog_ids, dict) else None
                        if prev_event:
                            bucket = bucket_for_sog(prev_event)
                            bucket_str = f" (bucket {bucket})" if bucket else ""
                            alerts.append(
                                f"SOG REMOVED: P{prev_event['period']} {prev_event['time_remaining']} "
                                f"{prev_event['team']}{bucket_str}"
                            )
                        else:
                            alerts.append(f"SOG REMOVED: event {eid}")
            else:
                alerts.append(f"Period {live_period} started")

            if alerts:
                period_changed = previous_live_period != live_period
                alert_type = "ok" if period_changed else "alert"
                msg = " | ".join(f"⚠ {a}" for a in alerts)
                st.session_state.warning_message = msg
                st.session_state.warning_type = alert_type
                st.session_state.alert_shown_until = time.time() + 7
                for a in alerts:
                    st.session_state.alert_log.append({
                        "Time": time.strftime("%H:%M:%S"),
                        "Period": live_period,
                        "Alert": a,
                        "Type": alert_type,
                    })
            elif time.time() >= st.session_state.alert_shown_until:
                st.session_state.warning_message = "STATUS: OK"
                st.session_state.warning_type = "ok"

            st.session_state.previous_faceoff_count = live_period_faceoff_count
            st.session_state.previous_live_period = live_period
            st.session_state.previous_faceoff_teams = current_faceoff_teams
            st.session_state.previous_sog_event_ids = current_sog_ids

            warning_box(st.session_state.warning_message, st.session_state.warning_type)

            hero_left, hero_right = st.columns([1, 3])
            with hero_left:
                st.markdown(
                    f"<div style='text-align:center; font-size:22px; font-weight:600; opacity:0.6; margin-bottom:4px;'>PERIOD</div>"
                    f"<div style='text-align:center; font-size:60px; font-weight:700; line-height:1;'>{live_period}</div>",
                    unsafe_allow_html=True,
                )
            with hero_right:
                st.markdown(
                    f"<div style='text-align:center; font-size:22px; font-weight:600; opacity:0.6; margin-bottom:4px;'>LIVE FACEOFFS</div>"
                    f"<div style='text-align:center; font-size:80px; font-weight:700; line-height:1;'>{live_period_faceoff_count}</div>",
                    unsafe_allow_html=True,
                )
                if lf := state["last_faceoff"]:
                    st.markdown(
                        f"<div style='text-align:center; font-size:13px; opacity:0.6;'>Last — P{lf['period']} {lf['time_remaining']} | {lf['team']} | #{lf['faceoff_number']}</div>",
                        unsafe_allow_html=True,
                    )

            st.divider()

            periods_present = sorted({e["period"] for e in state["events"] if e["period"] is not None}) or [1]
            selected_period = st.selectbox(
                "Period",
                options=periods_present,
                index=periods_present.index(live_period) if live_period in periods_present else len(periods_present) - 1,
                label_visibility="collapsed",
            )

            period_events = [e for e in state["events"] if e["period"] == selected_period]
            period_faceoffs = [e for e in period_events if e["display_type"] == "FACEOFF"]

            left, right = st.columns(2)

            with left:
                st.subheader(f"P{selected_period} — First Shot After Faceoff")
                rows = build_first_sog_after_faceoff(period_faceoffs, period_events, state["events"])
                if rows:
                    if st.session_state.filter_recent:
                        rows = list(reversed(rows))
                    st.markdown(html_table(rows, st.session_state.color_mode), unsafe_allow_html=True)
                else:
                    st.info("No faceoffs found in this period.")

            with right:
                st.subheader(f"P{selected_period} — 2-Min SOG Buckets")
                period_finished = selected_period < live_period or (selected_period == live_period and state["in_intermission"])
                bucket_results = build_two_minute_buckets(
                    period_events, state["home_label"], state["away_label"],
                    period_finished=period_finished,
                    clock_secs=state["clock_secs"] if selected_period == live_period else None,
                )
                rows = [
                    {
                        "Window": b["window"],
                        state["away_label"]: b["away_result"],
                        state["home_label"]: b["home_result"],
                    }
                    for b in bucket_results
                ]
                if st.session_state.filter_recent:
                    rows = list(reversed(rows))
                st.markdown(html_table(rows, st.session_state.color_mode), unsafe_allow_html=True)

        except Exception as e:
            st.error(f"Refresh error: {e}")
else:
    warning_box("STATUS: OK", "ok")
    st.info("Load live games, select one, and click Track Selected Game.")
