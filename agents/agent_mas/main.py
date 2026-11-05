from aegis_game.stub import *
import heapq
import json

# =============================================================================
# CPSC 383 - Assignment 2 (Multi-Agent Systems)
# Fall 2026
#
# Group members:
#   Geffery Addai     - Tutorial 03
#   David Oti George  - Tutorial ??
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
    "seen_msgs": set(),    # (sender_id, round_num, t) already processed
    "done_goals": set(),   # goal Locations that are finished
    "scan_claims": {},     # Location -> agent id that will drone_scan it
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
    """(M1, #6) Read messages, skip duplicates, update state per message type."""
    pass


def alive_ids():
    """(M1, #7) Ids heard from in the last 2 rounds, plus my own id."""
    return [get_id()]


def team_snapshot():
    """(M1, #7) List of (id, loc, energy, goal) for living agents (used by assign())."""
    return []


def am_i_leader():
    """(M1, #8) Lowest living id leads; next lowest takes over if it dies."""
    return get_id() == min(alive_ids())


def update_stacks():
    """(M1, #10) Store full stack per survivor loc when visible, else top layer (known=False)."""
    pass


def scan_if_needed():
    """(M1, #11) drone_scan an unknown, unclaimed goal stack if affordable.
    Returns True if it used this round's action (drone_scan), else False."""
    return False


# =============================================================================
# MEMBER 2 - David Oti George: map, pathfinding, energy, charging
# =============================================================================
def build_map():
    """(M2, #16) Round 1: cache move_cost + KILLER for every on-map cell, and chargers."""
    pass


def path(a, b):
    """(M2, #17) A*/Dijkstra with heapq, 8 directions, KILLER blocked.
    Cost = destination cell's move_cost. Returns (list of Locations, cost) or (None, inf)."""
    return None, float("inf")


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
# PATHFINDING FROM ASSIGNMENT 1 (author: Geffery Addai, A1)
# David adapts this into path() for #17 and cites it there.
# =============================================================================
"""
function used to find neighbors from the node current location.
Param: node location
"""
def findNeighbors(location):
    neighbors = {} #dictionary of neighbor locations and cost
    for d in Direction: # loop through different locations
        if d == Direction.CENTER: # if location == center then skip / ignore
            continue
        candidate = location.add(d) # get the next neighbor candidate by moving 1 space from current location
        if not on_map(candidate):# if the potential candidate is not map then skip / ignore
            continue
        cell = get_cell_info_at(candidate) #get the info potential next place

        if cell.is_killer_cell(): #  if cell is instant kill then skip / ignore
            continue
        neighbors[candidate] = cell.move_cost  #store possible location and travel cost

    return neighbors # return neighbor dictionary


"""
function used to guess how many moves are left to the survivor
Param: current location, location of goal
"""
def heuristic(location, goal):
    # diagonals count as 1 move here, so it's the bigger of the x gap and y gap
    # (chebyshev distance), and it never overestimates so A* stays optimal
    return max(abs(location.x - goal.x), abs(location.y - goal.y))


"""
A* search
"""
def moddedAStar(start, goal, neighbors, h):
    # A* pathfinding, based on: https://www.redblobgames.com/pathfinding/a-star/introduction.html
    unexplored_nodes = {start}        # cells found but not checked yet
    past_locations = {}               # where agent came from to get to each cell
    cheapest_cost = {start: 0}        # cheapest energy found so far to reach each cell
    f = {start: h(start, goal)}       # cost so far + guess of what's left

    while unexplored_nodes:
        # grab the cell with the lowest f
        explore_next = min(unexplored_nodes, key=lambda n: f[n])

        # made it to the survivor, so rebuild the path
        if explore_next == goal:
            path = [explore_next]
            while explore_next in past_locations:
                explore_next = past_locations[explore_next]
                path.append(explore_next)
            return path[::-1]         # reverse path array so it goes start -> goal

        unexplored_nodes.remove(explore_next)   # done with this cell

        # look at every potential location agent can step to from here
        for next_neighbor, step in neighbors(explore_next).items():
            new_cost = cheapest_cost[explore_next] + step
            # only keep it if this way is cheaper than any way found before
            if new_cost < cheapest_cost.get(next_neighbor, float("inf")):
                past_locations[next_neighbor] = explore_next
                cheapest_cost[next_neighbor] = new_cost
                f[next_neighbor] = new_cost + h(next_neighbor, goal)
                unexplored_nodes.add(next_neighbor)

    return None                       # ran out of cells, no path to the survivor


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
