from typing import List, Optional, Set

def mark_hits(grid:List[List[Optional[int]]], drawn:Set[int]):
    hit = [[False]*5 for _ in range(5)]
    for r in range(5):
        for c in range(5):
            v = grid[r][c]
            if v is None or v in drawn:
                hit[r][c] = True
    return hit

def has_bingo_standard(hit)->bool:
    for r in range(5):
        if all(hit[r][c] for c in range(5)): return True
    for c in range(5):
        if all(hit[r][c] for r in range(5)): return True
    if all(hit[i][i] for i in range(5)): return True
    if all(hit[i][4-i] for i in range(5)): return True
    return False

def has_bingo_corners(hit)->bool:
    return hit[0][0] and hit[0][4] and hit[4][0] and hit[4][4]

def has_bingo_x(hit)->bool:
    return all(hit[i][i] for i in range(5)) and all(hit[i][4-i] for i in range(5))

def check_bingo(hit, pattern:str)->bool:
    # Four Corners ist IMMER ein gültiger Bingo, egal welches Pattern
    if has_bingo_corners(hit):
        return True
    if pattern == 'x':
        return has_bingo_x(hit)
    if pattern == 'corners':
        return has_bingo_corners(hit)
    # default: Standard-Regelwerk (Zeile, Spalte, Diagonale)
    return has_bingo_standard(hit)
