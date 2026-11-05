from aegis_game.stub import *
#Geffery Addai
#Oct 4, 2026
#CPSC 383
#Fall 2026
# tut 03


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


"""
function used to plan and take next steps 
"""

def think() -> None:
    """Do not remove this function, it must always be defined."""
    # debug logs to see where agent is and energy spent each round
    log("At", get_location(), "energy", get_energy_level())
    log("Thinking")

    # round 1: stand still so the game shows the cell costs around current location
    if get_round_number() == 1:
        move(Direction.CENTER)
        return

    current_loc = get_location()               # current location

    # if current location is the location of a survivor, save them
    if current_loc in get_survs():
        save()
        log("Energy:", get_energy_level())     # energy left after the save
        return

    goal = get_survs()                         # list of survivor locations
    goal = goal[0]                             # only one survivor, so take the first

    # run A* from here to the survivor
    path = moddedAStar(current_loc, goal, findNeighbors, heuristic)

    # no route found (or already at the goal), so don't move this round
    if path is None or len(path) < 2:
        move(Direction.CENTER)
        return

    next_step = path[1]                        # path[0] == current location, so path[1] is the next cell
    direction = current_loc.direction_to(next_step)   # which way is that cell from current location
    move(direction)                            # one move per round, then A* runs again next round

    return


    # Get the top layer at the agent's current location.
    # If a survivor is present, save it and end the turn.
