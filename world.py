"""Static map model: terrain, solids, line of sight, walkability grid and A* paths."""
import heapq
import json
import math
import os
import time

import numpy as np


def _pip(px, py, flat):
    """Vectorized point-in-polygon (even-odd) for a flat [x0,y0,x1,y1,...] ring."""
    xs = np.asarray(flat[0::2], dtype=np.float64)
    ys = np.asarray(flat[1::2], dtype=np.float64)
    inside = np.zeros(px.shape, dtype=bool)
    n = len(xs)
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = xs[i], ys[i], xs[j], ys[j]
        if yi != yj:
            c = ((yi > py) != (yj > py)) & (px < (xj - xi) * (py - yi) / (yj - yi) + xi)
            inside ^= c
        j = i
    return inside


class World:
    C = 4  # nav cell size (m)

    def __init__(self, mapdir, cfg):
        t0 = time.time()
        mech = cfg["mech"]
        with open(os.path.join(mapdir, "v2", "world.json")) as f:
            w = json.load(f)
        tr = w["terrain"]
        self.nx, self.ny = int(tr["nx"]), int(tr["ny"])
        self.x0, self.y0 = float(tr["minX"]), float(tr["minY"])
        G = np.fromfile(os.path.join(mapdir, "v2", tr.get("file", "terrain.i16")), dtype="<i2")
        G = G.astype(np.float32).reshape(self.ny, self.nx) * float(tr.get("scale", 0.01))
        self.G = G
        ny, nx = self.ny, self.nx
        self.ST = np.full((ny, nx), -1e9, np.float32)  # solid top (buildings, stadium)
        self.SB = np.full((ny, nx), 1e9, np.float32)   # solid base
        self.BT = np.full((ny, nx), -1e9, np.float32)  # bridge top
        self.BB = np.full((ny, nx), 1e9, np.float32)   # bridge base
        blocked = np.zeros((ny, nx), bool)
        step_up = float(mech.get("step_up", 1.2))
        height = float(mech.get("height", 9))
        for p in w["prisms"]:
            ring = p["ring"]
            xs, ys = ring[0::2], ring[1::2]
            i0 = max(0, int(math.floor(min(xs) - self.x0)))
            i1 = min(nx - 1, int(math.ceil(max(xs) - self.x0)))
            j0 = max(0, int(math.floor(min(ys) - self.y0)))
            j1 = min(ny - 1, int(math.ceil(max(ys) - self.y0)))
            if i1 < i0 or j1 < j0:
                continue
            gx, gy = np.meshgrid(np.arange(i0, i1 + 1) + self.x0, np.arange(j0, j1 + 1) + self.y0)
            m = _pip(gx, gy, ring)
            for h in p.get("holes") or []:
                if len(h) >= 6:
                    m &= ~_pip(gx, gy, h)
            if not m.any():
                continue
            base, top = float(p["base"]), float(p["top"])
            sl = (slice(j0, j1 + 1), slice(i0, i1 + 1))
            if p.get("kind") == "bridge":
                self.BT[sl] = np.where(m, np.maximum(self.BT[sl], top), self.BT[sl])
                self.BB[sl] = np.where(m, np.minimum(self.BB[sl], base), self.BB[sl])
            else:
                self.ST[sl] = np.where(m, np.maximum(self.ST[sl], top), self.ST[sl])
                self.SB[sl] = np.where(m, np.minimum(self.SB[sl], base), self.SB[sl])
            g = G[sl]
            blocked[sl] |= m & (top - g > step_up) & (base - g < height)
        # surface height: what you stand on if you come down from above
        self.surf = np.maximum(G, np.maximum(self.ST, np.where(self.BT > -1e8, self.BT, -1e9)))
        # steep terrain
        slope = math.tan(math.radians(float(mech.get("max_slope_deg", 50))))
        dx = np.abs(np.diff(G, axis=1)) > slope
        dy = np.abs(np.diff(G, axis=0)) > slope
        blocked[:, 1:] |= dx
        blocked[:, :-1] |= dx
        blocked[1:, :] |= dy
        blocked[:-1, :] |= dy
        # arena edge
        b = w["bounds"]
        e = 4
        bi0 = int(b["minX"] - self.x0) + e
        bi1 = int(b["maxX"] - self.x0) - e
        bj0 = int(b["minY"] - self.y0) + e
        bj1 = int(b["maxY"] - self.y0) - e
        blocked[:, : max(bi0, 0)] = True
        blocked[:, bi1:] = True
        blocked[: max(bj0, 0), :] = True
        blocked[bj1:, :] = True
        self.blocked = blocked
        # inflate by mech radius (+margin)
        r = int(math.ceil(float(mech.get("radius", 2.5)) + 0.6))
        infl = blocked.copy()
        for dj in range(-r, r + 1):
            for di in range(-r, r + 1):
                if di * di + dj * dj > r * r or (di == 0 and dj == 0):
                    continue
                src = blocked[max(0, -dj): ny - max(0, dj), max(0, -di): nx - max(0, di)]
                infl[max(0, dj): ny - max(0, -dj), max(0, di): nx - max(0, -di)] |= src
        self.free1 = ~infl
        C = self.C
        self.nx4 = nx // C
        self.ny4 = ny // C
        sub = self.free1[: self.ny4 * C, : self.nx4 * C].reshape(self.ny4, C, self.nx4, C)
        frac = sub.mean(axis=(1, 3))
        centre = self.free1[C // 2: self.ny4 * C: C, C // 2: self.nx4 * C: C]
        f4 = centre & (frac > 0.45)
        self.free4 = f4
        self.f4 = f4.ravel().tolist()
        self.build_s = time.time() - t0

    # ---------------------------------------------------------------- lookups
    def idx(self, x, y):
        i = int(round(x - self.x0))
        j = int(round(y - self.y0))
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return j, i

    def ground(self, x, y):
        j, i = self.idx(x, y)
        return float(self.G[j, i])

    def surface(self, x, y):
        j, i = self.idx(x, y)
        return float(self.surf[j, i])

    def is_free(self, x, y):
        j, i = self.idx(x, y)
        return bool(self.free1[j, i])

    # ---------------------------------------------------------------- line of sight
    def los(self, a, b, step=1.5):
        """True if the 3D segment a->b is not blocked by terrain, buildings or bridges."""
        ax, ay, az = a
        bx, by, bz = b
        d = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2 + (bz - az) ** 2)
        n = int(d / step)
        if n < 2:
            return True
        n = min(n, 300)
        s = np.linspace(0.0, 1.0, n + 1)[1:-1]
        xs = ax + (bx - ax) * s
        ys = ay + (by - ay) * s
        zs = az + (bz - az) * s
        ii = np.clip(np.rint(xs - self.x0).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint(ys - self.y0).astype(np.int32), 0, self.ny - 1)
        if (zs < self.G[jj, ii] - 0.3).any():
            return False
        if ((zs < self.ST[jj, ii]) & (zs > self.SB[jj, ii])).any():
            return False
        if ((zs < self.BT[jj, ii]) & (zs > self.BB[jj, ii])).any():
            return False
        return True

    def walk_line(self, a, b):
        """True if a straight ground walk from a to b stays on free (inflated) cells."""
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        d = math.hypot(bx - ax, by - ay)
        n = int(d / 0.8) + 1
        s = np.linspace(0.0, 1.0, n + 1)
        ii = np.clip(np.rint(ax + (bx - ax) * s - self.x0).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint(ay + (by - ay) * s - self.y0).astype(np.int32), 0, self.ny - 1)
        return bool(self.free1[jj, ii].all())

    def clear_dist(self, x, y, ux, uy, maxd=12.0):
        """How far one can walk from (x,y) along unit (ux,uy) on free cells (up to maxd)."""
        n = int(maxd / 0.8)
        s = np.arange(1, n + 1) * 0.8
        ii = np.clip(np.rint(x + ux * s - self.x0).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint(y + uy * s - self.y0).astype(np.int32), 0, self.ny - 1)
        f = self.free1[jj, ii]
        bad = np.flatnonzero(~f)
        if len(bad) == 0:
            return maxd
        return float(s[bad[0]] - 0.8)

    # ---------------------------------------------------------------- A* on the nav grid
    def cell4(self, x, y):
        i = int((x - self.x0) // self.C)
        j = int((y - self.y0) // self.C)
        return min(max(j, 0), self.ny4 - 1), min(max(i, 0), self.nx4 - 1)

    def centre4(self, j, i):
        return (self.x0 + i * self.C + self.C / 2.0, self.y0 + j * self.C + self.C / 2.0)

    def snap4(self, j, i, rmax=6):
        f4, nx4 = self.f4, self.nx4
        if f4[j * nx4 + i]:
            return j, i
        for r in range(1, rmax + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    jj, ii = j + dj, i + di
                    if 0 <= jj < self.ny4 and 0 <= ii < nx4 and f4[jj * nx4 + ii]:
                        d = dj * dj + di * di
                        if best is None or d < best[0]:
                            best = (d, jj, ii)
            if best:
                return best[1], best[2]
        return None

    def path(self, start, goal, budget_s=0.010, weight=1.4):
        """A* from start (x,y) to goal (x,y). Returns list of (x,y) waypoints (may be partial)."""
        t_end = time.perf_counter() + budget_s
        s = self.snap4(*self.cell4(*start))
        g = self.snap4(*self.cell4(*goal))
        if s is None or g is None:
            return None
        nx4 = self.nx4
        ny4 = self.ny4
        f4 = self.f4
        si = s[0] * nx4 + s[1]
        gi = g[0] * nx4 + g[1]
        gj, gii = g
        SQ2 = 1.41421356

        def h(n):
            dj = abs(n // nx4 - gj)
            di = abs(n % nx4 - gii)
            return weight * ((di + dj) + (SQ2 - 2) * (di if di < dj else dj))

        openh = [(h(si), 0.0, si)]
        came = {si: -1}
        gs = {si: 0.0}
        best = (h(si), si)
        nbr = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
               (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))
        cnt = 0
        found = False
        while openh:
            f, gc, n = heapq.heappop(openh)
            if gc > gs.get(n, 1e18):
                continue
            if n == gi:
                found = True
                best = (0, n)
                break
            cnt += 1
            if (cnt & 127) == 0 and time.perf_counter() > t_end:
                break
            nj, ni = divmod(n, nx4)
            for dj, di, c in nbr:
                jj = nj + dj
                ii = ni + di
                if jj < 0 or jj >= ny4 or ii < 0 or ii >= nx4:
                    continue
                m = jj * nx4 + ii
                if not f4[m]:
                    continue
                if dj and di and (not f4[nj * nx4 + ii] or not f4[jj * nx4 + ni]):
                    continue
                ng = gc + c
                if ng < gs.get(m, 1e18):
                    gs[m] = ng
                    came[m] = n
                    hm = h(m)
                    if hm < best[0]:
                        best = (hm, m)
                    heapq.heappush(openh, (ng + hm, ng, m))
        end = best[1]
        out = []
        n = end
        while n != -1:
            out.append(self.centre4(*divmod(n, nx4)))
            n = came[n]
        out.reverse()
        if found:
            out.append((goal[0], goal[1]) if self.is_free(goal[0], goal[1]) else out[-1])
        return out
