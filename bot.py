"""Survival-first mechfight bot: loot a laser, fight from cover, stay in the zone, regen behind walls."""
import math
import random
import time

import mechfight as mf

try:
    from world import World
except Exception:  # pragma: no cover
    World = None

# ---- tuning presets: change PRESET to test a variant on the real scorer -----------------
PRESETS = {
    "v1":    {"RANGE_LASER": 95.0, "RANGE_GUN": 70.0, "RELOAD_COVER": False},  # scored 5.04
    "close": {"RANGE_LASER": 60.0, "RANGE_GUN": 45.0, "RELOAD_COVER": False},
    "peek":  {"RANGE_LASER": 60.0, "RANGE_GUN": 45.0, "RELOAD_COVER": True},  # scored 5.27
    "roof":  {"RANGE_LASER": 60.0, "RANGE_GUN": 45.0, "RELOAD_COVER": True, "ROOF": True},  # sim: much worse
    "near":  {"RANGE_LASER": 40.0, "RANGE_GUN": 30.0, "RELOAD_COVER": True},  # scored 5.33
    "nearlead": {"RANGE_LASER": 40.0, "RANGE_GUN": 30.0, "RELOAD_COVER": True, "LEAD_FRAC": 0.7},  # scored 4.96
    "nearer": {"RANGE_LASER": 25.0, "RANGE_GUN": 20.0, "RELOAD_COVER": True},  # scored 5.17
    "nearaggro": {"RANGE_LASER": 40.0, "RANGE_GUN": 30.0, "RELOAD_COVER": True, "RESUME_AT": 0.7, "HEAL_BELOW": 0.5,
                  "RETREAT_AT": 0.35},  # scored 5.42
    "berserk": {"RANGE_LASER": 40.0, "RANGE_GUN": 30.0, "RELOAD_COVER": True, "RESUME_AT": 0.45, "HEAL_BELOW": 0.3,
                "RETREAT_AT": 0.2},
    "nearheal": {"RANGE_LASER": 40.0, "RANGE_GUN": 30.0, "RELOAD_COVER": True, "RESUME_AT": 0.95, "HEAL_BELOW": 0.92},  # scored 5.10
    "nearup": {"RANGE_LASER": 40.0, "RANGE_GUN": 30.0, "RELOAD_COVER": True, "UPGRADES": [
        "armor", "nanites", "plating", "gun_damage", "gun_rate", "laser_damage", "laser_charge",
        "lightning_efficiency", "gun_mag", "gyros"]},  # scored 4.96
    "lead":  {"RANGE_LASER": 60.0, "RANGE_GUN": 45.0, "RELOAD_COVER": True, "LEAD_FRAC": 0.7},
}
PRESET = "berserk"
# ------------------------------------------------------------------------------------------

TAU = 2 * math.pi
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}
UPGRADE_PRIORITY = [
    "plating", "armor", "nanites", "laser_damage", "laser_charge", "gun_damage", "gun_rate",
    "lightning_efficiency", "gun_mag", "gyros", "lightning_range", "mortar_reload", "gun_velocity",
    "servos", "capacitor", "reactor", "jets", "sensors", "laser_pierce", "lightning_chain",
    "mortar_splash", "mortar_velocity",
]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def norm2(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


class Bot:
    name = "mech-royale-bot"

    def __init__(self, hello):
        t0 = time.time()
        self.cfg = cfg = hello["config"]
        self.rng = random.Random(int(hello.get("seed", 0)))
        self.me_id = hello.get("slot")
        self.world = None
        if World is not None:
            try:
                self.world = World(hello["map"]["dir"], cfg)
            except Exception as e:  # keep playing without a map
                mf.log("map load failed:", repr(e))
        self.W = cfg["weapons"]
        self.mech = cfg["mech"]
        self.reach = cfg["loot"]["pickup_radius"]
        self.cube_r = cfg["cubes"]["pickup_radius"]
        # memories
        self.pickups = {}   # id -> (kind, pos, t_seen)
        self.crates = {}    # id -> (pos, hp, t_seen)
        self.cubes = {}     # id -> (pos, t_seen)
        self.npcs = {}      # id -> (pos, t_seen, hp)
        self.enemies = {}   # id -> dict(last obs) + "t"
        self.bad_targets = {}  # loot ids we failed to reach -> until t
        self.last_dmg_t = -99.0
        self.last_dmg_bearing = None
        self.last_dmg_by = None
        self.dmg_window = []  # (t, amount)
        self.look_bearing = None
        self.look_until = -1.0
        # movement state
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.stuck_t = 0.0
        self.unstick_until = -1.0
        self.unstick_dir = (1.0, 0.0)
        self.last_pos = None
        self.strafe = 1
        self.strafe_until = 0.0
        self.cover = None
        self.cover_t = -99.0
        self.goal_name = ""
        self.recovering = False
        self.RANGE_LASER = 95.0
        self.RANGE_GUN = 70.0
        self.RELOAD_COVER = False
        self.ROOF = False
        self.LEAD_FRAC = 1.0
        self.HEAL_BELOW = 0.75
        self.UPGRADES = None
        self.roof = None
        self.roof_key = None
        self.last_enemy_t = -99.0
        self.roof_fail = {}
        self.roof_try_t = -99.0
        self.LASER_BONUS = 75.0
        self.CRATE_RANGE_NOLASER = 160.0
        self.crate_shots = {}
        self.prog = None
        self.jet_until = -1.0
        self.jet_dir = (1.0, 0.0)
        self.want_jet = False
        self.want_jump = False
        self.crate_hit_t = {}
        self.RETREAT_AT = 0.45
        self.RESUME_AT = 0.85
        self.loot_goal = None
        self.loot_goal_since = 0.0
        for k, v in PRESETS.get(PRESET, {}).items():
            setattr(self, k, v)
        mf.log(f"init {time.time() - t0:.2f}s preset={PRESET} map={'ok' if self.world else 'none'}")

    # ================================================================ helpers
    def ground(self, x, y):
        return self.world.ground(x, y) if self.world else 0.0

    def los(self, a, b):
        return self.world.los(a, b) if self.world else True

    def zone_at(self, z, t):
        c0, r0 = z["center"], z["radius"]
        c1, r1 = z["next_center"], z["next_radius"]
        s0, s1 = z["shrink_start"], z["shrink_end"]
        if t <= s0 or s1 <= s0:
            return c0, r0
        f = min(1.0, (t - s0) / (s1 - s0))
        return [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f], r0 + (r1 - r0) * f

    def zone_slack(self, z, t, p, speed):
        """Seconds of free time before we must start walking to stay inside the zone."""
        c1, r1 = z["next_center"], z["next_radius"]
        need = max(0.0, math.dist(p[:2], c1) - r1 * 0.7)
        travel = need / max(speed, 1.0) * 1.35 + 3.0
        # when does the shrinking edge reach our current spot?
        t_hit = None
        for k in range(0, 41):
            tt = t + k * 2.0
            c, r = self.zone_at(z, tt)
            if math.dist(p[:2], c) > r - 4:
                t_hit = tt
                break
            if tt > z["shrink_end"]:
                break
        if t_hit is None:
            # safe through this phase: only the next phase matters
            return max(z["shrink_end"] - t, 0.0) + 20.0 - travel if need > 0 else 999.0
        return t_hit - t - travel

    def zone_goal(self, z, p):
        c1, r1 = z["next_center"], z["next_radius"]
        dx, dy = p[0] - c1[0], p[1] - c1[1]
        d = math.hypot(dx, dy)
        lim = r1 * 0.55
        if d <= lim:
            return (p[0], p[1])
        return (c1[0] + dx / d * lim, c1[1] + dy / d * lim)

    def inside_next(self, z, xy, frac=0.85):
        return math.dist(xy, z["next_center"]) <= z["next_radius"] * frac

    # ================================================================ movement
    def steer(self, p, ux, uy):
        """Adjust a desired unit direction so it doesn't run straight into a wall."""
        if not self.world or (ux == 0 and uy == 0):
            return ux, uy
        base = math.atan2(uy, ux)
        best = None
        for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6, 8):
            a = base + k * math.pi / 12
            cx, cy = math.cos(a), math.sin(a)
            free = self.world.clear_dist(p[0], p[1], cx, cy, 8.0)
            score = free - abs(k) * 0.9
            if free >= 7.9 and k == 0:
                return ux, uy
            if best is None or score > best[0]:
                best = (score, cx, cy)
        return best[1], best[2]

    def go_to(self, obs, goal, t):
        """Return unit move direction toward goal (x,y) using A* + line-of-walk smoothing."""
        p = obs["self"]["pos"]
        d = math.dist(p[:2], goal)
        if d < 1.5:
            return 0.0, 0.0
        # progress monitor: if we are not getting closer, jet straight at the goal
        if self.prog is None or math.dist(self.prog[0], goal) > 8:
            self.prog = (goal, t, d)
        elif t - self.prog[1] > 4.0:
            if d > self.prog[2] - 4.0 and d > 6:
                self.jet_until = t + 3.0
                self.jet_dir = norm2(goal[0] - p[0], goal[1] - p[1])
                self.path = None
            self.prog = (goal, t, d)
        if t < self.jet_until:
            self.want_jet = True
            return norm2(goal[0] - p[0], goal[1] - p[1])
        if not self.world:
            return norm2(goal[0] - p[0], goal[1] - p[1])
        w = self.world
        # direct if walkable
        if d < 60 and w.walk_line(p, goal):
            return norm2(goal[0] - p[0], goal[1] - p[1])
        replan = (self.path is None or self.path_goal is None or math.dist(self.path_goal, goal) > 6
                  or t - self.path_t > 1.5)
        if replan:
            self.path = w.path((p[0], p[1]), goal, budget_s=0.008)
            self.path_goal = goal
            self.path_t = t
        path = self.path
        if not path:
            return self.steer(p, *norm2(goal[0] - p[0], goal[1] - p[1]))
        if len(path) <= 2 and math.dist(path[-1], goal) > 10 and math.dist(path[-1], p[:2]) < 8:
            # boxed in (courtyard, stadium, roof): fly out toward the goal
            self.jet_until = t + 2.5
            self.want_jet = True
            self.path = None
            return norm2(goal[0] - p[0], goal[1] - p[1])
        # drop passed waypoints
        while len(path) > 1 and math.dist(p[:2], path[0]) < 3.0:
            path.pop(0)
        # farthest visible waypoint (lookahead up to 8 points)
        tgt = path[0]
        for k in range(min(len(path), 10) - 1, 0, -1):
            if math.dist(p[:2], path[k]) < 70 and w.walk_line(p, path[k]):
                tgt = path[k]
                break
        return self.steer(p, *norm2(tgt[0] - p[0], tgt[1] - p[1]))

    # ================================================================ aiming
    def aim_point(self, m):
        scale = 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * m.get("cubes", 0)
        return [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * scale]

    def muzzle(self, me):
        p = me["pos"]
        return [p[0], p[1], p[2] + me["stats"].get("muzzle_height", 7)]

    def lead(self, src, tgt, vel, speed, grounded_guess=True):
        px, py, pz = tgt
        vx, vy, vz = vel
        if grounded_guess:
            vz = 0.0
        tt = math.dist(src, tgt) / speed
        for _ in range(3):
            q = (px + vx * tt, py + vy * tt, pz + vz * tt)
            tt = math.dist(src, q) / speed
        return [px + vx * tt, py + vy * tt, pz + vz * tt]

    def yaw_pitch(self, src, q):
        dx, dy, dz = q[0] - src[0], q[1] - src[1], q[2] - src[2]
        return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

    def aim_ok(self, me, yaw, pitch, dist, tol=1.6):
        ay, ap = me["aim"]
        ey = abs(wrap(ay - yaw)) * dist
        ep = abs(ap - pitch) * dist
        return ey < tol and ep < tol * 1.8

    def mortar_solution(self, src, q, high=False):
        v = self.W["mortar"]["speed"]
        g = self.W["mortar"]["gravity"]
        dx, dy = q[0] - src[0], q[1] - src[1]
        d = math.hypot(dx, dy)
        h = q[2] - src[2]
        disc = v ** 4 - g * (g * d * d + 2 * h * v * v)
        if disc < 0 or d < 1:
            return None
        r = math.sqrt(disc)
        th = math.atan2(v * v + r if high else v * v - r, g * d)
        tof = d / (v * math.cos(th))
        return math.atan2(dy, dx), th, tof

    # ================================================================ memory
    def remember(self, obs):
        t = obs["t"]
        me = obs["self"]
        p = me["pos"]
        for pk in obs["pickups"]:
            self.pickups[pk["id"]] = (pk["kind"], pk["pos"], t)
        for c in obs["crates"]:
            self.crates[c["id"]] = (c["pos"], c.get("hp", 300), t)
        for c in obs["cubes"]:
            self.cubes[c["id"]] = (c["pos"], t)
        seen_p = {x["id"] for x in obs["pickups"]}
        seen_c = {x["id"] for x in obs["crates"]}
        seen_q = {x["id"] for x in obs["cubes"]}
        near = self.cfg["vision"]["near_range"] - 8
        for store, seen in ((self.pickups, seen_p), (self.crates, seen_c), (self.cubes, seen_q)):
            for k in list(store):
                v = store[k]
                pos = v[1] if store is self.pickups else v[0]
                if k not in seen and (math.dist(pos[:2], p[:2]) < near or t - v[-1] > 90):
                    del store[k]
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.last_enemy_t = t
                d = dict(m)
                d["t"] = t
                self.enemies[m["id"]] = d
            else:
                self.npcs[m["id"]] = (m["pos"], t, m.get("hp", 350))
        for k in list(self.enemies):
            if t - self.enemies[k]["t"] > (4 if k == -1 else 12):
                del self.enemies[k]
        for k in list(self.npcs):
            if t - self.npcs[k][1] > 30:
                del self.npcs[k]
        for e in obs["events"]:
            ty = e.get("type")
            if ty == "damaged":
                if e.get("weapon") != "zone":
                    self.last_dmg_t = t
                    self.dmg_window.append((t, e.get("amount", 0)))
                    if e.get("bearing") is not None:
                        self.last_dmg_bearing = e["bearing"]
                        self.last_dmg_by = e.get("by")
            elif ty == "hit":
                if 1000 <= (e.get("target") or 0) < 5000:
                    self.crate_hit_t[e["target"]] = t
                    self.crate_shots[e["target"]] = 0
            elif ty == "kill":
                self.enemies.pop(e.get("target"), None)
            elif ty == "take_failed":
                self.bad_targets[e.get("id")] = t + 20
        self.dmg_window = [x for x in self.dmg_window if t - x[0] < 3.0]

    # ================================================================ main
    def act(self, obs):
        t = obs["t"]
        me = obs["self"]
        p = me["pos"]
        z = obs["zone"]
        self.remember(obs)
        act = {"tick": obs["tick"]}
        dbg = []

        # ---- upgrades
        if obs.get("offer"):
            ch = obs["offer"]["choices"]
            pri = list(self.UPGRADES or []) + [u for u in UPGRADE_PRIORITY if u not in (self.UPGRADES or [])]
            best = min(range(len(ch)), key=lambda i: pri.index(ch[i]["id"]) if ch[i]["id"] in pri else 99)
            act["pick"] = best

        weapons = me["weapons"]
        kinds = {w["kind"]: i for i, w in enumerate(weapons) if w}
        hp_frac = me["hp"] / max(me["max_hp"], 1)
        speed = me["stats"].get("walk_speed", 11.7)

        # ---- loot pickup in reach (always opportunistic)
        self.try_take(obs, act, kinds, weapons)

        # ---- threats
        vis = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npc_vis = [m for m in obs["mechs"] if m["kind"] == "npc"]
        slack = self.zone_slack(z, t, p, speed)
        zone_urgent = (not me["in_zone"]) or slack < 0
        recent_dmg = t - self.last_dmg_t < 2.5

        move = (0.0, 0.0)
        boost = False
        aim = None
        fire = [False, False]
        target = None

        if vis:
            target = self.pick_target(me, vis)
        elif recent_dmg and self.last_dmg_by is not None and self.last_dmg_by >= 100:
            # NPC shooting us
            cands = [m for m in npc_vis]
            if cands:
                target = min(cands, key=lambda m: math.dist(m["pos"], p))

        unseen_fire = (not vis and recent_dmg and self.last_dmg_by is not None and self.last_dmg_by < 100
                       and self.last_dmg_bearing is not None)
        roof_ctl = None
        if self.ROOF and self.world is not None:
            roof_ctl = self.roof_control(obs, me, target, vis, zone_urgent, hp_frac, t)
        # ---------------------------------------------------------- decide movement
        if roof_ctl is not None:
            move, boost, rjump = roof_ctl
            if rjump:
                self.want_jump = True
            if target is None:
                target = self.crate_or_npc_target(obs, me, kinds, hp_frac) if not vis else None
        elif unseen_fire:
            move, boost = self.evade_unseen(obs, me, zone_urgent, t)
            target = None
        elif target is not None and target["kind"] == "mech":
            move, boost = self.combat_move(obs, me, target, vis, zone_urgent, hp_frac, t, dbg)
        else:
            move, boost = self.explore_move(obs, me, zone_urgent, slack, hp_frac, t, kinds, dbg)
            if target is None:
                target = self.crate_or_npc_target(obs, me, kinds, hp_frac)

        # ---------------------------------------------------------- dodge incoming
        dmove = self.dodge(obs, me) if roof_ctl is None else None
        if dmove is not None:
            move = (move[0] * 0.3 + dmove[0], move[1] * 0.3 + dmove[1])
            move = norm2(*move)
            boost = boost or me["energy"] > 30

        # ---------------------------------------------------------- weapons
        if target is not None:
            aim, fire = self.shoot(obs, me, target, kinds, weapons, dbg)
            if target.get("kind") == "crate" and fire[kinds.get("gun", 0)]:
                cid = target["id"]
                self.crate_shots[cid] = self.crate_shots.get(cid, 0) + 1
                if self.crate_shots[cid] > 14:
                    self.bad_targets[cid] = t + 40
                    self.crate_shots[cid] = 0
        else:
            # release a laser charge if we lost the target, otherwise hold nothing
            aim = self.look_dir(obs, me, move, t)

        # ---------------------------------------------------------- unstick
        if roof_ctl is None:
            move, jump, boost = self.unstick(me, move, boost, t)
        else:
            jump = self.want_jump
        self.want_jump = False
        if self.want_jet:
            if me["grounded"]:
                jump = True
            boost = me["energy"] > 3 or not me["grounded"]
        self.want_jet = False

        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        act["aim"] = [round(aim[0], 4), round(max(-0.69, min(1.29, aim[1])), 4)]
        act["fire"] = fire
        # reload when idle
        gi = kinds.get("gun")
        if gi is not None and not any(fire):
            g = weapons[gi]
            if g.get("reload", 0) == 0 and g.get("ammo", 99) < g["stats"].get("mag", 12) * 0.5 and not vis:
                act["reload"] = True
        mf.draw.label(f"{self.goal_name} hp{me['hp']:.0f} s{slack:.0f}")
        return act

    # ================================================================ loot
    def want_kind(self, kind, kinds):
        """Return slot to fill (0/1) and whether a drop is needed, or None if we don't want it."""
        if kind in kinds or kind == "gun":
            return None
        if len(kinds) < 2:
            return ("empty", None)
        # both full: swap out the worst non-gun weapon if this one is better
        worst = None
        for k, i in kinds.items():
            if k == "gun":
                continue
            if worst is None or WEAPON_RANK[k] < WEAPON_RANK[worst[0]]:
                worst = (k, i)
        if worst and WEAPON_RANK[kind] > WEAPON_RANK[worst[0]]:
            return ("swap", worst[1])
        return None

    def try_take(self, obs, act, kinds, weapons):
        p = obs["self"]["pos"]
        best = None
        for pk in obs["pickups"]:
            if math.hypot(pk["pos"][0] - p[0], pk["pos"][1] - p[1]) > self.reach - 0.3:
                continue
            if abs(pk["pos"][2] - p[2]) > 3.8:
                continue
            w = self.want_kind(pk["kind"], kinds)
            if not w:
                continue
            if w[0] == "swap":
                wi = weapons[w[1]]
                if wi and wi.get("charge", 0) > 0:
                    continue
            if best is None or WEAPON_RANK[pk["kind"]] > WEAPON_RANK[best[0]["kind"]]:
                best = (pk, w)
        if best:
            act["take"] = best[0]["id"]
            if best[1][0] == "swap":
                act["drop"] = best[1][1]

    # ================================================================ targets
    def pick_target(self, me, vis):
        p = me["pos"]

        def score(m):
            d = math.dist(p, m["pos"])
            hp = m.get("hp", 1400)
            s = hp / 1400.0 * 100 + d * 0.5
            if not self.los(self.muzzle(me), self.aim_point(m)):
                s += 120
            if m.get("charging"):
                s -= 20
            return s

        return min(vis, key=score)

    def crate_or_npc_target(self, obs, me, kinds, hp_frac):
        """Shoot crates (and drones when healthy) for cubes and loot."""
        p = me["pos"]
        src = self.muzzle(me)
        best = None
        t = obs["t"]
        for c in obs["crates"]:
            d = math.dist(c["pos"], p)
            if d > 110 or self.bad_targets.get(c["id"], -1) > t:
                continue
            q = [c["pos"][0], c["pos"][1], c["pos"][2] + 1.2]
            if not self.los(src, q):
                continue
            if best is None or d < best[0]:
                best = (d, {"kind": "crate", "id": c["id"], "pos": c["pos"], "vel": [0, 0, 0], "aimz": 1.2})
        if best:
            return best[1]
        if hp_frac > 0.7 and obs["t"] < 330:
            for n in obs["mechs"]:
                if n["kind"] != "npc":
                    continue
                d = math.dist(n["pos"], p)
                if d > 120:
                    continue
                q = [n["pos"][0], n["pos"][1], n["pos"][2] + 1.75]
                if not self.los(src, q):
                    continue
                if best is None or d < best[0]:
                    best = (d, dict(n, aimz=1.75))
            if best:
                return best[1]
        return None

    # ================================================================ shooting
    def shoot(self, obs, me, target, kinds, weapons, dbg):
        src = self.muzzle(me)
        tp = target["pos"]
        is_mech = target.get("kind") == "mech"
        if is_mech:
            q0 = self.aim_point(target)
        else:
            q0 = [tp[0], tp[1], tp[2] + target.get("aimz", 1.5)]
        dist = math.dist(src, q0)
        vel = target.get("vel", [0, 0, 0])
        fire = [False, False]
        has_los = self.los(src, q0)
        tol = 1.8 if is_mech else 1.0
        cands = {}  # weapon -> (yaw, pitch)

        # gun solution
        gi = kinds.get("gun")
        gun_ready = False
        if gi is not None:
            gw = weapons[gi]
            st = gw["stats"]
            lv = [vel[0] * self.LEAD_FRAC, vel[1] * self.LEAD_FRAC, vel[2] * self.LEAD_FRAC]
            q = self.lead(src, q0, lv, st.get("speed", 252), grounded_guess=abs(vel[2]) < 2)
            cands["gun"] = self.yaw_pitch(src, q)
            gun_ready = (dist < st.get("range", 230) * 0.98 and has_los and gw.get("ammo", 1) > 0
                         and gw.get("reload", 0) == 0)

        # laser solution (hitscan)
        li = kinds.get("laser")
        laser_frac = 0.0
        if li is not None:
            lw = weapons[li]
            q = [q0[0] + vel[0] * 0.05, q0[1] + vel[1] * 0.05, q0[2]]
            cands["laser"] = self.yaw_pitch(src, q)
            laser_frac = lw.get("charge", 0) / max(lw["stats"].get("charge", 0.75), 1e-3)

        # mortar solution
        mi = kinds.get("mortar")
        msol = None
        if mi is not None and is_mech:
            tube = [me["pos"][0], me["pos"][1], me["pos"][2] + me["stats"].get("height", 9) + 0.5]
            hd = math.dist(tube[:2], tp[:2])
            if 30 < hd < 225:
                sol = self.mortar_solution(tube, [tp[0], tp[1], tp[2] + 1.0], high=not has_los)
                if sol:
                    tof = sol[2]
                    sol = self.mortar_solution(tube, [tp[0] + vel[0] * tof * 0.8, tp[1] + vel[1] * tof * 0.8,
                                                      tp[2] + 1.0], high=not has_los)
                if sol:
                    msol = sol
                    cands["mortar"] = (sol[0], sol[1])

        # choose what the torso aims for this tick
        mortar_ready = msol is not None and weapons[mi].get("cooldown", 0) <= 0.3
        if "laser" in cands and laser_frac >= 0.6 and has_los:
            mode = "laser"
        elif gun_ready:
            mode = "gun"
        elif mortar_ready and (not has_los or not gun_ready):
            mode = "mortar"
        elif "laser" in cands and has_los:
            mode = "laser"
        elif "gun" in cands:
            mode = "gun"
        elif cands:
            mode = next(iter(cands))
        else:
            mode = None
        aim = cands[mode] if mode else self.yaw_pitch(src, q0)

        # laser trigger
        if li is not None:
            lw = weapons[li]
            st = lw["stats"]
            if has_los and (is_mech or target.get("kind") == "npc") and dist < st.get("range", 280) * 0.97:
                yaw, pitch = cands["laser"]
                if lw.get("charge", 0) > 0:
                    fire[li] = not (laser_frac >= 0.97 and self.aim_ok(me, yaw, pitch, dist, 1.3))
                elif lw.get("cooldown", 0) <= 0.1:
                    fire[li] = True
            elif lw.get("charge", 0) > 0:
                fire[li] = True  # keep holding; it auto-fires after hold_s

        # lightning: wide cone, fire whenever in reach
        ni = kinds.get("lightning")
        if ni is not None:
            st = weapons[ni]["stats"]
            yaw, _ = self.yaw_pitch(src, q0)
            cone = math.radians(st.get("cone_deg", 30)) * 0.5
            if (dist < st.get("range", 45) * 0.95 and has_los and abs(wrap(me["aim"][0] - yaw)) < cone * 0.8
                    and me["energy"] > 12 and weapons[ni].get("cooldown", 0) <= 0.05):
                fire[ni] = True

        # mortar trigger
        if msol is not None and weapons[mi].get("cooldown", 0) <= 0.05:
            my_, mp_, _ = msol
            hd = math.dist(me["pos"][:2], tp[:2])
            if abs(wrap(me["aim"][0] - my_)) * hd < 3 and abs(me["aim"][1] - mp_) < 0.03:
                fire[mi] = True

        # gun trigger
        if gi is not None and gun_ready:
            yaw, pitch = cands["gun"]
            if self.aim_ok(me, yaw, pitch, dist, tol):
                fire[gi] = True
        if is_mech:
            mf.draw.line(src, q0, "#ff5040")
        return aim, fire

    # ================================================================ combat movement
    def combat_move(self, obs, me, target, vis, zone_urgent, hp_frac, t, dbg):
        p = me["pos"]
        z = obs["zone"]
        tp = target["pos"]
        dx, dy = tp[0] - p[0], tp[1] - p[1]
        d = math.hypot(dx, dy)
        ux, uy = norm2(dx, dy)
        kinds = {w["kind"] for w in me["weapons"] if w}
        n_threat = sum(1 for m in vis if math.dist(m["pos"], p) < 220)
        my_hp = me["hp"]
        their_hp = target.get("hp", 1400)
        if hp_frac < self.RETREAT_AT and (their_hp > my_hp * 0.7 or n_threat > 1):
            self.recovering = True
        if n_threat >= 3 and hp_frac < 0.6:
            self.recovering = True
        if hp_frac >= self.RESUME_AT:
            self.recovering = False
        # finish off a nearly dead target instead of running
        if their_hp < 250 and my_hp > their_hp * 1.5 and n_threat == 1:
            self.recovering = False
        losing = self.recovering
        if self.RELOAD_COVER and not losing:
            ws = {w["kind"]: w for w in me["weapons"] if w}
            g = ws.get("gun")
            reloading = g is not None and (g.get("reload", 0) > 0.6 or g.get("ammo", 1) == 0)
            lz = ws.get("laser")
            laser_ready = lz is not None and lz.get("cooldown", 0) < 0.3
            lightning_ready = "lightning" in ws and d < 45 and me["energy"] > 20
            if reloading and not laser_ready and not lightning_ready and their_hp > 200:
                cov = self.find_cover(obs, me, vis, t)
                if cov is not None and math.dist(cov, p[:2]) < 30:
                    self.goal_name = "reload-cover"
                    return self.go_to(obs, cov, t), me["energy"] > 20
        if zone_urgent:
            self.goal_name = "zone(fight)"
            mv = self.go_to(obs, self.zone_goal(z, p), t)
            return mv, me["energy"] > 25
        if losing:
            cov = self.find_cover(obs, me, vis, t)
            if cov is not None:
                self.goal_name = "cover"
                mv = self.go_to(obs, cov, t)
                return mv, me["energy"] > 20
        # preferred range
        if "lightning" in kinds and "laser" not in kinds and "mortar" not in (target.get("weapons") or []):
            want = 28.0
        elif "laser" in kinds:
            want = self.RANGE_LASER
        else:
            want = self.RANGE_GUN
        if hp_frac < 0.5:
            want += 40
        radial = 0.0
        if d > want + 25:
            radial = 0.7
        elif d < want - 20:
            radial = -0.7
        if t > self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
            self.strafe_until = t + self.rng.uniform(0.6, 1.6)
        sx, sy = -uy * self.strafe, ux * self.strafe
        mx, my = ux * radial + sx * 0.8, uy * radial + sy * 0.8
        # don't leave the next zone circle
        nxp = (p[0] + mx * 15, p[1] + my * 15)
        if not self.inside_next(z, nxp, 0.95) and not self.inside_next(z, p[:2], 0.95):
            gx, gy = self.zone_goal(z, p)
            mx, my = norm2(gx - p[0], gy - p[1])
        laser_charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in me["weapons"])
        grab = self.nearby_loot(me, kinds_held=self.kinds_of(me), maxd=40.0)
        if grab is not None and hp_frac > 0.25:
            mv = self.go_to(obs, grab, t)
            self.goal_name = "grab"
            mx, my = self.steer(p, *norm2(mv[0] + sx * 0.25, mv[1] + sy * 0.25))
            return (mx, my), me["energy"] > 40
        if radial > 0 and d > want + 25:
            # close distance with pathing
            mv = self.go_to(obs, (tp[0], tp[1]), t)
            mx, my = mv[0] * 0.8 + sx * 0.3, mv[1] * 0.8 + sy * 0.3
        mx, my = self.steer(p, *norm2(mx, my))
        self.goal_name = f"fight {target['id']}"
        enemy_charging = any(m.get("charging") for m in vis)
        return (mx, my), (enemy_charging and me["energy"] > 25 and not laser_charging)

    def evade_unseen(self, obs, me, zone_urgent, t):
        p = me["pos"]
        b = self.last_dmg_bearing
        if zone_urgent:
            self.goal_name = "zone(shot)"
            return self.go_to(obs, self.zone_goal(obs["zone"], p), t), me["energy"] > 25
        # pretend the shooter is 90 m away along the bearing and hide from it
        fake = {"pos": [p[0] + math.cos(b) * 90, p[1] + math.sin(b) * 90, p[2]], "t": t}
        self.enemies.setdefault(-1, fake).update(fake)
        cov = self.find_cover(obs, me, [fake], t)
        self.goal_name = "shot-unseen"
        if cov is not None:
            return self.go_to(obs, cov, t), me["energy"] > 30
        if t > self.strafe_until:
            self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(0.5, 1.2)
        return self.steer(p, -math.sin(b) * self.strafe, math.cos(b) * self.strafe), me["energy"] > 30

    # ================================================================ rooftop play
    def choose_roof(self, obs, me, t):
        z = obs["zone"]
        c1, r1 = z["next_center"], z["next_radius"]
        p = me["pos"]
        best = None
        for k, rf in enumerate(self.world.roofs):
            if self.roof_fail.get(k, -1) > t:
                continue
            if rf["h"] < 8 or rf["h"] > 32:
                continue
            if math.dist((rf["x"], rf["y"]), c1) > r1 * 0.8 - rf["r"]:
                continue
            d = math.dist((rf["x"], rf["y"]), p[:2])
            if d > 260:
                continue
            sc = d + rf["h"] * 1.5 - min(rf["r"], 20) * 1.5
            if best is None or sc < best[0]:
                best = (sc, k)
        return best[1] if best else None

    def on_roof(self, me):
        if self.roof is None:
            return False
        rf = self.world.roofs[self.roof]
        p = me["pos"]
        return abs(p[2] - rf["z"]) < 2.0 and math.dist(p[:2], (rf["x"], rf["y"])) < rf["r"] + 2

    def roof_control(self, obs, me, target, vis, zone_urgent, hp_frac, t):
        """Return (move, boost, jump) while using a roof, or None to fall back to normal play."""
        p = me["pos"]
        z = obs["zone"]
        threat = (t - self.last_enemy_t < 25) or (t - self.last_dmg_t < 8 and (self.last_dmg_by or 0) < 100)
        key = (round(z["next_center"][0]), round(z["next_center"][1]), round(z["next_radius"]))
        if self.roof_key != key:
            self.roof_key = key
            self.roof = None
        if zone_urgent:
            return None
        up = self.on_roof(me)
        if not threat and not up:
            return None
        if self.roof is None:
            self.roof = self.choose_roof(obs, me, t)
            self.roof_try_t = t
            if self.roof is None:
                return None
        rf = self.world.roofs[self.roof]
        cx, cy = rf["x"], rf["y"]
        R = max(2.0, rf["r"] - 4.0)
        if up:
            self.roof_try_t = t
            ws = {w["kind"]: w for w in me["weapons"] if w}
            g = ws.get("gun")
            reloading = g is not None and (g.get("reload", 0) > 0.6 or g.get("ammo", 1) == 0)
            lz = ws.get("laser")
            laser_ready = lz is not None and lz.get("cooldown", 0) < 0.3
            hide = hp_frac < 0.45 or (reloading and not laser_ready)
            if target is not None and target.get("kind") == "mech" and not hide:
                tp = target["pos"]
                ux, uy = norm2(tp[0] - cx, tp[1] - cy)
                src = self.muzzle(me)
                if self.los(src, self.aim_point(target)):
                    # hold position, small strafe along the edge
                    if t > self.strafe_until:
                        self.strafe = -self.strafe
                        self.strafe_until = t + self.rng.uniform(0.6, 1.4)
                    gx, gy = p[0] - uy * self.strafe * 3, p[1] + ux * self.strafe * 3
                else:
                    gx, gy = cx + ux * R, cy + uy * R
                self.goal_name = "roof-fight"
            else:
                gx, gy = cx, cy
                self.goal_name = "roof-hide"
            # stay on the roof
            ox, oy = gx - cx, gy - cy
            od = math.hypot(ox, oy)
            if od > R:
                gx, gy = cx + ox / od * R, cy + oy / od * R
            dx, dy = gx - p[0], gy - p[1]
            dd = math.hypot(dx, dy)
            if dd < 1.0:
                return (0.0, 0.0), False, False
            sp = min(1.0, dd / 4.0)
            return (dx / dd * sp, dy / dd * sp), False, False
        # not up yet: give up on roofs we can't reach in time
        if t - self.roof_try_t > 25:
            self.roof_fail[self.roof] = t + 60
            self.roof = None
            return None
        if hp_frac < 0.2:
            return None
        d = math.dist(p[:2], (cx, cy))
        need = min(95.0, (rf["h"] + 4) * 1.7)
        airborne = not me["grounded"]
        if airborne and p[2] > rf["z"] - 25:
            self.goal_name = "roof-climb"
            boost = p[2] < rf["z"] + 1.5 and me["energy"] > 1
            return norm2(cx - p[0], cy - p[1]), boost, False
        if d > rf["r"] + 9:
            self.goal_name = "roof-go"
            mv = self.go_to(obs, (cx, cy), t)
            self.want_jet = False
            return mv, False, False
        if me["energy"] >= need:
            self.goal_name = "roof-jump"
            return norm2(cx - p[0], cy - p[1]), False, True
        self.goal_name = "roof-wait"
        return (0.0, 0.0), False, False

    def kinds_of(self, me):
        return {w["kind"]: i for i, w in enumerate(me["weapons"]) if w}

    def nearby_loot(self, me, kinds_held, maxd):
        p = me["pos"]
        best = None
        for cid, (pos, ts) in self.cubes.items():
            if abs(pos[2] - p[2]) > 3.5:
                continue
            d = math.dist(pos[:2], p[:2])
            if d < maxd and (best is None or d < best[0]):
                best = (d, (pos[0], pos[1]))
        for pid, (kind, pos, ts) in self.pickups.items():
            if abs(pos[2] - p[2]) > 3.5 or not self.want_kind(kind, kinds_held):
                continue
            d = math.dist(pos[:2], p[:2]) - 10
            if d < maxd and (best is None or d < best[0]):
                best = (d, (pos[0], pos[1]))
        return best[1] if best else None

    def find_cover(self, obs, me, vis, t):
        if not self.world:
            return None
        p = me["pos"]
        if self.cover is not None and t - self.cover_t < 1.2:
            return self.cover
        z = obs["zone"]
        threats = [m for m in self.enemies.values() if t - m["t"] < 6]
        if not threats:
            return None
        threats = sorted(threats, key=lambda m: math.dist(m["pos"], p))[:3]
        t_stop = time.perf_counter() + 0.012
        eyes = [(m["pos"][0], m["pos"][1], m["pos"][2] + 8) for m in threats]
        best = None
        for r in (10, 18, 28, 40):
            if time.perf_counter() > t_stop:
                break
            for k in range(16):
                a = k * TAU / 16
                cx, cy = p[0] + math.cos(a) * r, p[1] + math.sin(a) * r
                if not self.world.is_free(cx, cy):
                    continue
                if not self.inside_next(z, (cx, cy), 0.95) and not self.inside_next(z, p[:2], 0.95):
                    continue
                gz = self.ground(cx, cy)
                hidden = True
                for e in eyes:
                    if self.world.los(e, (cx, cy, gz + 5.0)) or self.world.los(e, (cx, cy, gz + 8.5)):
                        hidden = False
                        break
                if not hidden:
                    continue
                score = r
                # prefer moving away from threats
                for m in threats:
                    ex, ey = m["pos"][0] - p[0], m["pos"][1] - p[1]
                    if math.cos(a) * ex + math.sin(a) * ey > 0:
                        score += 15
                if not self.world.walk_line(p, (cx, cy)):
                    score += 12
                if best is None or score < best[0]:
                    best = (score, (cx, cy))
            if best is not None and best[0] < r + 5:
                break
        self.cover = best[1] if best else None
        self.cover_t = t
        return self.cover

    # ================================================================ exploring
    def explore_move(self, obs, me, zone_urgent, slack, hp_frac, t, kinds, dbg):
        p = me["pos"]
        z = obs["zone"]
        if zone_urgent:
            self.goal_name = "zone"
            return self.go_to(obs, self.zone_goal(z, p), t), (not me["in_zone"]) or slack < -5

        # hiding to regen after being hurt
        threats = [m for m in self.enemies.values() if t - m["t"] < 6]
        if hp_frac >= self.RESUME_AT:
            self.recovering = False
        if (self.recovering or hp_frac < self.RETREAT_AT) and threats:
            cov = self.find_cover(obs, me, threats, t)
            if cov is not None:
                self.goal_name = "hide"
                return self.go_to(obs, cov, t), False

        # after a fight: collect what's close, then sit still out of sight and regen
        if hp_frac < self.HEAL_BELOW and 2.5 < t - self.last_dmg_t < 30:
            grab = self.nearby_loot(me, kinds, maxd=35.0)
            if grab is not None:
                self.goal_name = "grab"
                return self.go_to(obs, grab, t), False
            recent = [m for m in self.enemies.values() if t - m["t"] < 15]
            if recent:
                cov = self.find_cover(obs, me, recent, t)
                if cov is not None and math.dist(cov, p[:2]) > 2:
                    self.goal_name = "heal-cover"
                    return self.go_to(obs, cov, t), False
            self.goal_name = "heal"
            return (0.0, 0.0), False
        goal = None
        budget = slack - 5
        # 1. weapons we want
        cands = []
        for pid, (kind, pos, ts) in self.pickups.items():
            if self.bad_targets.get(pid, -1) > t:
                continue
            w = self.want_kind(kind, kinds)
            if not w:
                continue
            if pos[2] - self.ground(pos[0], pos[1]) > 3:
                continue  # roof loot: skip
            d = math.dist(pos[:2], p[:2])
            if d / 11 > budget:
                continue
            if not self.inside_next(z, pos[:2], 1.0) and d > 40 and kind != "laser":
                continue
            bonus = self.LASER_BONUS if kind == "laser" else WEAPON_RANK[kind] * 25
            cands.append((d - bonus, ("weapon", pid, pos)))
        # 2. cubes
        for cid, (pos, ts) in self.cubes.items():
            if self.bad_targets.get(cid, -1) > t:
                continue
            if pos[2] - self.ground(pos[0], pos[1]) > 3:
                continue
            d = math.dist(pos[:2], p[:2])
            if d / 11 > budget or d > 150:
                continue
            cands.append((d - 30, ("cube", cid, pos)))
        # 3. supply drops
        for dr in obs.get("drops", []):
            pos = dr["pos"]
            d = math.dist(pos[:2], p[:2])
            if d > 220 or hp_frac < 0.3:
                continue
            others_close = any(math.dist(m["pos"][:2], pos[:2]) < 60 for m in self.enemies.values() if t - m["t"] < 5)
            if others_close:
                continue
            eta = d / 11
            if eta + 2 < dr.get("lands_in", 0):
                # get close but not too early
                cands.append((d + 40, ("dropwait", dr["id"], pos)))
            else:
                cands.append((d - 60, ("drop", dr["id"], pos)))
        # 4. crates: go within shooting range of remembered crates
        for cid, (pos, hp, ts) in self.crates.items():
            if pos[2] - self.ground(pos[0], pos[1]) > 3:
                continue
            d = math.dist(pos[:2], p[:2])
            crange = 160 if "laser" in kinds else self.CRATE_RANGE_NOLASER
            if d > crange or d / 11 > budget:
                continue
            cands.append((d + 10, ("crate", cid, pos)))
        if cands:
            cands.sort(key=lambda c: c[0])
            kind, gid, pos = cands[0][1]
            # stick to a goal for a while; give up after 25 s
            if self.loot_goal != gid:
                self.loot_goal = gid
                self.loot_goal_since = t
            elif t - self.loot_goal_since > 25:
                self.bad_targets[gid] = t + 40
            if kind == "crate" and math.dist(pos[:2], p[:2]) < 50:
                goal = None
                self.goal_name = "crate"
                return (0.0, 0.0), False
            goal = (pos[0], pos[1])
            self.goal_name = kind
        if goal is None:
            # roam: drift toward a point inside the next circle, wander slowly
            zg = self.zone_goal(z, p)
            c1 = z["next_center"]
            if math.dist(p[:2], c1) > z["next_radius"] * 0.5:
                goal = zg
            else:
                ang = (t * 0.05 + (self.me_id or 0)) % TAU
                rr = z["next_radius"] * 0.35
                goal = (c1[0] + math.cos(ang) * rr, c1[1] + math.sin(ang) * rr)
            self.goal_name = "roam"
        return self.go_to(obs, goal, t), False

    # ================================================================ looking around
    def look_dir(self, obs, me, move, t):
        p = me["pos"]
        # damage from something we can't see: look there
        if t - self.last_dmg_t < 2.0 and self.last_dmg_bearing is not None:
            return (self.last_dmg_bearing, 0.0)
        # loud sounds nearby
        best = None
        for s in obs["sounds"]:
            if s["kind"] in ("step", "boost", "jet", "drop", "death", "crate", "npc_gun"):
                continue
            pr = {"near": 0, "mid": 1, "far": 2}.get(s["dist"], 2)
            if best is None or pr < best[0]:
                best = (pr, s["bearing"])
        if best is not None and best[0] <= 1:
            self.look_bearing = best[1]
            self.look_until = t + 1.5
        for s in obs["sounds"]:
            if s["kind"] in ("step", "boost", "jet") and s["dist"] == "near":
                self.look_bearing = s["bearing"]
                self.look_until = t + 1.2
        if t < self.look_until and self.look_bearing is not None:
            return (self.look_bearing, 0.0)
        # recently seen enemy: face it
        recent = [m for m in self.enemies.values() if t - m["t"] < 8]
        if recent:
            m = min(recent, key=lambda m: math.dist(m["pos"], p))
            return (math.atan2(m["pos"][1] - p[1], m["pos"][0] - p[0]), 0.0)
        base = math.atan2(move[1], move[0]) if (move[0] or move[1]) else me["aim"][0]
        sweep = math.sin(t * 1.3) * 1.2
        return (wrap(base + sweep), 0.0)

    # ================================================================ dodging
    def dodge(self, obs, me):
        p = me["pos"]
        c = (p[0], p[1], p[2] + 4.5)
        out = None
        for pr in obs["projectiles"]:
            q, v = pr["pos"], pr["vel"]
            if pr["kind"] == "mortar":
                # predict landing near our height
                g = self.W["mortar"]["gravity"]
                a = -0.5 * g
                b = v[2]
                cc = q[2] - p[2]
                disc = b * b - 4 * a * cc
                if disc < 0:
                    continue
                tl = (-b - math.sqrt(disc)) / (2 * a)
                if tl < 0 or tl > 5:
                    continue
                lx, ly = q[0] + v[0] * tl, q[1] + v[1] * tl
                dd = math.hypot(lx - p[0], ly - p[1])
                if dd < self.W["mortar"].get("splash_radius", 7) * 1.4 + 3:
                    ax, ay = norm2(p[0] - lx, p[1] - ly)
                    if ax == 0 and ay == 0:
                        ax, ay = norm2(-v[1], v[0])
                    out = (ax, ay)
                continue
            rx, ry, rz = c[0] - q[0], c[1] - q[1], c[2] - q[2]
            vv = v[0] ** 2 + v[1] ** 2 + v[2] ** 2
            if vv < 1:
                continue
            tca = (rx * v[0] + ry * v[1] + rz * v[2]) / vv
            if tca < 0 or tca > 0.7:
                continue
            mx = q[0] + v[0] * tca - c[0]
            my = q[1] + v[1] * tca - c[1]
            if math.hypot(mx, my) < 3.2:
                # step perpendicular to the bullet, away from its closest approach
                px, py = norm2(-v[1], v[0])
                if px * mx + py * my > 0:
                    px, py = -px, -py
                out = (px, py)
        return out

    # ================================================================ stuck handling
    def unstick(self, me, move, boost, t):
        p = me["pos"]
        v = me["vel"]
        jump = False
        moving_cmd = abs(move[0]) + abs(move[1]) > 0.3
        hs = math.hypot(v[0], v[1])
        if moving_cmd and hs < 2.0 and me["grounded"]:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.05)
        if self.stuck_t > 0.8 and t > self.unstick_until:
            self.unstick_until = t + 1.2
            a = math.atan2(move[1], move[0]) + self.rng.choice((-1, 1)) * self.rng.uniform(0.6, 1.6)
            self.unstick_dir = (math.cos(a), math.sin(a))
            self.stuck_t = 0.0
            self.path = None
        if t < self.unstick_until:
            # try jumping + jetting over whatever blocks us, while sidestepping a bit
            mx = move[0] * 0.6 + self.unstick_dir[0] * 0.6
            my = move[1] * 0.6 + self.unstick_dir[1] * 0.6
            move = norm2(mx, my)
            if me["grounded"]:
                jump = True
            if me["energy"] > 15:
                boost = True
        return move, jump, boost


if __name__ == "__main__":
    mf.run(Bot)