"""Rough mechfight simulator for local testing (NOT the real server). 1 test bot vs 15 squad hunters."""
import gzip
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import mechfight as mf  # noqa: E402
from world import World  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ARENA = os.path.join(HERE, "..", "arena")
CFG = json.loads(gzip.open(os.path.join(HERE, "..", "check", "stream.jsonl.gz"), "rt").readline())["config"]
_WORLD = None


def world():
    global _WORLD
    if _WORLD is None:
        _WORLD = World(ARENA, CFG)
    return _WORLD


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


DIFF = {  # aim noise (rad), reaction, weapons
    "easy": (0.035, 0.6, ["gun"]),
    "medium": (0.02, 0.4, ["gun", "lightning"]),
    "hard": (0.012, 0.25, ["gun", "laser"]),
    "expert": (0.007, 0.15, ["gun", "laser"]),
}


class M:
    def __init__(self, mid, pos, diff=None):
        self.id = mid
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd_aim = [0.0, 0.0]
        self.hp = 1400.0
        self.max_hp = 1400.0
        self.energy = 100.0
        self.cubes = 0
        self.diff = diff
        self.alive = True
        self.last_dmg = -99
        self.last_spend = -99
        self.weapons = [self.wobj("gun"), None]
        if diff:
            ws = DIFF[diff][2]
            if len(ws) > 1:
                self.weapons[1] = self.wobj(ws[1])
        self.events = []
        self.death_t = None
        self.hunting = False
        self.path = None
        self.path_t = -9
        self.charging = 0.0
        self.upgrades = []

    def wobj(self, kind):
        st = dict(CFG["weapons"][kind])
        d = {"kind": kind, "cooldown": 0.0, "stats": st}
        if kind == "gun":
            d.update(ammo=st["mag"], reload=0.0)
        if kind == "laser":
            d["charge"] = 0.0
        return d

    def kinds(self):
        return [w["kind"] for w in self.weapons if w]


class Sim:
    def __init__(self, seed, bot_factory, verbose=False):
        self.rng = random.Random(seed)
        self.w = world()
        self.t = 0.0
        self.tick = 0
        self.verbose = verbose
        z = CFG["zone"]
        self.zc = list(z["start_center"])
        self.zr = z["start_radius"]
        self.phase = 0
        self.phase_t0 = 0.0
        self.next_c, self.next_r = self.pick_next(self.zc, self.zr, z["phases"][0]["radius"])
        self.mechs = []
        spots = []
        for i in range(16):
            for _ in range(500):
                a = self.rng.uniform(0, 2 * math.pi)
                r = self.rng.uniform(0, self.zr * 0.8)
                x, y = self.zc[0] + math.cos(a) * r, self.zc[1] + math.sin(a) * r
                if self.w.is_free(x, y) and all(math.dist((x, y), s) > 110 for s in spots):
                    break
            spots.append((x, y))
        diffs = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3
        self.me = M(0, (spots[0][0], spots[0][1], self.w.ground(*spots[0])))
        self.mechs.append(self.me)
        for i in range(15):
            self.mechs.append(M(i + 1, (spots[i + 1][0], spots[i + 1][1], self.w.ground(*spots[i + 1])), diffs[i]))
        self.pickups = {}
        nid = 8000
        for _ in range(40):
            x, y = self.rand_free()
            self.pickups[nid] = {"id": nid, "kind": self.rng.choice(["mortar", "laser", "lightning"]),
                                 "pos": [x, y, self.w.ground(x, y)]}
            nid += 1
        self.nid = nid
        self.crates = {}
        for k in range(36):
            x, y = self.rand_free()
            self.crates[1000 + k] = {"id": 1000 + k, "pos": [x, y, self.w.ground(x, y)], "hp": 300.0}
        self.cubes = {}
        self.cid = 5000
        self.bullets = []
        self.kills = 0
        self.mem = {}
        self.bot = bot_factory({"type": "hello", "slot": 0, "seed": seed, "map": {"dir": ARENA, "files": []}, "config": CFG})
        self.my_place = None
        self.shots = 0
        self.hits = 0
        self.dmg_by = {}
        self.max_ms = 0

    def rand_free(self):
        while True:
            x = self.rng.uniform(-480, 520)
            y = self.rng.uniform(-300, 360)
            if self.w.is_free(x, y) and math.dist((x, y), self.zc) < self.zr * 0.9:
                return x, y

    def pick_next(self, c, r, nr):
        for _ in range(200):
            a = self.rng.uniform(0, 2 * math.pi)
            d = self.rng.uniform(0, max(r - nr, 0) * 0.8)
            nc = [c[0] + math.cos(a) * d, c[1] + math.sin(a) * d]
            if self.w.is_free(*nc) or nr < 30:
                return nc, nr
        return list(c), nr

    def zone(self):
        ph = CFG["zone"]["phases"]
        if self.phase >= len(ph):
            return self.zc, self.zr, self.zc, self.zr, 1e9, 1e9, ph[-1]["dps"]
        p = ph[self.phase]
        s0 = self.phase_t0 + p["hold_s"]
        s1 = s0 + p["shrink_s"]
        return self.zc, self.zr, self.next_c, self.next_r, s0, s1, p["dps"]

    def zone_now(self):
        c0, r0, c1, r1, s0, s1, dps = self.zone()
        if self.t <= s0:
            return c0, r0, dps
        f = min(1, (self.t - s0) / (s1 - s0))
        return [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f], r0 + (r1 - r0) * f, dps

    def advance_zone(self):
        c0, r0, c1, r1, s0, s1, dps = self.zone()
        if self.t >= s1:
            self.zc, self.zr = c1, r1
            self.phase += 1
            self.phase_t0 = self.t
            ph = CFG["zone"]["phases"]
            if self.phase < len(ph):
                self.next_c, self.next_r = self.pick_next(self.zc, self.zr, ph[self.phase]["radius"])

    # ------------------------------------------------------------------ vision
    def sees(self, a, b):
        dx, dy = b.pos[0] - a.pos[0], b.pos[1] - a.pos[1]
        d = math.hypot(dx, dy)
        if d > 200:
            return False
        if d > 45:
            if abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(60):
                return False
        return self.w.los((a.pos[0], a.pos[1], a.pos[2] + 8), (b.pos[0], b.pos[1], b.pos[2] + 4.5)) or \
            self.w.los((a.pos[0], a.pos[1], a.pos[2] + 8), (b.pos[0], b.pos[1], b.pos[2] + 8.5))

    def sees_pt(self, a, q):
        dx, dy = q[0] - a.pos[0], q[1] - a.pos[1]
        d = math.hypot(dx, dy)
        if d > 200:
            return False
        if d > 45 and abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(60):
            return False
        return self.w.los((a.pos[0], a.pos[1], a.pos[2] + 8), (q[0], q[1], q[2] + 1))

    # ------------------------------------------------------------------ obs
    def obs_for_me(self):
        me = self.me
        mechs = []
        for m in self.mechs[1:]:
            if not m.alive:
                continue
            if self.sees(me, m):
                self.mem[m.id] = self.t
            if self.t - self.mem.get(m.id, -99) <= 5:
                mechs.append({"id": m.id, "kind": "mech", "pos": [round(v, 2) for v in m.pos], "vel": list(m.vel),
                              "aim": list(m.aim), "hp": m.hp, "max_hp": m.max_hp, "cubes": m.cubes,
                              "weapons": m.kinds(), "charging": m.charging > 0})
        z = self.zone()
        c, r, dps = self.zone_now()
        return {
            "type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": sum(m.alive for m in self.mechs),
            "self": {"id": 0, "pos": list(me.pos), "vel": list(me.vel), "aim": list(me.aim), "hp": me.hp,
                     "max_hp": me.max_hp, "energy": me.energy, "max_energy": 100, "grounded": True, "cubes": me.cubes,
                     "upgrades": list(me.upgrades), "in_zone": math.dist(me.pos[:2], c) <= r,
                     "weapons": [dict(w) if w else None for w in me.weapons],
                     "stats": dict(CFG["mech"])},
            "mechs": mechs,
            "projectiles": [{"id": b["id"], "kind": "gun", "pos": b["pos"], "vel": b["vel"]} for b in self.bullets
                            if b["owner"] != 0 and math.dist(b["pos"], me.pos) < 200],
            "crates": [c_ for c_ in self.crates.values() if self.sees_pt(me, c_["pos"])],
            "cubes": [c_ for c_ in self.cubes.values() if self.sees_pt(me, c_["pos"])],
            "pickups": [p for p in self.pickups.values() if self.sees_pt(me, p["pos"])],
            "sounds": [], "beams": [], "events": me.events, "drops": [],
            "zone": {"center": z[0], "radius": z[1], "next_center": z[2], "next_radius": z[3],
                     "shrink_start": z[4], "shrink_end": z[5], "dps": z[6]},
            "offer": None,
        }

    # ------------------------------------------------------------------ physics
    def move(self, m, mv, boost, dt):
        mx, my = mv
        L = math.hypot(mx, my)
        if L > 1:
            mx, my = mx / L, my / L
        sp = CFG["mech"]["walk_speed"]
        if boost and m.energy > 1 and L > 0.1:
            sp = CFG["mech"]["boost_speed"]
            m.energy -= CFG["mech"]["boost_drain"] * dt
            m.last_spend = self.t
        if m.charging > 0:
            sp *= 0.5
        tvx, tvy = mx * sp, my * sp
        a = CFG["mech"]["ground_accel"] * dt
        dvx, dvy = tvx - m.vel[0], tvy - m.vel[1]
        dl = math.hypot(dvx, dvy)
        if dl > a:
            dvx, dvy = dvx / dl * a, dvy / dl * a
        m.vel[0] += dvx
        m.vel[1] += dvy
        nx, ny = m.pos[0] + m.vel[0] * dt, m.pos[1] + m.vel[1] * dt
        if self.ok(nx, ny):
            m.pos[0], m.pos[1] = nx, ny
        elif self.ok(nx, m.pos[1]):
            m.pos[0] = nx
            m.vel[1] = 0
        elif self.ok(m.pos[0], ny):
            m.pos[1] = ny
            m.vel[0] = 0
        else:
            m.vel[0] = m.vel[1] = 0
        m.pos[2] = self.w.ground(m.pos[0], m.pos[1])
        if self.t - m.last_spend > CFG["mech"]["energy_regen_delay"]:
            m.energy = min(100, m.energy + CFG["mech"]["energy_regen"] * dt)

    def ok(self, x, y):
        w = self.w
        for ox, oy in ((0, 0), (2.4, 0), (-2.4, 0), (0, 2.4), (0, -2.4), (1.7, 1.7), (-1.7, 1.7), (1.7, -1.7), (-1.7, -1.7)):
            j, i = w.idx(x + ox, y + oy)
            if w.blocked[j, i]:
                return False
        return True

    def slew(self, m, dt):
        tr = CFG["mech"]["turn_rate"] * dt
        e = wrap(m.cmd_aim[0] - m.aim[0])
        m.aim[0] = wrap(m.aim[0] + max(-tr, min(tr, e)))
        pr = CFG["mech"]["pitch_rate"] * dt
        e = m.cmd_aim[1] - m.aim[1]
        m.aim[1] += max(-pr, min(pr, e))

    # ------------------------------------------------------------------ damage
    def damage(self, victim, amt, by, weapon, src_xy=None):
        if not victim.alive:
            return
        if src_xy is not None:
            b = math.atan2(src_xy[1] - victim.pos[1], src_xy[0] - victim.pos[0])
            rear = abs(wrap(b - victim.aim[0])) > math.pi / 2
        else:
            b, rear = None, False
        if rear:
            amt *= 1.5
        if by is not None:
            amt *= 1 + 0.04 * min(by.cubes, 10)
        victim.hp -= amt
        victim.last_dmg = self.t
        if by is not None:
            by.last_dmg = self.t
        if victim is self.me:
            self.me.events.append({"type": "damaged", "amount": amt, "weapon": weapon, "bearing": b,
                                   "by": by.id if by else None, "rear": rear})
            if by:
                self.dmg_by[by.diff] = self.dmg_by.get(by.diff, 0) + amt
        if by is self.me:
            self.hits += 1
            self.me.events.append({"type": "hit", "target": victim.id, "amount": amt, "weapon": weapon})
        if victim.hp <= 0:
            victim.alive = False
            victim.death_t = self.t
            if victim is self.me:
                self.my_place = sum(m.alive for m in self.mechs) + 1
            else:
                if by is self.me:
                    self.kills += 1
                    self.me.events.append({"type": "kill", "target": victim.id})
                for k in range(victim.cubes + 2):
                    self.cubes[self.cid] = {"id": self.cid, "pos": [victim.pos[0] + k, victim.pos[1], victim.pos[2]]}
                    self.cid += 1
                for w in victim.weapons:
                    if w and w["kind"] != "gun":
                        self.pickups[self.nid] = {"id": self.nid, "kind": w["kind"], "pos": list(victim.pos)}
                        self.nid += 1

    def fire(self, m, slot, trig):
        w = m.weapons[slot]
        if not w:
            return
        k = w["kind"]
        st = w["stats"]
        if k == "gun":
            if not trig or w["cooldown"] > 0 or w["reload"] > 0 or w["ammo"] <= 0:
                return
            w["ammo"] -= 1
            w["cooldown"] = st["cooldown"]
            if w["ammo"] == 0:
                w["reload"] = st["reload"]
            yaw = m.aim[0] + self.rng.gauss(0, math.radians(st["spread_deg"]))
            pit = m.aim[1] + self.rng.gauss(0, math.radians(st["spread_deg"]))
            d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
            o = [m.pos[0] + d[0] * 3, m.pos[1] + d[1] * 3, m.pos[2] + 7 + d[2] * 3]
            self.bullets.append({"id": len(self.bullets) + 100000 + self.tick * 100, "owner": m.id, "pos": o,
                                 "vel": [d[0] * st["speed"], d[1] * st["speed"], d[2] * st["speed"]],
                                 "left": st["range"], "dmg": st["damage"]})
            if m is self.me:
                self.shots += 1
        elif k == "laser":
            if trig:
                if w["cooldown"] <= 0:
                    w["charge"] = min(st["charge"], w["charge"] + 0.1)
                    m.charging = w["charge"]
            elif w["charge"] > 0:
                frac = st["min_frac"] + (1 - st["min_frac"]) * min(1, w["charge"] / st["charge"])
                self.hitscan(m, st["damage"] * frac, st["range"], "laser")
                w["charge"] = 0
                m.charging = 0
                w["cooldown"] = st["cooldown"]
        elif k == "lightning":
            if not trig or w["cooldown"] > 0 or m.energy < st["energy"]:
                return
            m.energy -= st["energy"]
            m.last_spend = self.t
            w["cooldown"] = st["cooldown"]
            best = None
            for o in self.mechs:
                if o is m or not o.alive:
                    continue
                if m is not self.me and o is not self.me:
                    continue
                dx, dy = o.pos[0] - m.pos[0], o.pos[1] - m.pos[1]
                d = math.hypot(dx, dy)
                if d < st["range"] and abs(wrap(math.atan2(dy, dx) - m.aim[0])) < math.radians(st["cone_deg"] / 2):
                    if best is None or d < best[0]:
                        best = (d, o)
            if best:
                self.damage(best[1], st["damage"], m, "lightning", m.pos)
        elif k == "mortar":
            pass

    def hitscan(self, m, dmg, rng, kind):
        d = (math.cos(m.aim[1]) * math.cos(m.aim[0]), math.cos(m.aim[1]) * math.sin(m.aim[0]), math.sin(m.aim[1]))
        o = (m.pos[0], m.pos[1], m.pos[2] + 7)
        best = None
        for t in self.mechs:
            if t is m or not t.alive:
                continue
            if m is not self.me and t is not self.me:
                continue
            rx, ry = t.pos[0] - o[0], t.pos[1] - o[1]
            s = rx * d[0] + ry * d[1]
            if s < 0 or s > rng:
                continue
            cx, cy = o[0] + d[0] * s, o[1] + d[1] * s
            cz = o[2] + d[2] * s / max(math.hypot(d[0], d[1]), 1e-6) * math.hypot(d[0], d[1])
            if math.hypot(cx - t.pos[0], cy - t.pos[1]) < 2.5 and t.pos[2] - 0.5 < cz < t.pos[2] + 9.5:
                if self.w.los(o, (cx, cy, cz)):
                    if best is None or s < best[0]:
                        best = (s, t)
        if best:
            self.damage(best[1], dmg, m, kind, m.pos)

    def step_bullets(self, dt):
        keep = []
        for b in self.bullets:
            p0 = list(b["pos"])
            p1 = [p0[i] + b["vel"][i] * dt for i in range(3)]
            hit = False
            for m in self.mechs:
                if not m.alive or m.id == b["owner"]:
                    continue
                if b["owner"] != 0 and m is not self.me:
                    continue
                # closest approach of segment to mech axis (2D) + height
                sx, sy = p1[0] - p0[0], p1[1] - p0[1]
                L2 = sx * sx + sy * sy
                u = 0 if L2 == 0 else max(0, min(1, ((m.pos[0] - p0[0]) * sx + (m.pos[1] - p0[1]) * sy) / L2))
                cx, cy, cz = p0[0] + sx * u, p0[1] + sy * u, p0[2] + (p1[2] - p0[2]) * u
                if math.hypot(cx - m.pos[0], cy - m.pos[1]) < 2.8 and m.pos[2] < cz < m.pos[2] + 9:
                    owner = self.mechs[b["owner"]]
                    self.damage(m, b["dmg"], owner, "gun", p0)
                    hit = True
                    break
            if not hit and b["owner"] == 0:
                for c in list(self.crates.values()):
                    sx, sy = p1[0] - p0[0], p1[1] - p0[1]
                    L2 = sx * sx + sy * sy or 1
                    u = max(0, min(1, ((c["pos"][0] - p0[0]) * sx + (c["pos"][1] - p0[1]) * sy) / L2))
                    cx, cy, cz = p0[0] + sx * u, p0[1] + sy * u, p0[2] + (p1[2] - p0[2]) * u
                    if math.hypot(c["pos"][0] - cx, c["pos"][1] - cy) < 1.6 and c["pos"][2] - 0.5 < cz < c["pos"][2] + 2.6:
                        self.me.events.append({"type": "hit", "target": c["id"], "amount": b["dmg"], "weapon": "gun"})
                        c["hp"] -= b["dmg"]
                        hit = True
                        if c["hp"] <= 0:
                            del self.crates[c["id"]]
                            self.cubes[self.cid] = {"id": self.cid, "pos": list(c["pos"])}
                            self.cid += 1
                            if self.rng.random() < 0.5:
                                self.pickups[self.nid] = {"id": self.nid, "kind": self.rng.choice(["laser", "lightning", "mortar"]),
                                                          "pos": [c["pos"][0] + 1, c["pos"][1], c["pos"][2]]}
                                self.nid += 1
                        break
            if hit:
                continue
            if not self.w.los(p0, p1, step=1.0):
                continue
            b["left"] -= math.dist(p0, p1)
            if b["left"] <= 0:
                continue
            b["pos"] = p1
            keep.append(b)
        self.bullets = keep

    # ------------------------------------------------------------------ house bots
    def n_hunters(self):
        if self.t > 300:
            return 15
        base = 1 + self.kills
        if self.t > 200:
            base += 3
        return base

    def house(self, m, dt):
        me = self.me
        noise, react, _ = DIFF[m.diff]
        living = [x for x in self.mechs[1:] if x.alive]
        order = sorted(living, key=lambda x: ["easy", "medium", "hard", "expert"].index(x.diff))
        m.hunting = m in order[: self.n_hunters()]
        c, r, _ = self.zone_now()
        _, _, nc, nr, _, _, _ = self.zone()
        squad_sees = any(self.sees(x, me) for x in living[:4]) if me.alive else False
        target_pos = me.pos if m.hunting and me.alive else None
        goal = None
        if math.dist(m.pos[:2], nc) > nr * 0.8:
            goal = nc
        if target_pos is not None:
            d = math.dist(m.pos[:2], target_pos[:2])
            if d > 60 or not self.w.los((m.pos[0], m.pos[1], m.pos[2] + 7), (me.pos[0], me.pos[1], me.pos[2] + 4.5)):
                goal = target_pos[:2]
        mv = (0.0, 0.0)
        if goal is not None:
            if m.path is None or self.t - m.path_t > 2:
                m.path = self.w.path(tuple(m.pos[:2]), tuple(goal), budget_s=0.004)
                m.path_t = self.t
            if m.path:
                while len(m.path) > 1 and math.dist(m.pos[:2], m.path[0]) < 4:
                    m.path.pop(0)
                q = m.path[0]
                dx, dy = q[0] - m.pos[0], q[1] - m.pos[1]
                L = math.hypot(dx, dy) or 1
                mv = (dx / L, dy / L)
        else:
            # strafe
            a = math.atan2(me.pos[1] - m.pos[1], me.pos[0] - m.pos[0]) + math.pi / 2
            s = 1 if int(self.t / 1.5 + m.id) % 2 else -1
            mv = (math.cos(a) * s, math.sin(a) * s)
        self.move(m, mv, False, dt)
        fire = [False, False]
        if me.alive and m.hunting and self.sees(m, me):
            src = (m.pos[0], m.pos[1], m.pos[2] + 7)
            q = (me.pos[0], me.pos[1], me.pos[2] + 4.5)
            d = math.dist(src, q)
            tt = d / 252
            q2 = (q[0] + me.vel[0] * tt * 0.9, q[1] + me.vel[1] * tt * 0.9, q[2])
            m.cmd_aim = [math.atan2(q2[1] - src[1], q2[0] - src[0]) + self.rng.gauss(0, noise),
                         math.atan2(q2[2] - src[2], math.hypot(q2[0] - src[0], q2[1] - src[1])) + self.rng.gauss(0, noise * 0.5)]
            if abs(wrap(m.cmd_aim[0] - m.aim[0])) < 0.05 and d < 220:
                fire[0] = True
                w1 = m.weapons[1]
                if w1 and w1["kind"] == "laser":
                    fire[1] = not (w1["charge"] >= w1["stats"]["charge"])
                elif w1 and w1["kind"] == "lightning":
                    fire[1] = d < 45
        else:
            if mv != (0.0, 0.0):
                m.cmd_aim = [math.atan2(mv[1], mv[0]), 0]
        self.slew(m, dt * 2)
        for i in (0, 1):
            self.fire(m, i, fire[i])
        if m.weapons[1] and m.weapons[1]["kind"] == "laser" and not fire[1] and m.weapons[1]["charge"] > 0:
            self.fire(m, 1, False)

    # ------------------------------------------------------------------ loop
    def run(self, max_t=480):
        dt = 0.1
        last_act = {}
        while self.t < max_t:
            alive = [m for m in self.mechs if m.alive]
            if len(alive) <= 1 or not self.me.alive:
                break
            obs = self.obs_for_me()
            self.me.events = []
            t0 = time.perf_counter()
            try:
                act = self.bot.act(obs)
            except Exception:
                import traceback
                traceback.print_exc()
                act = dict(last_act)
            ms = (time.perf_counter() - t0) * 1000
            self.max_ms = max(self.max_ms, ms)
            mf.draw.clear()
            last_act = act
            me = self.me
            if "aim" in act:
                me.cmd_aim = list(act["aim"])
            self.slew(me, dt)
            self.move(me, act.get("move", [0, 0]), act.get("boost", False), dt)
            fire = act.get("fire", [False, False])
            for i in (0, 1):
                self.fire(me, i, bool(fire[i]) if i < len(fire) else False)
            if "take" in act:
                p = self.pickups.get(act["take"])
                if p and math.dist(p["pos"][:2], me.pos[:2]) < 4:
                    slot = None
                    if p["kind"] in me.kinds():
                        pass
                    elif me.weapons[1] is None:
                        slot = 1
                    elif me.weapons[0] is None:
                        slot = 0
                    elif "drop" in act:
                        slot = act["drop"]
                        old = me.weapons[slot]
                        self.pickups[self.nid] = {"id": self.nid, "kind": old["kind"], "pos": list(me.pos)}
                        self.nid += 1
                    if slot is not None:
                        me.weapons[slot] = me.wobj(p["kind"])
                        me.weapons[slot]["cooldown"] = 1.0
                        del self.pickups[p["id"]]
                        me.events.append({"type": "took", "id": p["id"], "kind": p["kind"], "slot": slot})
            for m in self.mechs[1:]:
                if m.alive:
                    self.house(m, dt)
            self.step_bullets(dt)
            # cubes
            for c in list(self.cubes.values()):
                for m in self.mechs:
                    if m.alive and math.dist(c["pos"][:2], m.pos[:2]) < 3.5:
                        m.cubes += 1
                        m.max_hp *= 1.04 / (1 + 0.04 * (m.cubes - 1)) * (1 + 0.04 * (m.cubes - 1)) if m.cubes <= 10 else 1
                        m.hp = min(m.max_hp, m.hp + 56)
                        del self.cubes[c["id"]]
                        if m is self.me:
                            me.events.append({"type": "pickup", "cubes": m.cubes})
                        break
            # weapons timers, regen, zone
            c, r, dps = self.zone_now()
            for m in self.mechs:
                if not m.alive:
                    continue
                for w in m.weapons:
                    if not w:
                        continue
                    w["cooldown"] = max(0, w["cooldown"] - dt)
                    if w["kind"] == "gun" and w["reload"] > 0:
                        w["reload"] = max(0, w["reload"] - dt)
                        if w["reload"] == 0:
                            w["ammo"] = w["stats"]["mag"]
                if math.dist(m.pos[:2], c) > r:
                    m.hp -= dps * dt
                    m.last_dmg = self.t
                    if m is self.me:
                        me.events.append({"type": "damaged", "amount": dps * dt, "weapon": "zone", "bearing": None, "by": None})
                    if m.hp <= 0:
                        self.damage(m, 0.01, None, "zone")
                elif self.t - m.last_dmg > 5:
                    m.hp = min(m.max_hp, m.hp + 45 * dt)
            if act.get("reload") and me.weapons[0] and me.weapons[0]["kind"] == "gun" and me.weapons[0]["reload"] == 0:
                me.weapons[0]["reload"] = me.weapons[0]["stats"]["reload"]
            self.t = round(self.t + dt, 2)
            self.tick += 2
            self.advance_zone()
            if self.verbose and self.tick % 100 == 0:
                print(f"t={self.t:5.1f} hp={me.hp:6.0f} pos=({me.pos[0]:6.0f},{me.pos[1]:6.0f}) "
                      f"goal={self.bot.goal_name:14s} w={me.kinds()} cubes={me.cubes} kills={self.kills} "
                      f"inzone={math.dist(me.pos[:2], c) <= r} hunters={self.n_hunters()}")
        if self.my_place is None:
            alive = [m for m in self.mechs if m.alive]
            if not self.me.alive:
                self.my_place = len(alive) + 1
            else:
                better = sum(1 for m in alive if m is not self.me and m.hp > self.me.hp)
                self.my_place = better + 1
        return 16 - self.my_place


if __name__ == "__main__":
    import importlib
    modname = sys.argv[1] if len(sys.argv) > 1 else "bot"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    verbose = "-v" in sys.argv
    mod = importlib.import_module(modname)
    scores = []
    for s in range(n):
        sim = Sim(1000 + s, mod.Bot, verbose=verbose)
        sc = sim.run()
        scores.append(sc)
        print(f"seed {s}: score {sc} t={sim.t:.0f} kills={sim.kills} shots={sim.shots} hits={sim.hits} "
              f"w={sim.me.kinds()} cubes={sim.me.cubes} dmg_by={ {k: round(v) for k, v in sim.dmg_by.items()} } maxms={sim.max_ms:.1f}")
    print("mean", sum(scores) / len(scores))
