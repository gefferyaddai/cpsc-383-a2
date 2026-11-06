from aegis_game.stub import *
import heapq
import json

# =============================================================================
# CPSC 383 - Assignment 2 (Multi-Agent Systems)
# Fall 2026
#
# Group members:
#   Geffery Addai     - Tutorial 03
#   David Oti George  - Tutorial 03
#   Anant             - Tutorial ??
#   Morgan Lee        - Tutorial ??
#
# Date: Nov 2026
#
# Approach (leader-based plan, see docs/assignment2-projectoutline.md):
#   1. Know the map   - round 1: cache move costs, KILLER cells, chargers, goals
#   2. Share state    - every round each agent broadcasts STATUS; full stacks go out as SCAN
#   3. Plan centrally - the lowest living id (leader) assigns agents to goals (ASSIGN)
#   4. Act locally    - each agent paths to its goal, charges if needed, digs/saves
#   5. Re-plan        - DONE, a death, or a newly revealed layer makes the leader reassign
#
# TODO (Geffery, #14): finalise header (tutorials, UCIDs if required)
# TODO (Geffery, #15): AI-use appendix comment block at the bottom of this file
# =============================================================================


# =============================================================================
# MESSAGE FORMAT (#4) - every message is json.dumps of a dict with type key "t".
# Locations are always [x, y] lists (never Location objects) inside messages.
# Messages arrive NEXT round; read_messages() holds the last 5 rounds.
#
#   STATUS  (everyone, every round)
#       {"t": "STATUS", "id": int, "loc": [x, y], "energy": int, "goal": [x, y] | None}
#   SCAN    (anyone who sees a full stack)
#       {"t": "SCAN", "loc": [x, y], "layers": [layer, ...]}   # top -> bottom
#       layer = {"k": "R", "agents": int, "energy": int}  for Rubble
#             | {"k": "S"}                                for Survivor
#   ASSIGN  (leader)
#       {"t": "ASSIGN", "a": {"<agent id>": [x, y] | None}}
#   DONE    (agent that empties a goal cell)
#       {"t": "DONE", "loc": [x, y]}
#
# ROUND ORDER (every agent, every round):
#   1. read_messages() and update state
#   2. leader only: recompute assignments if something changed, broadcast ASSIGN
#   3. pick one action from my assignment
#   4. send_message(status, [])   (free)
#   5. make exactly one action call
#
# Timing: round 1 everyone broadcasts + waits, round 2 leader assigns,
# round 3 agents start moving.
# =============================================================================


# =============================================================================
# SHARED STATE - module-level variables persist between rounds (think() runs
# once per round per agent). Changing this layout needs an OK in group chat.
# Keys are Location objects (hashable) unless noted.
# =============================================================================
state = {
    "round": 0,            # current round number
    "team": {},            # agent id -> {"loc": Location, "energy": int, "goal": Location|None, "last_heard": int, "dead": bool}
    "stacks": {},          # Location -> {"layers": [layer dicts top->bottom], "known": bool}
    "goals": {},           # Location -> {"needed": 1|2, "tentative": bool, "done": bool}
    "assign": {},          # agent id -> goal Location | None (newest ASSIGN)
    "assign_round": -1,    # round_num of the ASSIGN currently applied
    "map": {},             # Location -> {"cost": int, "killer": bool}
    "region": {},          # Location -> region id (from reachable_from flood fill)
    "chargers": [],        # list of charging Locations
    "seen_msgs": set(),    # (sender_id, round_num, raw text) already processed
    "done_goals": set(),   # goal Locations that are finished
    "scan_claims": {},     # Location -> agent id that will drone_scan it
    "scanned": {},         # Location -> round I drone_scanned it (full stack visible to me next 4 rounds)
    "paths": {},           # (start, goal) -> (path list, cost) cache
    "charging": None,      # charger Location if on a charging detour
    "done_round": -1,      # round I sent DONE (to time out waiting for ASSIGN)
    "dirty": True,         # True -> leader should re-plan this round
    "leader": None,        # id of the current leader (for logging hand-overs)
    "outbox": [],          # extra messages (SCAN/DONE/ASSIGN) to send this round
}


# =============================================================================
# SHARED HELPERS (everyone may use, ask before changing)
# =============================================================================
def to_xy(loc):
    """Location -> [x, y] for JSON messages (None stays None)."""
    return None if loc is None else [loc.x, loc.y]


def from_xy(xy):
    """[x, y] from a JSON message -> Location (None stays None)."""
    return None if xy is None else Location(xy[0], xy[1])


def layer_to_dict(layer):
    """Rubble/Survivor object -> layer dict used in state['stacks'] and SCAN."""
    if isinstance(layer, Rubble):
        return {"k": "R", "agents": layer.agents_required, "energy": layer.energy_required}
    if isinstance(layer, Survivor):
        return {"k": "S"}
    return None


def wait():
    """Stay put. move(Direction.CENTER) costs 0 energy."""
    move(Direction.CENTER)


# =============================================================================
# MEMBER 1 - Geffery Addai: messaging, team state, leader, drone scans
# =============================================================================
def send_status():
    """(M1, #5) Broadcast STATUS {id, loc, energy, goal} and update my own team row.

    send_message() never delivers to the sender, so I write my own row in
    state['team'] here; teammates' rows come from read_and_merge().
    Sending is free, so this is called every round before the one action.
    """
    my_id = get_id()
    loc = get_location()
    energy = get_energy_level()
    goal = follow_assign()  # None until the leader's first ASSIGN arrives

    # keep my own row current (alive_ids()/team_snapshot() read this table)
    state["team"][my_id] = {
        "loc": loc,
        "energy": energy,
        "goal": goal,
        "last_heard": state["round"],
        "dead": False,
    }

    msg = {"t": "STATUS", "id": my_id, "loc": to_xy(loc),
           "energy": energy, "goal": to_xy(goal)}
    send_message(json.dumps(msg), [])  # [] = broadcast to every teammate


def read_and_merge():
    """(M1, #6) Read messages, skip duplicates, update state per message type.

    read_messages() returns the last 5 rounds of messages every call, so each
    one is remembered in state['seen_msgs'] and only handled once. The key is
    (sender_id, round_num, raw text) because one agent can send several SCANs
    in the same round. Anything that changes the plan sets state['dirty'] so
    the leader re-runs assign() this round.
    """
    for m in read_messages():
        key = (m.sender_id, m.round_num, m.message)
        if key in state["seen_msgs"]:
            continue
        state["seen_msgs"].add(key)

        try:
            msg = json.loads(m.message)
        except ValueError:
            continue  # not one of our messages, ignore it
        if not isinstance(msg, dict):
            continue
        t = msg.get("t")

        if t == "STATUS":
            sid = msg["id"]
            row = state["team"].get(sid)
            # buffer order is not guaranteed, so never let an older STATUS overwrite a newer one
            if row is not None and row["last_heard"] > m.round_num:
                continue
            if row is None:
                state["dirty"] = True  # new teammate -> leader should include it
            state["team"][sid] = {
                "loc": from_xy(msg["loc"]),
                "energy": msg["energy"],
                "goal": from_xy(msg["goal"]),
                "last_heard": m.round_num,
                "dead": False,
            }

        elif t == "SCAN":
            loc = from_xy(msg["loc"])
            old = state["stacks"].get(loc)
            if old is None or not old["known"] or old["layers"] != msg["layers"]:
                state["dirty"] = True  # revealed or changed layers can change agents needed
            state["stacks"][loc] = {"layers": msg["layers"], "known": True}

        elif t == "ASSIGN":
            # newest ASSIGN wins; keys arrive as strings because JSON object keys are strings
            if m.round_num > state["assign_round"]:
                state["assign"] = {int(k): from_xy(v) for k, v in msg["a"].items()}
                state["assign_round"] = m.round_num

        elif t == "DONE":
            loc = from_xy(msg["loc"])
            state["done_goals"].add(loc)
            if loc in state["goals"]:
                state["goals"][loc]["done"] = True
            state["dirty"] = True  # freed agents need a new goal

    # messages older than the 5-round buffer can never come back, so forget them
    oldest = state["round"] - 6
    state["seen_msgs"] = {k for k in state["seen_msgs"] if k[1] >= oldest}


def alive_ids():
    """(M1, #7) Ids heard from in the last 2 rounds, plus my own id (sorted).

    A STATUS sent in round r is read in round r+1, so a living teammate's
    newest last_heard is normally round - 1. If it is older than round - 2,
    the agent has missed 2 STATUS rounds and is treated as dead. The first
    time an agent is marked dead, state['dirty'] is set so the leader
    reassigns its goal. A later STATUS (read_and_merge) revives it.
    """
    my_id = get_id()
    alive = [my_id]
    for aid, row in state["team"].items():
        if aid == my_id:
            continue
        if state["round"] - row["last_heard"] <= 2:
            alive.append(aid)
        elif not row["dead"]:
            row["dead"] = True
            state["dirty"] = True
            log("agent", aid, "silent since round", row["last_heard"], "-> dead")
    alive.sort()  # the sandbox has no sorted() builtin
    return alive


def team_snapshot():
    """(M1, #7) List of (id, loc, energy, goal) for living agents, sorted by id
    (used by assign()).

    My own row is written by send_status(), which runs after the leader plans,
    so for myself I use the live values instead of last round's row.
    """
    my_id = get_id()
    snap = []
    for aid in alive_ids():
        if aid == my_id:
            snap.append((my_id, get_location(), get_energy_level(), follow_assign()))
        else:
            row = state["team"][aid]
            snap.append((aid, row["loc"], row["energy"], row["goal"]))
    return snap


def am_i_leader():
    """(M1, #8) Lowest living id leads; next lowest takes over if it dies.

    Every agent runs this on the same team table, so all of them agree on the
    leader with no election messages. When the leader changes (the old one
    went silent), it is logged in state['leader'], and if I am the new leader
    I set state['dirty'] so I send a fresh ASSIGN straight away.
    Round 1 has no messages yet, so everyone sees only themselves as leader;
    think() does not call assign() in round 1, so that is harmless.
    """
    leader = min(alive_ids())
    if leader != state["leader"]:
        if state["leader"] is not None:
            log("leader change:", state["leader"], "->", leader)
        state["leader"] = leader
        if leader == get_id():
            state["dirty"] = True
    return leader == get_id()


def update_stacks():
    """(M1, #10) Store full stack per survivor loc when visible, else top layer (known=False).

    get_survs() lists every cell with a survivor anywhere in its stack (buried
    ones too) and drops a cell once all its survivors are saved (checked in the
    engine source, game.py get_survs).

    get_cell_info_at() shows every layer only if the cell is adjacent (or my
    own cell) or drone scanned; otherwise .layers is just [top_layer]. The
    engine does not say which case we are in, so a stack counts as visible when:
      - I am on or next to the cell, or
      - more than one layer came back (only possible when visible), or
      - I scanned it myself (state['scanned']). A scan made in round r is
        active from r+1 and lasts 5 ticks, so I trust rounds r+1..r+4.
    A teammate's scan reaches me as a SCAN message (read_and_merge) instead.

    When I see a full stack that is new or changed, I queue a SCAN so the team
    shares it, and set state['dirty'] so the leader re-plans.
    """
    me = get_location()
    rnd = state["round"]

    for loc in get_survs():
        info = get_cell_info_at(loc)
        layers = [d for d in (layer_to_dict(l) for l in info.layers) if d is not None]
        old = state["stacks"].get(loc)

        scan_round = state["scanned"].get(loc)
        scanned = scan_round is not None and scan_round + 1 <= rnd <= scan_round + 4
        visible = me.is_adjacent_to(loc) or len(layers) > 1 or scanned

        if visible:
            if old is None or not old["known"] or old["layers"] != layers:
                state["stacks"][loc] = {"layers": layers, "known": True}
                state["outbox"].append({"t": "SCAN", "loc": to_xy(loc), "layers": layers})
                state["dirty"] = True
            continue

        # only the top layer is visible from here
        top = layers[:1]
        if old is not None and old["known"]:
            if old["layers"][:1] == top:
                continue  # still matches the full stack we already know
            # top changed, so layers were removed: drop known layers until the tops line up
            rest = old["layers"]
            while rest and rest[:1] != top:
                rest = rest[1:]
            if rest:
                state["stacks"][loc] = {"layers": rest, "known": True}
            else:
                state["stacks"][loc] = {"layers": top, "known": False}
            state["dirty"] = True
        elif old is None or old["layers"] != top:
            state["stacks"][loc] = {"layers": top, "known": False}
            state["dirty"] = True


def scan_if_needed():
    """(M1, #11) drone_scan an unknown, unclaimed goal stack if affordable.
    Returns True if it used this round's action (drone_scan), else False.

    Who scans what is worked out locally, with no claim messages: the goals
    to scan (sorted by x, y) are paired with the living ids (sorted), so goal
    i goes to agent i. Every agent has the same team table and stacks, so they
    all compute the same pairing and each goal is scanned once.
    - A goal I scanned stays in the list for 2 more rounds (my SCAN reaches
      the team 2 rounds after the scan), so the pairing does not shift for me
      before the others have it.
    - Agent ids are random (10001+), so in round 1 nobody knows the team yet
      and everyone would pick the same goal: no scans before round 2.
    - drone_scan costs 2 energy; reaching 0 kills the agent, so it also needs
      energy_plan() (M2) and more than 2 energy left.
    The result is visible next round; update_stacks() then reads the full
    stack and queues the SCAN message.
    """
    rnd = state["round"]
    if rnd < 2:
        return False

    to_scan = []
    for loc in get_survs():
        if loc in state["done_goals"]:
            continue
        stack = state["stacks"].get(loc)
        scanned_at = state["scanned"].get(loc)
        mine_pending = scanned_at is not None and rnd - scanned_at <= 2
        if stack is None or not stack["known"] or mine_pending:
            to_scan.append(loc)
    to_scan.sort(key=lambda l: (l.x, l.y))

    ids = alive_ids()
    # the sandbox has no zip() builtin, so pair goal i with agent i by index
    pairs = min(len(to_scan), len(ids))
    state["scan_claims"] = {to_scan[i]: ids[i] for i in range(pairs)}

    me = get_id()
    target = None
    for loc, aid in state["scan_claims"].items():
        if aid == me:
            target = loc
            break
    if target is None:
        return False

    # already scanned (waiting for my SCAN to spread) or I can see it myself next round
    scanned_at = state["scanned"].get(target)
    if scanned_at is not None and rnd - scanned_at <= 4:
        return False
    if get_location().is_adjacent_to(target):
        return False

    trip_goal = follow_assign() or get_location()
    if get_energy_level() <= 2 or not energy_plan(trip_goal, 2):
        return False

    drone_scan(target)
    state["scanned"][target] = rnd
    log("drone_scan", target)
    return True


# =============================================================================
# MEMBER 2 - David Oti George: map, pathfinding, energy, charging
# =============================================================================
def build_map():
    """(M2, #16) Round 1: cache move_cost + KILLER for every on-map cell, and chargers.

    The API has no width/height call, so the size is found by walking along
    each axis from (0, 0) until on_map() is false. Only on-map cells are
    queried, because get_cell_info_at() raises an error off the map.
    Cell type and move_cost never change and are visible from anywhere, so
    reading them once here is enough. Layers do change, so update_stacks()
    tracks those instead.
    """
    width = 0
    while on_map(Location(width, 0)):
        width += 1
    height = 0
    while on_map(Location(0, height)):
        height += 1

    killers = 0
    for x in range(width):
        for y in range(height):
            loc = Location(x, y)
            info = get_cell_info_at(loc)
            killer = info.is_killer_cell()
            state["map"][loc] = {"cost": info.move_cost, "killer": killer}
            if killer:
                killers += 1

    state["chargers"] = get_charging_cells()
    log("map", width, "x", height, "cells", len(state["map"]),
        "killers", killers, "chargers", len(state["chargers"]))


# the 8 directions an agent can step in (CENTER is left out, it isn't a step)
DIRECTIONS = [
    Direction.NORTH,
    Direction.NORTHEAST,
    Direction.EAST,
    Direction.SOUTHEAST,
    Direction.SOUTH,
    Direction.SOUTHWEST,
    Direction.WEST,
    Direction.NORTHWEST,
]


def path(start, goal):
    """(M2, #17) Cheapest route from start to goal using A*, KILLER cells blocked.

    Returns (list of Locations from start to goal, both included, total cost),
    or (None, inf) if goal cannot be reached. Entering a cell costs that cell's
    move_cost, so the start cell is free and path(a, a) is ([a], 0).

    Adapted from David Oti-George's A1 search, based on
    https://www.redblobgames.com/pathfinding/a-star/introduction.html
    Changes from A1: cells come from state['map'] instead of get_cell_info_at(),
    an unreachable goal returns (None, inf), and results are cached.

    The cache in state['paths'] lasts the whole game, which is safe because
    costs and KILLER cells never change. Callers share the cached list, so
    they must not modify it.
    """
    key = (start, goal)
    if key in state["paths"]:
        return state["paths"][key]

    #if the goal is off the map or a killer cell there is no way to reach it
    goal_cell = state["map"].get(goal)
    if goal_cell is None or goal_cell["killer"]:
        state["paths"][key] = (None, float("inf"))
        return state["paths"][key]

    frontier = []
    counter = 0  #tie breaker, if 2 locations have the same priority the one added first is popped first

    came_from = {}
    cost_so_far = {}

    heapq.heappush(frontier, (0, counter, start))

    came_from[start] = None
    cost_so_far[start] = 0

    while frontier:

        #pop lowest priority
        current = heapq.heappop(frontier)[2]

        if current == goal:
            break

        #now for each direction adjacent to the current popped location
        for direction in DIRECTIONS:

            neighbor = current.add(direction)
            neighbor_cell = state["map"].get(neighbor)

            if neighbor_cell is None: #if location off map
                continue

            if neighbor_cell["killer"]: #if that neighbor cell is killer cell don't add to frontier
                continue

            new_cost = cost_so_far[current] + neighbor_cell["cost"]

            #add it if it has no cost so far, or if this way is cheaper than the existing one
            if neighbor not in cost_so_far or cost_so_far[neighbor] > new_cost:
                cost_so_far[neighbor] = new_cost
                priority = new_cost + heuristic(neighbor, goal)
                counter += 1
                heapq.heappush(frontier, (priority, counter, neighbor))
                came_from[neighbor] = current  #set the came_from of the neighbor to be the current popped location

    #frontier ran out without reaching the goal
    if goal not in came_from:
        state["paths"][key] = (None, float("inf"))
        return state["paths"][key]

    #backtrack from the goal to the start
    path = []
    current = goal

    while current is not None:
        path.append(current)
        current = came_from[current]

    path.reverse()

    state["paths"][key] = (path, cost_so_far[goal])
    return state["paths"][key]


def path_cost(a, b):
    """(M2, #17) Just the cost part of path(a, b)."""
    return path(a, b)[1]


def next_move(goal):
    """(M2, #18) Take one step along path(get_location(), goal); wait if no path."""
    wait()


def reachable_from(loc):
    """(M2, #19) Flood fill over non-KILLER cells; returns set of reachable Locations."""
    return set()


def region_id(loc):
    """(M2, #19) Region label for loc (same id = mutually reachable)."""
    return 0


def energy_plan(goal, work):
    """(M2, #20) True if energy covers path cost + work at goal + margin."""
    return True


def charge_detour(goal, work):
    """(M2, #21) If energy_plan fails: go to best charger and recharge() until the
    trip fits. Returns True if it used this round's action, else False."""
    return False


# =============================================================================
# MEMBER 3 - Anant: goals, agents needed, splitting the team
# =============================================================================
def build_goals():
    """(M3, #25) One goal per get_survs() location; drop finished ones."""
    pass


def agents_needed(goal):
    """(M3, #26) Highest agents_required over Rubble layers in the stack (1 or 2)."""
    return 1


def assign():
    """(M3, #27-#31) Leader only: staff every goal at once (cheapest, same region),
    place spares, write state['assign'] and queue an ASSIGN message."""
    pass


def follow_assign():
    """(M3, #30) My current goal from the newest ASSIGN (None until one arrives)."""
    return state["assign"].get(get_id())


# =============================================================================
# MEMBER 4 - Morgan Lee: digging, saving, layers, re-planning
# =============================================================================
def act_at_goal(goal):
    """(M4, #34) On the goal cell: Survivor -> save (one saver), Rubble -> dig
    (paired if 2), empty -> on_goal_done(). Must make exactly one action call."""
    wait()


def dig_ready():
    """(M4, #35) True when enough agents assigned to my goal share my cell."""
    return False


def on_goal_done(goal):
    """(M4, #39) Queue DONE, clear my goal, fall back to nearest goal after 2 rounds."""
    pass


# =============================================================================
# PATHFINDING FROM ASSIGNMENT 1 (author: David Oti-George, A1)
# The A* search itself was adapted into path() (#17).
# =============================================================================
#estimate how far the current location is from the goal,
#since diagonal moves are allowed we use the larger of the x or y distance
def heuristic(loc: Location, goal: Location) -> int:
    dx = abs(loc.x - goal.x)
    dy = abs(loc.y - goal.y)
    return max(dx, dy)


# =============================================================================
# MAIN LOOP - follows the round order above. Every path ends with exactly
# one STATUS message and exactly one action call.
# =============================================================================
def flush_outbox():
    """Send queued SCAN/DONE/ASSIGN messages (sending is free)."""
    for msg in state["outbox"]:
        send_message(json.dumps(msg), [])
    state["outbox"] = []


def choose_action():
    """Step 3: pick and perform exactly one action from my assignment."""
    goal = follow_assign()

    # no goal yet (or waiting for leader's ASSIGN)
    if goal is None:
        wait()
        return

    work = 0  # TODO (M2/M4): energy needed at the goal from state['stacks']
    if not energy_plan(goal, work):
        if charge_detour(goal, work):
            return

    if get_location() == goal:
        act_at_goal(goal)
    else:
        next_move(goal)


def think() -> None:
    """Do not remove this function, it must always be defined."""
    state["round"] = get_round_number()

    # round 1: nobody has messages yet -> cache the world, broadcast, wait/scan
    if state["round"] == 1:
        build_map()
        build_goals()
        update_stacks()
        send_status()
        flush_outbox()
        if not scan_if_needed():
            wait()
        return

    # 1. read messages and update shared state
    read_and_merge()
    update_stacks()
    build_goals()

    # 2. leader re-plans if something changed
    if am_i_leader() and state["dirty"]:
        assign()
        state["dirty"] = False

    # 3-5. STATUS + queued messages go out first (free), then one action.
    # Scans take priority so the leader learns hidden stacks before staffing.
    send_status()
    flush_outbox()
    if scan_if_needed():
        return
    choose_action()


# =============================================================================
# AI-USE APPENDIX (#15) - to be completed before submission
# =============================================================================
