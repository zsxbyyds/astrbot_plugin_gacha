"""大福鱼抽卡机：聊天随机掉抽数，攒够抽卡，五星带保底，附 WebUI 主页。

设计要点：
- 抽卡次数不直接给，靠跟机器人聊天随机掉落（可配置概率/冷却），另有每日签到。
- 卡池分三星/四星/五星三档，五星 90 抽硬保底，十连至少一个四星+。
- 数据按用户（QQ 号）存 KV，跨群/私聊共享同一份抽卡余额和图鉴。
- WebUI 主页走 AstrBot 官方 views 机制：views/gacha/index.html，用 bridge SDK 调本插件接口。
"""

import asyncio
import random
import secrets
import string
import time
from typing import Any

from quart import jsonify

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import json_response, request as web_request

# 卡池：星级 -> 角色列表
POOL: dict[str, list[dict[str, str]]] = {
    "5": [
        {"name": "大福鱼·传说", "desc": "浑身发光，抽到就是欧气本欧", "img": "5_0.png", "element": "金"},
        {"name": "大福鱼·限定", "desc": "稀有换装，出一次吹一年", "img": "5_1.png", "element": "水"},
        {"name": "锦鲤福神", "desc": "会带来好运的福神", "img": "5_2.png", "element": "火"},
    ],
    "4": [
        {"name": "咸鱼干", "desc": "晒得恰到好处", "img": "4_0.png", "element": "水"},
        {"name": "油炸小黄鱼", "desc": "外酥里嫩", "img": "4_1.png", "element": "火"},
        {"name": "红烧胖头鱼", "desc": "肥美下饭", "img": "4_2.png", "element": "土"},
    ],
    "3": [
        {"name": "小虾米", "desc": "极小，一口一个", "img": "3_0.png", "element": "水"},
        {"name": "小贝壳", "desc": "普普通通", "img": "3_1.png", "element": "土"},
        {"name": "海带丝", "desc": "配汤挺好", "img": "3_2.png", "element": "木"},
    ],
}

STAR_LABEL = {"5": "五星", "4": "四星", "3": "三星"}
STAR_EMOJI = {"5": "🌟", "4": "⭐", "3": "·"}

# 元素克制：五行相克（金克木、木克土、土克水、水克火、火克金）
ELEMENTS = ["金", "木", "水", "火", "土"]
BEATS = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}
# 战斗克制系数
ELEM_BEAT = 1.3      # 我方克制敌方
ELEM_WEAK = 0.85     # 我方被克
CRIT_BASE = 0.10     # 基础暴击率
CRIT_MULT = 1.5      # 暴击伤害倍率
CRIT_CAP = 0.30      # 暴击率上限

# 限定 UP：五星里 UP 卡占更高权重（管理员可切换）
UP_DEFAULT = "大福鱼·限定"
UP_WEIGHT_DEFAULT = 0.5

# 图鉴收集成就：达成一次发一次奖
ACHIEVEMENTS = [
    {"id": "first", "name": "初出茅庐", "desc": "收集到第 1 张卡", "coins": 100},
    {"id": "collect5", "name": "小有所成", "desc": "累计收集 5 种卡", "coins": 300},
    {"id": "star3", "name": "三星集齐", "desc": "集齐全部三星卡", "coins": 500, "exp": 3},
    {"id": "star4", "name": "四星集齐", "desc": "集齐全部四星卡", "coins": 1000, "exp": 5},
    {"id": "star5", "name": "五星集齐", "desc": "集齐全部五星卡", "coins": 3000, "exp": 10, "title": "欧皇"},
    {"id": "full", "name": "全图鉴", "desc": "集齐全部 9 张卡", "coins": 5000, "draws": 20, "title": "鱼塘之主"},
]

# 养成基础数值：按星级
# atk/def/hp 为 1 级基础，g_* 为每级成长，max_lv 为等级上限，exp_lv 为每级所需经验
CHAR_BASE = {
    "5": {"atk": 80, "def": 80, "hp": 300, "g_atk": 7, "g_def": 7, "g_hp": 25, "max_lv": 90, "exp_lv": 120},
    "4": {"atk": 50, "def": 50, "hp": 180, "g_atk": 4, "g_def": 4, "g_hp": 15, "max_lv": 70, "exp_lv": 80},
    "3": {"atk": 30, "def": 30, "hp": 100, "g_atk": 2, "g_def": 2, "g_hp": 8, "max_lv": 50, "exp_lv": 50},
}
MAX_BT = 6            # 最大突破阶段
MAX_STAMINA = 100     # 体力上限
STAMINA_REGEN_SEC = 300  # 每 300 秒回 1 点体力
EXP_PER_BOOK = 10     # 每本经验书提供的经验

MAX_RK = 10            # 碎片升阶上限
RK_LV_UP = 5           # 每升 1 阶提高的等级上限
RK_POWER_MULT = 0.10   # 每阶三维加成
RK_SHARD_BASE = 5      # 升到 1 阶所需碎片，此后每阶 +5

# 词条装备：副本胜利随机掉落，全局加成
RELIC_POOL = [
    {"id": "atk", "name": "锋锐之刃", "stat": "atk", "val": 0.10, "desc": "攻击 +10%"},
    {"id": "def", "name": "铁鳞甲", "stat": "def", "val": 0.12, "desc": "防御 +12%"},
    {"id": "hp", "name": "生命护符", "stat": "hp", "val": 0.15, "desc": "生命 +15%"},
    {"id": "crit", "name": "会心珠", "stat": "crit", "val": 0.05, "desc": "暴击率 +5%"},
    {"id": "power", "name": "战意徽章", "stat": "power", "val": 0.08, "desc": "全属性 +8%"},
]
MAX_RELICS = 6          # 最多同时生效的词条数
RELIC_DROP_RATE = 0.6   # 胜利掉落词条的概率
BOSS_ENRAGE_ATK = 1.4   # 狂暴后攻击倍率

# 好感度（感情）系统：每 10 点好感三维 +2%，上限 100
AFF_POWER_STEP = 0.02   # 每 10 点好感，三维加成比例
AFF_MAX = 100           # 好感度上限
AFF_PAT_WIN = 3         # 上阵出战胜利，队伍每张卡 +3 好感
AFF_PAT_DAILY = 2       # 每日互动（/摸摸）每张卡 +2 好感

# 感情副本：AI 下场扮演卡牌角色谈心，聊满轮数结算好感
AFF_CHAT_ROUNDS = 5     # 聊满几轮结算
AFF_CHAT_REWARD = 10    # 结算给的好感

# 编队羁绊加成（上阵卡牌之间的组合加成，与元素克制是两码事）
BOND_FULL = 0.05        # 三人满编
BOND_ELEM2 = 0.08       # 两张同元素
BOND_ELEM3 = 0.12       # 三张同元素（纯色队，替代 2 张档）

# 好感专属对话：好感达到 20 / 50 / 80 各解锁一条，只对这张卡本人
AFF_LINE_LEVELS = [20, 50, 80]
AFF_LINES: dict[str, dict[int, str]] = {
    "大福鱼·传说": {
        20: "这身光，也就你靠近时不躲。",
        50: "把好运分你一半，反正我身上多的是。",
        80: "我这条鱼的运气，从今往后都算你的。",
    },
    "大福鱼·限定": {
        20: "这套衣服，只在你面前才穿出来。",
        50: "别人出多少鱼都不换，你一句话就行。",
        80: "限定不限定无所谓，你才是那个独一无二。",
    },
    "锦鲤福神": {
        20: "你多看看我，福气就黏上你了。",
        50: "许个愿吧，我帮你往前推一把。",
        80: "有我在，你的鱼塘只会越来越满。",
    },
    "咸鱼干": {
        20: "晒得再干，见你还是会心软。",
        50: "躺着挺好，但陪你翻个身也行。",
        80: "我这条咸鱼，也能为你咸鱼翻身。",
    },
    "油炸小黄鱼": {
        20: "刚出锅那会儿最脆，就像你来的时机。",
        50: "别老盯着我酥皮，里子更香。",
        80: "你咬一口，就知道我为什么只对你酥。",
    },
    "红烧胖头鱼": {
        20: "我这一身肉，炖给你下饭正好。",
        50: "多吃点，别客气，胖有胖的好。",
        80: "能把你喂饱，就是我最大的出息。",
    },
    "小虾米": {
        20: "别看小，呛人的劲儿一点不少。",
        50: "跟着你，小虾米也能掀起浪。",
        80: "一口一个也行，但可别把我忘了。",
    },
    "小贝壳": {
        20: "普通点好，普通才不吵你。",
        50: "壳里藏着点东西，只给你看。",
        80: "我嘴笨，但壳会一直朝你开着。",
    },
    "海带丝": {
        20: "汤里一放我就软乎了，跟你说话也是。",
        50: "缠上你，就别想轻易甩开了。",
        80: "一碗热汤，我陪你喝到见底。",
    },
}

# 每日任务：完成自动发奖，每天 0 点刷新
DAILY_TASKS = {
    "sign": {"name": "每日签到", "coins": 50},
    "pull": {"name": "抽卡 1 次（单抽也算）", "exp": 2},
    "train": {"name": "养成 1 次", "coins": 30},
    "dungeon": {"name": "打 1 次副本", "coins": 40},
    "gift": {"name": "收到 1 次掉抽", "exp": 2},
}


def _task_reward_text(info: dict) -> str:
    parts = []
    if info.get("coins"):
        parts.append(f"{info['coins']} 肥鱼币")
    if info.get("exp"):
        parts.append(f"{info['exp']} 本经验书")
    return "、".join(parts) if parts else "无"


# 副本：需求战力是门槛，越高越难，赢了掉对应资源
DUNGEONS = [
    {
        "id": "pond",
        "name": "鱼塘清扫",
        "desc": "把鱼塘里乱窜的杂鱼清一清。",
        "element": "水",
        "boss_img": "boss_pond.png",
        "power": 250,
        "stamina": 10,
        "win": {"coins": (30, 60), "exp": (1, 3), "mat": ("3", 1)},
        "lose": {"coins": (8, 15)},
    },
    {
        "id": "wreck",
        "name": "沉船探宝",
        "desc": "下到沉船里捞点值钱的。",
        "element": "土",
        "boss_img": "boss_wreck.png",
        "power": 750,
        "stamina": 15,
        "win": {"coins": (80, 140), "exp": (2, 4), "mat": ("4", 1)},
        "lose": {"coins": (20, 35)},
    },
    {
        "id": "boss",
        "name": "深海首领",
        "desc": "打那只盘在海底的老东西。",
        "element": "金",
        "boss_img": "boss_deep.png",
        "power": 1600,
        "stamina": 20,
        "win": {"coins": (180, 280), "exp": (4, 6), "mat": ("5", 1)},
        "lose": {"coins": (45, 70)},
    },
]


def _drop_text(d: dict) -> dict:
    """把副本掉落转成前端可直接展示的文案。"""
    win = d.get("win") or {}
    parts = []
    if win.get("coins"):
        parts.append(f"肥鱼币 {win['coins'][0]}~{win['coins'][1]}")
    if win.get("exp"):
        parts.append(f"经验书 {win['exp'][0]}~{win['exp'][1]}")
    if win.get("mat"):
        mat_star, mat_n = win["mat"]
        parts.append(f"{STAR_LABEL.get(mat_star, '')}素材 ×{mat_n}（概率）")
    lose = d.get("lose") or {}
    lose_text = ""
    if lose.get("coins"):
        lose_text = f"肥鱼币 {lose['coins'][0]}~{lose['coins'][1]}"
    return {"win": " · ".join(parts), "lose": lose_text}


# 商店：肥鱼币结算。give 是买一份加的东西
SHOP_ITEMS = {
    "经验书": {"coins": 50, "give": {"exp": 1}},
    "三星素材": {"coins": 80, "give": {"mat": "3"}},
    "四星素材": {"coins": 150, "give": {"mat": "4"}},
    "五星素材": {"coins": 300, "give": {"mat": "5"}},
    "体力": {"coins": 100, "give": {"stamina": 50}},
    "抽数": {"coins": 400, "give": {"draws": 1}},
}

STATE_KEY = "gacha_state"
BIND_KEY = "gacha_bind"


class Gacha(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = dict(config or {})
        self._state: dict = {"users": {}}
        self._lock = asyncio.Lock()
        self._up_loaded = False
        self._battles: dict[str, dict] = {}
        self._aff_chats: dict[str, dict] = {}
        self._register_web_apis()

    # ---------------- 配置 ----------------

    def _cfg(self, key, default=None):
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def _ssr_rate(self) -> float:
        v = self._cfg("ssr_rate", 0.024)
        try:
            return min(max(float(v), 0.0), 1.0)
        except Exception:
            return 0.024

    def _sr_rate(self) -> float:
        v = self._cfg("sr_rate", 0.12)
        try:
            return min(max(float(v), 0.0), 1.0)
        except Exception:
            return 0.12

    def _pity(self) -> int:
        v = self._cfg("pity", 90)
        try:
            return max(int(v), 1)
        except Exception:
            return 90

    # ---------------- 数据 ----------------

    def _pool_index(self) -> dict[str, dict]:
        idx: dict[str, dict] = {}
        for star, chars in POOL.items():
            for c in chars:
                idx[c["name"]] = {"star": star, "char": c}
        return idx

    def _resolve_name(self, name: str) -> str | None:
        idx = self._pool_index()
        name = str(name or "").strip()
        if name in idx:
            return name
        hits = [n for n in idx if name and name in n]
        if len(hits) == 1:
            return hits[0]
        return None

    def _resolve_item(self, name: str) -> str | None:
        name = str(name or "").strip()
        if name in SHOP_ITEMS:
            return name
        hits = [k for k in SHOP_ITEMS if name and name in k]
        if len(hits) == 1:
            return hits[0]
        return None

    async def _load_state(self) -> dict:
        data = await self.get_kv_data(STATE_KEY, None)
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            return data
        return {"users": {}}

    async def _save_state(self) -> None:
        try:
            await self.put_kv_data(STATE_KEY, self._state)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[抽卡机] 保存数据失败: {exc}")

    @staticmethod
    def _new_user() -> dict:
        return {
            "name": "",
            "draws": 0,
            "free_pull": 1,
            "pity": 0,
            "total": 0,
            "ssr": 0,
            "owned": {},
            "coins": 0,
            "exp": 0,
            "mats": {"3": 0, "4": 0, "5": 0},
            "stamina": MAX_STAMINA,
            "stamina_ts": time.time(),
            "chars": {},
            "team": [],
            "last_gift": 0,
            "last_sign": "",
            "daily": {"date": "", "tasks": {}},
            "achievements": [],
            "pull_log": [],
            "title": "",
            "relics": [],
        }

    @staticmethod
    def _ensure_fields(user: dict) -> bool:
        user.setdefault("owned", {})
        user.setdefault("coins", 0)
        user.setdefault("exp", 0)
        user.setdefault("mats", {"3": 0, "4": 0, "5": 0})
        user.setdefault("stamina", MAX_STAMINA)
        user.setdefault("stamina_ts", time.time())
        user.setdefault("chars", {})
        user.setdefault("team", [])
        user.setdefault("daily", {"date": "", "tasks": {}})
        user.setdefault("achievements", [])
        user.setdefault("pull_log", [])
        user.setdefault("free_pull", 1)
        user.setdefault("title", "")
        user.setdefault("relics", [])
        for k in ("3", "4", "5"):
            user["mats"].setdefault(k, 0)
        # 老数据迁移：养成功能上线前抽的卡只记在图鉴 owned 里，chars 是空的。
        # 这里把已拥有的卡补进 chars，第一张当本体，其余按重复卡转碎片。
        chars = user["chars"]
        changed = False
        for name, cnt in (user.get("owned") or {}).items():
            c = int(cnt or 0)
            if c > 0 and name not in chars:
                chars[name] = {"level": 1, "exp": 0, "bt": 0, "shards": max(c - 1, 0), "rk": 0}
                changed = True
        # 老卡补升阶字段
        for c in chars.values():
            if "rk" not in c:
                c["rk"] = 0
                changed = True
            if "aff" not in c:
                c["aff"] = 0
                changed = True
            if "aff_lines" not in c:
                c["aff_lines"] = []
                changed = True
        return changed

    @staticmethod
    def _daily_refresh(user: dict) -> None:
        today = time.strftime("%Y-%m-%d")
        daily = user.setdefault("daily", {"date": "", "tasks": {}})
        if daily.get("date") != today:
            user["daily"] = {"date": today, "tasks": {}}
            user["free_pull"] = 1

    @staticmethod
    def _complete_task(user: dict, task: str) -> str | None:
        Gacha._daily_refresh(user)
        info = DAILY_TASKS.get(task)
        if not info:
            return None
        tasks = user["daily"]["tasks"]
        if tasks.get(task):
            return None
        tasks[task] = True
        rewards = []
        if "coins" in info:
            user["coins"] = int(user.get("coins", 0)) + int(info["coins"])
            rewards.append(f"{info['coins']} 肥鱼币")
        if "exp" in info:
            user["exp"] = int(user.get("exp", 0)) + int(info["exp"])
            rewards.append(f"{info['exp']} 本经验书")
        return f"完成今日任务「{info['name']}」，得 {'、'.join(rewards)}"

    async def _load_up_config(self) -> None:
        """从 KV 加载限定 UP 配置（管理员切换后重启也保留）。"""
        if self._up_loaded:
            return
        self._up_loaded = True
        try:
            data = await self.get_kv_data("gacha_up", None)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("char"):
            self.config["up_char"] = data.get("char")
            if data.get("weight") is not None:
                self.config["up_weight"] = data.get("weight")

    async def _get_user(self, uid: str) -> dict:
        await self._load_up_config()
        async with self._lock:
            if not self._state.get("users"):
                self._state = await self._load_state()
            users = self._state.setdefault("users", {})
            if uid not in users:
                users[uid] = self._new_user()
            elif self._ensure_fields(users[uid]):
                await self._save_state()
            return users[uid]

    async def _update_user(self, uid: str, user: dict) -> None:
        async with self._lock:
            users = self._state.setdefault("users", {})
            users[uid] = user
            await self._save_state()

    # ---------------- 抽卡核心 ----------------

    def _roll_five(self) -> dict:
        """五星里给 UP 卡提权重。"""
        five = POOL["5"]
        up = str(self._cfg("up_char", UP_DEFAULT) or "").strip()
        weight = self._cfg("up_weight", UP_WEIGHT_DEFAULT)
        try:
            weight = min(max(float(weight), 0.0), 1.0)
        except Exception:
            weight = UP_WEIGHT_DEFAULT
        up_char = next((c for c in five if c["name"] == up), None)
        if up_char and random.random() < weight:
            return up_char
        others = [c for c in five if c["name"] != up]
        return random.choice(others) if others else random.choice(five)

    def _roll_with_pity(self, user: dict) -> tuple[str, dict]:
        ssr = self._ssr_rate()
        sr = self._sr_rate()
        pity = self._pity()
        if int(user.get("pity", 0)) >= pity - 1:
            star = "5"
            user["pity"] = 0
        else:
            r = random.random()
            if r < ssr:
                star = "5"
                user["pity"] = 0
            elif r < ssr + sr:
                star = "4"
                user["pity"] = int(user.get("pity", 0)) + 1
            else:
                star = "3"
                user["pity"] = int(user.get("pity", 0)) + 1
        char = self._roll_five() if star == "5" else random.choice(POOL[star])
        return star, char

    def _pull(self, user: dict, times: int) -> list[tuple[str, dict]]:
        results: list[tuple[str, dict]] = []
        for _ in range(times):
            results.append(self._roll_with_pity(user))
        # 十连保底：至少一个四星+
        if times >= 10 and all(s == "3" for s, _ in results):
            char = random.choice(POOL["4"])
            results[-1] = ("4", char)
            # 保底没吃 pity 的话不动 pity；这里简化处理，不改计数器
        return results

    @staticmethod
    def _char_stats(star: str, level: int, bt: int, rk: int = 0, aff: int = 0) -> dict:
        base = CHAR_BASE.get(star, CHAR_BASE["3"])
        lv = max(int(level), 1)
        b = max(int(bt), 0)
        r = max(int(rk), 0)
        a = max(int(aff), 0)
        mult = 1.0 + 0.2 * b + RK_POWER_MULT * r + AFF_POWER_STEP * (a // 10)
        atk = int((base["atk"] + base["g_atk"] * (lv - 1)) * mult)
        df = int((base["def"] + base["g_def"] * (lv - 1)) * mult)
        hp = int((base["hp"] + base["g_hp"] * (lv - 1)) * mult)
        return {"atk": atk, "def": df, "hp": hp, "power": atk + df + hp}

    @staticmethod
    def _max_level(star: str, rk: int) -> int:
        base = CHAR_BASE.get(star, CHAR_BASE["3"])
        return base["max_lv"] + max(int(rk), 0) * RK_LV_UP

    @staticmethod
    def _rk_cost(rk: int) -> int:
        return (max(int(rk), 0) + 1) * RK_SHARD_BASE

    def _team_bond(self, user: dict, team: list) -> tuple[float, list[str]]:
        """编队羁绊：满编 +5%，同元素 2 张 +8%，纯色 3 张 +12%。"""
        idx = self._pool_index()
        notes: list[str] = []
        bond = 0.0
        if len(team) >= 3:
            bond += BOND_FULL
            notes.append(f"三人满编 +{BOND_FULL * 100:.0f}%")
        elems: dict[str, int] = {}
        for n in team:
            e = str((idx.get(n, {}).get("char") or {}).get("element") or "")
            if e:
                elems[e] = elems.get(e, 0) + 1
        top = max(elems.values()) if elems else 0
        if top >= 3:
            bond += BOND_ELEM3
            notes.append(f"三卡同元素 +{BOND_ELEM3 * 100:.0f}%")
        elif top >= 2:
            bond += BOND_ELEM2
            notes.append(f"两卡同元素 +{BOND_ELEM2 * 100:.0f}%")
        return bond, notes

    def _team_power(self, user: dict) -> tuple[int, list[dict]]:
        idx = self._pool_index()
        chars = user.get("chars", {}) or {}
        team = user.get("team", []) or []
        total = 0
        detail = []
        for name in team:
            if name not in chars:
                continue
            c = chars[name]
            star = idx.get(name, {}).get("star", "3")
            lv = int(c.get("level", 1))
            bt = int(c.get("bt", 0))
            rk = int(c.get("rk", 0))
            aff = int(c.get("aff", 0))
            st = self._char_stats(star, lv, bt, rk, aff)
            total += st["power"]
            detail.append({"name": name, "star": star, "level": lv, "power": st["power"]})
        bond, _ = self._team_bond(user, team)
        total = int(total * (1 + bond))
        return total, detail

    def _element_factor(self, char_ele: str, dungeon_ele: str) -> tuple[float, str]:
        """元素克制关系与系数。"""
        if not char_ele or not dungeon_ele or char_ele not in BEATS:
            return 1.0, ""
        if BEATS.get(char_ele) == dungeon_ele:
            return ELEM_BEAT, "克制"
        if BEATS.get(dungeon_ele) == char_ele:
            return ELEM_WEAK, "被克"
        return 1.0, ""

    def _battle_power(self, user: dict, d: dict) -> tuple[int, list[dict], float]:
        """队伍对某个副本的有效战力（含元素克制）与暴击率。"""
        idx = self._pool_index()
        chars = user.get("chars", {}) or {}
        team = user.get("team", []) or []
        d_ele = str(d.get("element") or "")
        total = 0
        detail = []
        crit_rate = CRIT_BASE
        bonus = self._relic_bonus(user)
        for name in team:
            if name not in chars:
                continue
            c = chars[name]
            entry = idx.get(name, {}) or {}
            star = entry.get("star", "3")
            char_ele = str((entry.get("char") or {}).get("element") or "")
            lv = int(c.get("level", 1))
            bt = int(c.get("bt", 0))
            rk = int(c.get("rk", 0))
            aff = int(c.get("aff", 0))
            st = self._char_stats(star, lv, bt, rk, aff)
            ap = st["atk"] * (1 + bonus["atk"])
            dp = st["def"] * (1 + bonus["def"])
            hp = st["hp"] * (1 + bonus["hp"])
            factor, rel = self._element_factor(char_ele, d_ele)
            adj = int((ap + dp + hp) * factor)
            total += adj
            if star == "5":
                crit_rate += 0.02
            crit_rate += 0.01 * bt
            crit_rate += bonus["crit"]
            detail.append({"name": name, "star": star, "element": char_ele, "power": adj, "rel": rel})
        bond, _ = self._team_bond(user, team)
        total = int(total * (1 + bond))
        crit_rate = min(crit_rate, CRIT_CAP)
        return total, detail, crit_rate

    def _battle_roll(self, user: dict, d: dict) -> tuple[bool, int, list[dict], float, bool]:
        """副本战斗结算：返回 (是否胜利, 有效战力, 队伍明细, 暴击率, 是否暴击)。"""
        power, detail, crit_rate = self._battle_power(user, d)
        roll = power * random.uniform(0.9, 1.2)
        crit = random.random() < crit_rate
        if crit:
            roll *= CRIT_MULT
        win = roll >= d["power"]
        return win, power, detail, crit_rate, crit

    def _check_aff_unlock(self, user: dict, name: str) -> list[str]:
        """好感跨过 20/50/80 时，返回新解锁的专属对话。"""
        c = (user.get("chars", {}) or {}).get(name)
        if not c:
            return []
        aff = int(c.get("aff", 0))
        unlocked = c.setdefault("aff_lines", [])
        lines = AFF_LINES.get(name, {})
        new: list[str] = []
        for lv in AFF_LINE_LEVELS:
            if aff >= lv and lv not in unlocked and lv in lines:
                unlocked.append(lv)
                new.append(lines[lv])
        return new

    # ---------------- 回合制副本战斗 ----------------

    async def _aff_reply(self, char_name: str, char_desc: str, history: list, player_text: str) -> str | None:
        """让 LLM 扮演卡牌角色回一句话，接不上返回 None。"""
        prov = None
        try:
            getter = getattr(self.context, "get_using_provider_async", None)
            if getter:
                prov = await getter()
            else:
                sync = getattr(self.context, "get_using_provider", None)
                if sync:
                    prov = sync()
        except Exception:
            prov = None
        if not prov:
            return None
        system_prompt = (
            f"你是《大福鱼抽卡机》里的卡牌角色「{char_name}」。"
            f"角色介绍：{char_desc}。"
            "玩家来找你谈心培养感情。用这个角色的性格和口吻回复，"
            "一两句话，自然口语化，别太长，别提自己是 AI 或模型。"
        )
        hist_text = "\n".join(history) if history else "（还没有历史）"
        prompt = f"之前的对话：\n{hist_text}\n\n玩家刚说：{player_text}\n\n请以「{char_name}」的身份回复："
        try:
            resp = await prov.text_chat(prompt=prompt, system_prompt=system_prompt)
            text = getattr(resp, "completion_text", None) or getattr(resp, "text", None) or ""
            text = str(text).strip()
            return text or None
        except Exception as exc:
            logger.warning(f"[抽卡机] 感情副本 LLM 调用失败: {exc}")
            return None

    def _relic_bonus(self, user: dict) -> dict:
        """词条装备的全局属性加成（均为小数比例）。"""
        bonus = {"atk": 0.0, "def": 0.0, "hp": 0.0, "crit": 0.0, "power": 0.0}
        for r in (user.get("relics") or []):
            stat = str(r.get("stat") or "")
            try:
                val = float(r.get("val", 0))
            except Exception:
                val = 0.0
            if stat == "power":
                bonus["atk"] += val
                bonus["def"] += val
                bonus["hp"] += val
            elif stat in bonus:
                bonus[stat] += val
        return bonus

    def _boss_stats(self, d: dict) -> dict:
        """由副本需求战力推导首领的回合制数值。"""
        power = int(d.get("power", 100))
        hp = int(power * 3)
        return {
            "name": d["name"],
            "element": str(d.get("element") or ""),
            "hp": hp,
            "max_hp": hp,
            "atk": int(power * 0.30),
            "def": int(power * 0.12),
            "phase": 1,
            "enraged": False,
        }

    def _start_battle(self, user: dict, d: dict) -> dict:
        idx = self._pool_index()
        chars = user.get("chars", {}) or {}
        team = user.get("team", []) or []
        bonus = self._relic_bonus(user)
        party = []
        for name in team:
            if name not in chars:
                continue
            c = chars[name]
            entry = idx.get(name, {}) or {}
            star = entry.get("star", "3")
            char_ele = str((entry.get("char") or {}).get("element") or "")
            lv = int(c.get("level", 1))
            bt = int(c.get("bt", 0))
            rk = int(c.get("rk", 0))
            st = self._char_stats(star, lv, bt, rk)
            atk = int(st["atk"] * (1 + bonus["atk"]))
            df = int(st["def"] * (1 + bonus["def"]))
            hp = int(st["hp"] * (1 + bonus["hp"]))
            party.append({
                "name": name, "element": char_ele, "star": star,
                "img": (entry.get("char") or {}).get("img", ""),
                "atk": atk, "def": df, "hp": hp, "max_hp": hp, "alive": True,
            })
        boss = self._boss_stats(d)
        boss["img"] = str(d.get("boss_img") or "")
        b = {
            "dungeon_id": d["id"],
            "dungeon_name": d["name"],
            "element": d.get("element", ""),
            "turn": 0,
            "boss": boss,
            "party": party,
            "hand": [],
            "log": [],
            "bonus": bonus,
            "rewards": None,
            "finished": False,
            "win": False,
        }
        b["log"].append(f"进入【{d['name']}】，首领 {boss['name']} 挡在面前。")
        b["hand"] = self._draw_hand(b)
        return b

    @staticmethod
    def _draw_hand(b: dict) -> list[str]:
        alive = [p for p in b["party"] if p["alive"]]
        if not alive:
            return []
        names = [p["name"] for p in alive]
        if len(names) <= 2:
            return names[:]
        return random.sample(names, 2)

    def _battle_turn(self, b: dict, chosen: str) -> None:
        """结算一个回合：我方出手 -> 判断胜负 -> 首领反击 -> 判断胜负 -> 发下一手牌。"""
        if b.get("finished"):
            return
        b["turn"] += 1
        log = b["log"]
        boss = b["boss"]
        card = next((p for p in b["party"] if p["alive"] and p["name"] == chosen), None)
        if card is None:
            b["hand"] = self._draw_hand(b)
            return
        # 我方攻击
        ef, rel = self._element_factor(card["element"], boss["element"])
        crit_chance = CRIT_BASE + (0.02 if card["star"] == "5" else 0) + b["bonus"]["crit"]
        crit_chance = min(crit_chance, CRIT_CAP)
        crit = random.random() < crit_chance
        base = max(card["atk"] - boss["def"] * 0.3, card["atk"] * 0.2)
        dmg = base * ef * random.uniform(0.9, 1.15)
        if crit:
            dmg *= CRIT_MULT
        dmg = max(int(dmg), 1)
        boss["hp"] = max(boss["hp"] - dmg, 0)
        parts = [f"你打出 {card['name']}，造成 {dmg} 伤害"]
        if rel:
            parts.append(f"元素{rel}")
        if crit:
            parts.append(f"暴击！({crit_chance * 100:.0f}%)")
        log.append("，".join(parts))
        # 狂暴阶段
        if not boss["enraged"] and boss["hp"] > 0 and boss["hp"] <= boss["max_hp"] // 2:
            boss["enraged"] = True
            boss["phase"] = 2
            boss["atk"] = int(boss["atk"] * BOSS_ENRAGE_ATK)
            log.append("⚠️ 首领狂暴了，攻击暴涨！")
        if boss["hp"] <= 0:
            b["finished"] = True
            b["win"] = True
            log.append(f"【{b['dungeon_name']}】通关！")
            return
        # 首领反击
        alive = [p for p in b["party"] if p["alive"]]
        if alive:
            target = random.choice(alive)
            ef2, rel2 = self._element_factor(boss["element"], target["element"])
            bbase = max(boss["atk"] - target["def"] * 0.3, boss["atk"] * 0.2)
            bdmg = bbase * ef2 * random.uniform(0.9, 1.15)
            bdmg = max(int(bdmg), 1)
            target["hp"] = max(target["hp"] - bdmg, 0)
            tparts = [f"首领反击 {target['name']}，造成 {bdmg} 伤害"]
            if rel2:
                tparts.append(f"元素{rel2}")
            log.append("，".join(tparts))
            if target["hp"] <= 0:
                target["alive"] = False
                log.append(f"{target['name']} 倒下了！")
        if not any(p["alive"] for p in b["party"]):
            b["finished"] = True
            b["win"] = False
            log.append(f"队伍全灭，败给【{b['dungeon_name']}】。")
            return
        b["hand"] = self._draw_hand(b)

    def _battle_state(self, b: dict) -> dict:
        boss = b["boss"]
        party = b["party"]
        by_name = {p["name"]: p for p in party}
        hand = []
        for n in b["hand"]:
            p = by_name.get(n)
            if p:
                hand.append({
                    "name": p["name"], "img": p.get("img", ""),
                    "star": p.get("star", ""), "element": p.get("element", ""),
                    "atk": p.get("atk", 0),
                })
        return {
            "turn": b["turn"],
            "finished": b["finished"],
            "win": b["win"],
            "dungeon_id": b["dungeon_id"],
            "dungeon_name": b["dungeon_name"],
            "boss": {
                "name": boss["name"],
                "img": boss.get("img", ""),
                "element": boss["element"],
                "hp": boss["hp"],
                "max_hp": boss["max_hp"],
                "phase": boss["phase"],
                "enraged": boss["enraged"],
            },
            "party": [
                {"name": p["name"], "img": p.get("img", ""), "star": p.get("star", ""),
                 "element": p["element"], "hp": p["hp"], "max_hp": p["max_hp"], "alive": p["alive"]}
                for p in party
            ],
            "hand": hand,
            "log": b["log"][-8:],
            "rewards": b.get("rewards"),
        }

    def _apply_battle_rewards(self, user: dict, d: dict, win: bool) -> dict:
        """按胜负结算资源，胜利时可能掉词条。返回给前端展示的奖励结构。"""
        rewards = {"coins": 0, "exp": 0, "mat": None, "relic": None, "text": []}
        if win:
            _chars = user.get("chars", {}) or {}
            for _nm in (user.get("team") or []):
                _c = _chars.get(_nm)
                if _c:
                    _c["aff"] = min(AFF_MAX, int(_c.get("aff", 0)) + AFF_PAT_WIN)
            cmin, cmax = d["win"]["coins"]
            coins = random.randint(cmin, cmax)
            user["coins"] = int(user.get("coins", 0)) + coins
            rewards["coins"] = coins
            rewards["text"].append(f"{coins} 肥鱼币")
            emin, emax = d["win"]["exp"]
            expn = random.randint(emin, emax)
            user["exp"] = int(user.get("exp", 0)) + expn
            rewards["exp"] = expn
            rewards["text"].append(f"{expn} 本经验书")
            mat_star, mat_n = d["win"]["mat"]
            if random.random() < 0.5:
                mats = user.setdefault("mats", {"3": 0, "4": 0, "5": 0})
                mats[mat_star] = int(mats.get(mat_star, 0)) + mat_n
                rewards["mat"] = f"{STAR_LABEL[mat_star]}素材 ×{mat_n}"
                rewards["text"].append(rewards["mat"])
            relics = user.setdefault("relics", [])
            if len(relics) < MAX_RELICS and random.random() < RELIC_DROP_RATE:
                r = random.choice(RELIC_POOL)
                relics.append({"id": r["id"], "name": r["name"], "stat": r["stat"], "val": r["val"], "desc": r["desc"]})
                rewards["relic"] = f"{r['name']}（{r['desc']}）"
                rewards["text"].append(f"词条【{r['name']}】{r['desc']}")
        else:
            cmin, cmax = d["lose"]["coins"]
            coins = random.randint(cmin, cmax)
            user["coins"] = int(user.get("coins", 0)) + coins
            rewards["coins"] = coins
            rewards["text"].append(f"{coins} 肥鱼币")
        rewards["text_str"] = "、".join(rewards["text"])
        return rewards

    @staticmethod
    def _achievement_done(a: dict, total: int, by_star: dict) -> bool:
        aid = a["id"]
        if aid == "first":
            return total >= 1
        if aid == "collect5":
            return total >= 5
        if aid == "star3":
            return by_star.get("3", 0) >= 3
        if aid == "star4":
            return by_star.get("4", 0) >= 3
        if aid == "star5":
            return by_star.get("5", 0) >= 3
        if aid == "full":
            return total >= 9
        return False

    def _check_achievements(self, user: dict) -> list[str]:
        """抽卡后检查并发放新达成的图鉴成就。"""
        owned = user.get("owned", {}) or {}
        achieved = user.setdefault("achievements", [])
        by_star = {"3": 0, "4": 0, "5": 0}
        for star in ("5", "4", "3"):
            for c in POOL[star]:
                if int(owned.get(c["name"], 0) or 0) > 0:
                    by_star[star] += 1
        total = sum(by_star.values())
        msgs = []
        for a in ACHIEVEMENTS:
            if a["id"] in achieved:
                continue
            if not self._achievement_done(a, total, by_star):
                continue
            achieved.append(a["id"])
            if "coins" in a:
                user["coins"] = int(user.get("coins", 0)) + int(a["coins"])
            if "exp" in a:
                user["exp"] = int(user.get("exp", 0)) + int(a["exp"])
            if "draws" in a:
                user["draws"] = int(user.get("draws", 0)) + int(a["draws"])
            if "title" in a:
                user["title"] = a["title"]
            reward = []
            if "coins" in a:
                reward.append(f"{a['coins']} 肥鱼币")
            if "exp" in a:
                reward.append(f"{a['exp']} 本经验书")
            if "draws" in a:
                reward.append(f"{a['draws']} 抽")
            if "title" in a:
                reward.append(f"称号「{a['title']}」")
            msgs.append(f"解锁成就「{a['name']}」，奖励 {'、'.join(reward)}")
        return msgs

    @staticmethod
    def _find_dungeon(name: str) -> dict | None:
        name = str(name or "").strip()
        for d in DUNGEONS:
            if name in (d["id"], d["name"]):
                return d
        hits = [d for d in DUNGEONS if name and name in d["name"]]
        if len(hits) == 1:
            return hits[0]
        return None

    @staticmethod
    def _regen_stamina(user: dict) -> int:
        now = time.time()
        ts = float(user.get("stamina_ts", now))
        cur = int(user.get("stamina", MAX_STAMINA))
        if cur < MAX_STAMINA:
            gained = int((now - ts) // STAMINA_REGEN_SEC)
            if gained > 0:
                cur = min(cur + gained, MAX_STAMINA)
                user["stamina"] = cur
                user["stamina_ts"] = ts + gained * STAMINA_REGEN_SEC
        else:
            user["stamina_ts"] = now
        return cur

    def _apply_results(self, user: dict, results: list[tuple[str, dict]]) -> None:
        owned = user.setdefault("owned", {})
        chars = user.setdefault("chars", {})
        log = user.setdefault("pull_log", [])
        ts = int(time.time())
        for star, char in results:
            user["total"] = int(user.get("total", 0)) + 1
            if star == "5":
                user["ssr"] = int(user.get("ssr", 0)) + 1
            name = char["name"]
            owned[name] = int(owned.get(name, 0)) + 1
            if name in chars:
                chars[name]["shards"] = int(chars[name].get("shards", 0)) + 1
            else:
                chars[name] = {"level": 1, "exp": 0, "bt": 0, "shards": 0, "rk": 0}
            log.append({"t": ts, "star": star, "name": name})
        user["pull_log"] = log[-50:]

    @staticmethod
    def _fmt_results(results: list[tuple[str, dict]], cost: int) -> str:
        lines: list[str] = []
        for star, char in results:
            mark = STAR_EMOJI.get(star, "·")
            label = STAR_LABEL.get(star, "?")
            lines.append(f"{mark}{label} {char['name']}")
        ssr_hits = sum(1 for s, _ in results if s == "5")
        head = f"花了 {cost} 抽："
        if ssr_hits:
            head += f" 出了 {ssr_hits} 个五星！"
        return head + "\n" + "\n".join(lines)

    # ---------------- 指令 ----------------

    @filter.command("十连", alias={"抽卡", "抽十连"})
    async def cmd_ten(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        nick = str(event.get_sender_name() or "")
        user = await self._get_user(uid)
        user["name"] = nick or user.get("name", "")
        if int(user.get("draws", 0)) < 10:
            left = 10 - int(user.get("draws", 0))
            yield event.plain_result(
                f"抽卡次数不够，还差 {left} 抽。找大福鱼多唠两句，攒着就掉抽数了。"
            )
            return
        user["draws"] = int(user.get("draws", 0)) - 10
        results = self._pull(user, 10)
        self._apply_results(user, results)
        task_msg = self._complete_task(user, "pull")
        ach_msgs = self._check_achievements(user)
        await self._update_user(uid, user)
        extra = f"\n{task_msg}" if task_msg else ""
        if ach_msgs:
            extra += "\n" + "\n".join(ach_msgs)
        yield event.plain_result(
            self._fmt_results(results, 10)
            + f"\n剩 {user['draws']} 抽｜保底还有 {self._pity() - int(user['pity'])} 抽出五星"
            + extra
        )

    @filter.command("单抽")
    async def cmd_one(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        nick = str(event.get_sender_name() or "")
        user = await self._get_user(uid)
        user["name"] = nick or user.get("name", "")
        if int(user.get("draws", 0)) < 1:
            yield event.plain_result("没抽数了，去找大福鱼唠嗑掉点抽数吧。")
            return
        user["draws"] = int(user.get("draws", 0)) - 1
        results = self._pull(user, 1)
        self._apply_results(user, results)
        task_msg = self._complete_task(user, "pull")
        ach_msgs = self._check_achievements(user)
        await self._update_user(uid, user)
        star, char = results[0]
        extra = f"\n{task_msg}" if task_msg else ""
        if ach_msgs:
            extra += "\n" + "\n".join(ach_msgs)
        if star == "5":
            yield event.plain_result(f"🌟五星 {char['name']}！这把你欧得离谱。{extra}")
        elif star == "4":
            yield event.plain_result(f"⭐四星 {char['name']}，还行。{extra}")
        else:
            yield event.plain_result(f"·三星 {char['name']}，给你留个念想。{extra}")

    @filter.command("免费抽", alias={"每日一抽", "白嫖"})
    async def cmd_free(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        nick = str(event.get_sender_name() or "")
        user = await self._get_user(uid)
        user["name"] = nick or user.get("name", "")
        self._daily_refresh(user)
        if int(user.get("free_pull", 0)) <= 0:
            yield event.plain_result("今天的免费一抽用完了，明天 0 点刷新。")
            return
        user["free_pull"] = int(user.get("free_pull", 0)) - 1
        results = self._pull(user, 1)
        self._apply_results(user, results)
        task_msg = self._complete_task(user, "pull")
        ach_msgs = self._check_achievements(user)
        await self._update_user(uid, user)
        star, char = results[0]
        extra = f"\n{task_msg}" if task_msg else ""
        if ach_msgs:
            extra += "\n" + "\n".join(ach_msgs)
        tag = "免费一抽，"
        if star == "5":
            yield event.plain_result(f"{tag}🌟五星 {char['name']}！白嫖出货，赚翻了。{extra}")
        elif star == "4":
            yield event.plain_result(f"{tag}⭐四星 {char['name']}，白嫖不亏。{extra}")
        else:
            yield event.plain_result(f"{tag}·三星 {char['name']}，就当垫刀了。{extra}")

    @filter.command("抽卡记录", alias={"记录", "抽卡历史", "出货记录"})
    async def cmd_log(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        self._daily_refresh(user)
        await self._update_user(uid, user)
        log = user.get("pull_log", []) or []
        pity = self._pity() - int(user.get("pity", 0))
        lines = [
            f"累计抽了 {user.get('total', 0)} 抽，出了 {user.get('ssr', 0)} 个五星。",
            f"距下次五星保底还有 {pity} 抽。",
        ]
        if not log:
            lines.append("还没有抽卡记录，先来一抽。")
        else:
            lines.append("最近记录：")
            for r in reversed(log[-10:]):
                t = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("t", 0))))
                mark = STAR_EMOJI.get(r.get("star", "3"), "·")
                lines.append(f"{t} {mark} {r.get('name', '?')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("台词", alias={"专属对话", "心里话"})
    async def cmd_aff_lines(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.get("chars", {}) or {}
        name = self._resolve_name(str(arg or ""))
        if not name:
            lines = ["好感 20 / 50 / 80 各解锁一句专属对话。", "用法：/台词 卡名"]
            has = False
            for n, c in sorted(chars.items()):
                ul = c.get("aff_lines") or []
                if ul:
                    lines.append(f"{n}：已解锁 {len(ul)} 句")
                    has = True
            if not has:
                lines.append("还没有解锁任何对话，多 /摸摸 培养感情吧。")
            yield event.plain_result("\n".join(lines))
            return
        if name not in chars:
            yield event.plain_result("你还没这张卡。")
            return
        c = chars[name]
        aff = int(c.get("aff", 0))
        unlocked = c.get("aff_lines") or []
        line_map = AFF_LINES.get(name, {})
        out = [f"{name}（好感 {aff}/{AFF_MAX}）"]
        for lv in AFF_LINE_LEVELS:
            txt = line_map.get(lv)
            if not txt:
                continue
            if lv in unlocked:
                out.append(f"【{lv}】{txt}")
            else:
                out.append(f"【{lv}】？？？（好感 {lv} 解锁）")
        yield event.plain_result("\n".join(out))

    @filter.command("我的抽卡", alias={"抽卡余额", "图鉴"})
    async def cmd_mine(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        draws = int(user.get("draws", 0))
        total = int(user.get("total", 0))
        ssr = int(user.get("ssr", 0))
        pity = self._pity() - int(user.get("pity", 0))
        owned = user.get("owned", {}) or {}
        lines = []
        title = user.get("title") or ""
        if title:
            lines.append(f"称号：{title}")
        lines += [
            f"抽卡余额：{draws} 抽",
            f"累计抽了 {total} 抽，出了 {ssr} 个五星",
            f"距下次五星保底还有 {pity} 抽",
        ]
        if owned:
            collected = "、".join(f"{k}×{v}" for k, v in sorted(owned.items()))
            lines.append(f"已收集：{collected}")
        yield event.plain_result("\n".join(lines))

    @filter.command("资源")
    async def cmd_res(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        stam = self._regen_stamina(user)
        await self._update_user(uid, user)
        mats = user.get("mats", {})
        lines = [
            f"肥鱼币：{user.get('coins', 0)}",
            f"经验书：{user.get('exp', 0)}",
            f"体力：{stam}/{MAX_STAMINA}",
            f"突破素材：三星 {mats.get('3', 0)} / 四星 {mats.get('4', 0)} / 五星 {mats.get('5', 0)}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("养成", alias={"升级"})
    async def cmd_level(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        parts = str(arg or "").split()
        if not parts:
            yield event.plain_result("用法：/养成 <卡名> [经验书数量]，不填数量就全喂这张卡。")
            return
        name = self._resolve_name(parts[0])
        if not name:
            yield event.plain_result("没这张卡，/卡池 看看名字。")
            return
        if name not in chars:
            yield event.plain_result("你还没抽到这张卡。")
            return
        idx = self._pool_index()
        star = idx[name]["star"]
        base = CHAR_BASE[star]
        c = chars[name]
        level = int(c.get("level", 1))
        max_lv = self._max_level(star, int(c.get("rk", 0)))
        if level >= max_lv:
            yield event.plain_result(f"{name} 已经满级（{max_lv} 级）了。")
            return
        total_exp = int(user.get("exp", 0))
        if total_exp <= 0:
            yield event.plain_result("没经验书，去 /签到 领，以后副本也掉。")
            return
        want = total_exp
        if len(parts) > 1:
            try:
                want = max(int(parts[1]), 0)
            except Exception:
                want = total_exp
        used = min(want, total_exp)
        if used <= 0:
            yield event.plain_result("要喂的经验书数量得是正数。")
            return
        gain = used * EXP_PER_BOOK + int(c.get("exp", 0))
        while gain >= base["exp_lv"] and level < max_lv:
            gain -= base["exp_lv"]
            level += 1
        c["exp"] = gain
        c["level"] = level
        user["exp"] = total_exp - used
        task_msg = self._complete_task(user, "train")
        await self._update_user(uid, user)
        extra = f"\n{task_msg}" if task_msg else ""
        yield event.plain_result(f"{name} 升到 {level} 级，用了 {used} 本经验书。剩 {user['exp']} 本。{extra}")

    @filter.command("突破")
    async def cmd_bt(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        name = self._resolve_name(str(arg or ""))
        if not name:
            yield event.plain_result("没这张卡，/卡池 看看名字。")
            return
        if name not in chars:
            yield event.plain_result("你还没抽到这张卡。")
            return
        idx = self._pool_index()
        star = idx[name]["star"]
        c = chars[name]
        bt = int(c.get("bt", 0))
        level = int(c.get("level", 1))
        if bt >= MAX_BT:
            yield event.plain_result(f"{name} 已经满突破了。")
            return
        need_lv = (bt + 1) * 10
        if level < need_lv:
            yield event.plain_result(f"等级不够，{name} 要到 {need_lv} 级才能突破。")
            return
        need_mat = bt + 1
        mats = user.setdefault("mats", {"3": 0, "4": 0, "5": 0})
        if int(mats.get(star, 0)) < need_mat:
            yield event.plain_result(
                f"素材不够，要 {need_mat} 个{STAR_LABEL[star]}素材（现在 {mats.get(star, 0)} 个）。"
            )
            return
        mats[star] = int(mats[star]) - need_mat
        c["bt"] = bt + 1
        task_msg = self._complete_task(user, "train")
        await self._update_user(uid, user)
        st = self._char_stats(star, level, bt + 1, int(c.get("rk", 0)))
        extra = f"\n{task_msg}" if task_msg else ""
        yield event.plain_result(
            f"{name} 突破到 {bt + 1} 阶。三维：攻 {st['atk']} / 防 {st['def']} / 血 {st['hp']}。{extra}"
        )

    @filter.command("升阶", alias={"觉醒"})
    async def cmd_rk(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        name = self._resolve_name(str(arg or ""))
        if not name:
            yield event.plain_result("没这张卡，/卡池 看看名字。")
            return
        if name not in chars:
            yield event.plain_result("你还没抽到这张卡。")
            return
        idx = self._pool_index()
        star = idx[name]["star"]
        c = chars[name]
        rk = int(c.get("rk", 0))
        if rk >= MAX_RK:
            yield event.plain_result(f"{name} 已经满阶（{MAX_RK} 阶）了。")
            return
        cost = self._rk_cost(rk)
        shards = int(c.get("shards", 0))
        if shards < cost:
            yield event.plain_result(
                f"碎片不够，{name} 升到 {rk + 1} 阶要 {cost} 片（现在 {shards} 片）。重复抽到同卡才掉碎片。"
            )
            return
        c["shards"] = shards - cost
        c["rk"] = rk + 1
        task_msg = self._complete_task(user, "train")
        await self._update_user(uid, user)
        st = self._char_stats(star, int(c.get("level", 1)), int(c.get("bt", 0)), rk + 1)
        extra = f"\n{task_msg}" if task_msg else ""
        yield event.plain_result(
            f"{name} 升到 {rk + 1} 阶。三维：攻 {st['atk']} / 防 {st['def']} / 血 {st['hp']}，"
            f"等级上限 {self._max_level(star, rk + 1)}。{extra}"
        )

    @filter.command("商店", alias={"商城"})
    async def cmd_shop(self, event: AstrMessageEvent):
        lines = ["大福鱼商店（肥鱼币结算）："]
        for key, item in SHOP_ITEMS.items():
            lines.append(f"{key}：{item['coins']} 币")
        lines.append("用法：/买 <商品> [数量]")
        yield event.plain_result("\n".join(lines))

    @filter.command("买", alias={"购买"})
    async def cmd_buy(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        parts = str(arg or "").split()
        if not parts:
            yield event.plain_result("用法：/买 <商品> [数量]，先 /商店 看价。")
            return
        key = self._resolve_item(parts[0])
        if not key:
            yield event.plain_result("没这商品，/商店 看看。")
            return
        qty = 1
        if len(parts) > 1:
            try:
                qty = max(int(parts[1]), 1)
            except Exception:
                qty = 1
        item = SHOP_ITEMS[key]
        cost = item["coins"] * qty
        coins = int(user.get("coins", 0))
        if coins < cost:
            yield event.plain_result(f"肥鱼币不够，要 {cost} 币，现在 {coins} 币。")
            return
        give = item["give"]
        if give.get("stamina"):
            self._regen_stamina(user)
            gain = int(give["stamina"]) * qty
            user["coins"] = coins - cost
            user["stamina"] = int(user.get("stamina", 0)) + gain
            await self._update_user(uid, user)
            yield event.plain_result(f"买了 {gain} 点体力，现在 {user['stamina']} 点。花了 {cost} 币。剩 {user['coins']} 币。")
            return
        user["coins"] = coins - cost
        if "exp" in give:
            user["exp"] = int(user.get("exp", 0)) + give["exp"] * qty
        if "mat" in give:
            mats = user.setdefault("mats", {"3": 0, "4": 0, "5": 0})
            mats[give["mat"]] = int(mats.get(give["mat"], 0)) + qty
        if "draws" in give:
            user["draws"] = int(user.get("draws", 0)) + give["draws"] * qty
        await self._update_user(uid, user)
        yield event.plain_result(f"买了 {qty} 份「{key}」，花了 {cost} 币。剩 {user['coins']} 币。")

    @filter.command("碎片")
    async def cmd_shards(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.get("chars", {}) or {}
        lines = []
        for name, c in sorted(chars.items()):
            s = int(c.get("shards", 0))
            if s > 0:
                lines.append(f"{name}：{s} 片")
        if not lines:
            yield event.plain_result("还没有碎片，重复抽到卡才会转成碎片。")
            return
        yield event.plain_result("碎片一览：\n" + "\n".join(lines))

    @filter.command("战力")
    async def cmd_power(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.get("chars", {}) or {}
        if not chars:
            yield event.plain_result("还没抽到卡，先 /十连 吧。")
            return
        idx = self._pool_index()
        name = self._resolve_name(str(arg or ""))
        if name:
            if name not in chars:
                yield event.plain_result("你还没这张卡。")
                return
            star = idx[name]["star"]
            c = chars[name]
            st = self._char_stats(star, int(c.get("level", 1)), int(c.get("bt", 0)), int(c.get("rk", 0)), int(c.get("aff", 0)))
            yield event.plain_result(
                f"{name}（{STAR_LABEL[star]} Lv.{c.get('level', 1)} 突破{c.get('bt', 0)}阶）\n"
                f"攻 {st['atk']} / 防 {st['def']} / 血 {st['hp']}，战力 {st['power']}"
            )
            return
        power, detail = self._team_power(user)
        if detail:
            bond, notes = self._team_bond(user, user.get("team", []) or [])
            if notes:
                yield event.plain_result(f"队伍战力 {power}（{'，'.join(notes)}）。")
            else:
                yield event.plain_result(f"队伍战力 {power}。")
            return
        lines = []
        total_power = 0
        for n, c in sorted(chars.items()):
            star = idx.get(n, {}).get("star", "3")
            st = self._char_stats(star, int(c.get("level", 1)), int(c.get("bt", 0)), int(c.get("rk", 0)), int(c.get("aff", 0)))
            total_power += st["power"]
            lines.append(f"{n} Lv.{c.get('level', 1)}：战力 {st['power']}")
        lines.append(f"总战力：{total_power}")
        yield event.plain_result("\n".join(lines))

    @filter.command("摸摸", alias={"互动", "培养感情"})
    async def cmd_aff(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.get("chars", {}) or {}
        if not chars:
            yield event.plain_result("还没抽到卡，先 /十连 吧。")
            return
        idx = self._pool_index()
        name = self._resolve_name(str(arg or ""))
        if not name:
            lines = []
            for n, c in sorted(chars.items()):
                aff = int(c.get("aff", 0))
                star = idx.get(n, {}).get("star", "3")
                bonus = AFF_POWER_STEP * (aff // 10)
                lines.append(f"{n}（{STAR_LABEL[star]}）好感 {aff}/{AFF_MAX}，三维加成 +{bonus * 100:.0f}%")
            lines.append("用法：/摸摸 卡名 跟 TA 互动（每天每张卡一次，+好感）")
            yield event.plain_result("\n".join(lines))
            return
        if name not in chars:
            yield event.plain_result("你还没这张卡。")
            return
        c = chars[name]
        today = time.strftime("%Y-%m-%d")
        pat = c.setdefault("aff_log", {})
        if pat.get(today):
            yield event.plain_result(f"今天已经跟 {name} 互动过了，明天再来。")
            return
        pat[today] = True
        for k in list(pat.keys()):
            if k != today:
                del pat[k]
        c["aff"] = min(AFF_MAX, int(c.get("aff", 0)) + AFF_PAT_DAILY)
        new_lines = self._check_aff_unlock(user, name)
        await self._update_user(uid, user)
        star = idx[name]["star"]
        st = self._char_stats(star, int(c.get("level", 1)), int(c.get("bt", 0)), int(c.get("rk", 0)), int(c.get("aff", 0)))
        msg = (
            f"摸了摸 {name}，TA 挺高兴的（好感 +{AFF_PAT_DAILY}，现在 {c['aff']}/{AFF_MAX}）。\n"
            f"攻 {st['atk']} / 防 {st['def']} / 血 {st['hp']}，战力 {st['power']}"
        )
        if new_lines:
            msg += "\n解锁专属对话：\n" + "\n".join(f"「{t}」" for t in new_lines)
        yield event.plain_result(msg)

    @filter.command("感情副本", alias={"谈心"})
    async def cmd_aff_chat(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.get("chars", {}) or {}
        if not chars:
            yield event.plain_result("还没抽到卡，先 /十连 吧。")
            return
        idx = self._pool_index()
        name = self._resolve_name(str(arg or ""))
        if not name:
            sess = self._aff_chats.get(uid)
            if sess:
                yield event.plain_result(
                    f"正在跟 {sess['char']} 谈心，已聊 {sess['rounds']}/{AFF_CHAT_ROUNDS} 轮。\n"
                    f"用 /说 想说的话 继续，聊满 {AFF_CHAT_ROUNDS} 轮自动结算好感。"
                )
            else:
                yield event.plain_result("用法：/感情副本 卡名，选一张卡开始谈心。")
            return
        if name not in chars:
            yield event.plain_result("你还没这张卡。")
            return
        entry = idx.get(name, {}) or {}
        desc = (entry.get("char") or {}).get("desc", "") or entry.get("desc", "") or "一只可爱的鱼。"
        self._aff_chats[uid] = {"char": name, "rounds": 0, "history": []}
        reply = await self._aff_reply(name, desc, [], "（玩家刚来找你，你先打个招呼，一两句话）")
        if reply is None:
            self._aff_chats.pop(uid, None)
            yield event.plain_result("语言模型没接上，感情副本暂时开不了。")
            return
        self._aff_chats[uid]["history"].append(f"【{name}】{reply}")
        yield event.plain_result(
            f"你开始跟 {name} 谈心（/说 继续，聊满 {AFF_CHAT_ROUNDS} 轮结算）。\n\n{name}：{reply}"
        )

    @filter.command("说", alias={"say"})
    async def cmd_aff_say(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        sess = self._aff_chats.get(uid)
        if not sess:
            yield event.plain_result("还没在谈心，先 /感情副本 卡名 开始。")
            return
        text = str(arg or "").strip()
        if not text:
            yield event.plain_result("说点啥？/说 想说的话")
            return
        name = sess["char"]
        idx = self._pool_index()
        entry = idx.get(name, {}) or {}
        desc = (entry.get("char") or {}).get("desc", "") or entry.get("desc", "") or "一只可爱的鱼。"
        hist = sess["history"][-6:]
        reply = await self._aff_reply(name, desc, hist, text)
        if reply is None:
            yield event.plain_result("语言模型没接上，这轮没回上。")
            return
        sess["history"].append(f"玩家：{text}")
        sess["history"].append(f"【{name}】{reply}")
        sess["rounds"] = int(sess.get("rounds", 0)) + 1
        if sess["rounds"] >= AFF_CHAT_ROUNDS:
            user = await self._get_user(uid)
            c = user.get("chars", {}).get(name)
            new_lines: list[str] = []
            if c:
                c["aff"] = min(AFF_MAX, int(c.get("aff", 0)) + AFF_CHAT_REWARD)
                new_lines = self._check_aff_unlock(user, name)
                await self._update_user(uid, user)
            self._aff_chats.pop(uid, None)
            aff_now = c["aff"] if c else 0
            msg = (
                f"{name}：{reply}\n\n"
                f"聊得挺投缘，这轮谈心结束，{name} 好感 +{AFF_CHAT_REWARD}（现在 {aff_now}/{AFF_MAX}）。"
            )
            if new_lines:
                msg += "\n解锁专属对话：\n" + "\n".join(f"「{t}」" for t in new_lines)
            yield event.plain_result(msg)
            return
        yield event.plain_result(f"{name}：{reply}\n\n（{sess['rounds']}/{AFF_CHAT_ROUNDS} 轮）")

    @filter.command("结束感情", alias={"退出感情", "不聊了"})
    async def cmd_aff_end(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        sess = self._aff_chats.pop(uid, None)
        if sess:
            yield event.plain_result(f"跟 {sess['char']} 的谈心先到这，下次再聊。")
        else:
            yield event.plain_result("没在谈心。")

    @filter.command("卡池")
    async def cmd_pool(self, event: AstrMessageEvent):
        ssr = self._ssr_rate()
        sr = self._sr_rate()
        r_rate = max(1.0 - ssr - sr, 0.0)

        def row(chars):
            return "、".join(f"{c['name']}({c.get('element', '')})" for c in chars)

        lines = [
            "大福鱼卡池一览（括号是属性）：",
            f"🌟五星 {ssr*100:.1f}%：{row(POOL['5'])}",
            f"⭐四星 {sr*100:.1f}%：{row(POOL['4'])}",
            f"·三星 {r_rate*100:.1f}%：{row(POOL['3'])}",
            f"十连保底一个四星+，{self._pity()} 抽硬保底五星。",
            "属性相克：金克木、木克土、土克水、水克火、火克金。",
            "抽数靠聊天随机掉，或 /签到 领。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("限定池", alias={"UP池"})
    async def cmd_limited(self, event: AstrMessageEvent):
        await self._load_up_config()
        up = str(self._cfg("up_char", UP_DEFAULT) or "").strip()
        weight = self._cfg("up_weight", UP_WEIGHT_DEFAULT)
        try:
            weight = min(max(float(weight), 0.0), 1.0)
        except Exception:
            weight = UP_WEIGHT_DEFAULT
        five_names = [c["name"] for c in POOL["5"]]
        if up not in five_names:
            yield event.plain_result("当前没设限定卡，管理员发 /限定up <卡名> 指定。")
            return
        yield event.plain_result(
            f"限定UP：{up}\n五星里出这张的概率提升到 {weight*100:.0f}%，其余五星平分剩下的。"
        )

    @filter.command("限定up")
    async def cmd_limited_up(self, event: AstrMessageEvent, arg: str = ""):
        if not event.is_admin():
            yield event.plain_result("切换限定卡只有管理员能用。")
            return
        name = self._resolve_name(str(arg or ""))
        if not name:
            yield event.plain_result("用法：/限定up <五星卡名>，如 /限定up 大福鱼·限定")
            return
        if self._pool_index()[name]["star"] != "5":
            yield event.plain_result("限定卡得是五星，选张五星卡。")
            return
        await self._load_up_config()
        self.config["up_char"] = name
        try:
            await self.put_kv_data(
                "gacha_up", {"char": name, "weight": self._cfg("up_weight", UP_WEIGHT_DEFAULT)}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[抽卡机] 保存限定配置失败: {exc}")
        yield event.plain_result(f"限定卡已切到 {name}，下次抽五星就开始提概率。")

    @filter.command("成就")
    async def cmd_achievements(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        achieved = user.get("achievements", []) or []
        lines = []
        title = user.get("title") or ""
        if title:
            lines.append(f"当前称号：{title}")
        for a in ACHIEVEMENTS:
            mark = "✅" if a["id"] in achieved else "⬜"
            lines.append(f"{mark} {a['name']}：{a['desc']}")
        lines.append("抽到卡自动解锁并发奖。")
        yield event.plain_result("\n".join(lines))

    def _do_sign(self, user: dict) -> tuple[bool, str]:
        today = time.strftime("%Y-%m-%d")
        if user.get("last_sign") == today:
            return False, "今天签过到了，明天再来。"
        gift = int(self._cfg("daily_sign_draws", 3))
        user["last_sign"] = today
        user["draws"] = int(user.get("draws", 0)) + gift
        user["exp"] = int(user.get("exp", 0)) + 5
        user["coins"] = int(user.get("coins", 0)) + 100
        task_msg = self._complete_task(user, "sign")
        extra = f"\n{task_msg}" if task_msg else ""
        return True, f"签到了，送你 {gift} 抽、5 本经验书、100 肥鱼币。现在 {user['draws']} 抽。{extra}"

    @filter.command("签到")
    async def cmd_sign(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        nick = str(event.get_sender_name() or "")
        user = await self._get_user(uid)
        user["name"] = nick or user.get("name", "")
        _, msg = self._do_sign(user)
        await self._update_user(uid, user)
        yield event.plain_result(msg)

    @filter.command("每日任务", alias={"任务"})
    async def cmd_daily(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        self._daily_refresh(user)
        await self._update_user(uid, user)
        tasks = user["daily"]["tasks"]
        lines = ["今日任务："]
        for key, info in DAILY_TASKS.items():
            mark = "✅" if tasks.get(key) else "⬜"
            lines.append(f"{mark} {info['name']}（{_task_reward_text(info)}）")
        lines.append("完成自动发奖，每天 0 点刷新。")
        yield event.plain_result("\n".join(lines))

    @filter.command("副本")
    async def cmd_dungeons(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        stam = self._regen_stamina(user)
        await self._update_user(uid, user)
        power, _ = self._team_power(user)
        lines = [f"副本（队伍战力 {power}，体力 {stam}/{MAX_STAMINA}）："]
        for d in DUNGEONS:
            lines.append(f"{d['name']}（{d.get('element', '')}属性）｜需求战力 {d['power']}｜耗体力 {d['stamina']}｜{d['desc']}")
        lines.append("用法：/上阵 卡名 卡名 卡名 组队，/打副本 副本名 开打。")
        yield event.plain_result("\n".join(lines))

    @filter.command("上阵", alias={"组队"})
    async def cmd_team_set(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        names = [self._resolve_name(p) for p in str(arg or "").split()]
        names = [n for n in names if n]
        if not names:
            yield event.plain_result("用法：/上阵 卡名 卡名 卡名（最多 3 张，空格隔开）。")
            return
        seen: list[str] = []
        for n in names:
            if n not in seen:
                seen.append(n)
        names = seen[:3]
        bad = [n for n in names if n not in chars]
        if bad:
            yield event.plain_result(f"没抽到：{'、'.join(bad)}，先 /十连 抽。")
            return
        user["team"] = names
        await self._update_user(uid, user)
        power, _ = self._team_power(user)
        _, notes = self._team_bond(user, names)
        tail = f"（{'，'.join(notes)}）" if notes else ""
        yield event.plain_result(f"队伍上阵：{'、'.join(names)}。总战力 {power}{tail}。")

    @filter.command("队伍")
    async def cmd_team(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        power, detail = self._team_power(user)
        if not detail:
            yield event.plain_result("队伍空着，/上阵 卡名 卡名 卡名 组一下。")
            return
        _, notes = self._team_bond(user, user.get("team", []) or [])
        lines = [f"当前队伍（总战力 {power}）："]
        for d in detail:
            lines.append(f"{STAR_LABEL.get(d['star'], '?')} {d['name']} Lv.{d['level']} 战力 {d['power']}")
        if notes:
            lines.append("编队羁绊：" + "，".join(notes))
        yield event.plain_result("\n".join(lines))

    @filter.command("打副本", alias={"挑战", "打本"})
    async def cmd_dungeon(self, event: AstrMessageEvent, arg: str = ""):
        uid = str(event.get_sender_id() or "")
        user = await self._get_user(uid)
        d = self._find_dungeon(str(arg or ""))
        if not d:
            yield event.plain_result("没这副本，/副本 看看有哪些。")
            return
        stam = self._regen_stamina(user)
        if stam < d["stamina"]:
            yield event.plain_result(f"体力不够，要 {d['stamina']} 点（现在 {stam} 点）。")
            return
        _, team_detail = self._team_power(user)
        if not team_detail:
            yield event.plain_result("队伍空着，先 /上阵 组队。")
            return
        user["stamina"] = stam - d["stamina"]
        win, _power, detail, crit_rate, crit = self._battle_roll(user, d)
        lines = [f"挑战【{d['name']}】…"]
        rels = [x for x in detail if x.get("rel")]
        if rels:
            lines.append("元素克制：" + "、".join(f"{x['name']}({x['element']}·{x['rel']})" for x in rels))
        if crit:
            lines.append(f"触发暴击！(暴击率 {crit_rate*100:.0f}%)")
        if win:
            rw = self._apply_battle_rewards(user, d, True)
            lines.append(f"赢了，掉了 {rw['text_str']}。")
        else:
            rw = self._apply_battle_rewards(user, d, False)
            lines.append(f"没打过，捡了点零碎 {rw['text_str']}。")
        task_msg = self._complete_task(user, "dungeon")
        await self._update_user(uid, user)
        if task_msg:
            lines.append(task_msg)
        lines.append(f"剩体力 {user['stamina']}/{MAX_STAMINA}。")
        yield event.plain_result("\n".join(lines))

    @filter.command("送抽卡")
    async def cmd_give(self, event: AstrMessageEvent, amount: str = ""):
        if not event.is_admin():
            yield event.plain_result("送抽卡只有管理员能用。")
            return
        uid = str(event.get_sender_id() or "")
        try:
            n = int(str(amount).strip())
        except Exception:
            yield event.plain_result("用法：/送抽卡 <数量>，给当前用户加抽数。")
            return
        if n <= 0:
            yield event.plain_result("数量得是正数。")
            return
        user = await self._get_user(uid)
        user["draws"] = int(user.get("draws", 0)) + n
        await self._update_user(uid, user)
        yield event.plain_result(f"给 {user.get('name') or uid} 加了 {n} 抽，现在 {user['draws']} 抽。")

    @filter.command("看板绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        nick = str(event.get_sender_name() or "")
        session_str = str(event.unified_msg_origin or "")
        code = await self._get_or_create_bind(uid, session_str, nick)
        yield event.plain_result(
            f"你的看板码：{code}\n这个码固定不变，去抽卡主页填一次浏览器就记住了，以后不用再输。"
        )

    async def _get_or_create_bind(self, uid: str, session_str: str, nick: str) -> str:
        binds = await self.get_kv_data(BIND_KEY, None)
        if not isinstance(binds, dict):
            binds = {}
        for code, info in binds.items():
            if isinstance(info, dict) and str(info.get("uid") or "") == uid:
                binds[code] = {"uid": uid, "session": session_str, "name": nick}
                try:
                    await self.put_kv_data(BIND_KEY, binds)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[抽卡机] 更新绑定码失败: {exc}")
                return code
        code = "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)
        )
        binds[code] = {"uid": uid, "session": session_str, "name": nick}
        try:
            await self.put_kv_data(BIND_KEY, binds)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[抽卡机] 保存绑定码失败: {exc}")
        return code

    # ---------------- 聊天随机掉抽数 ----------------

    async def _gift_notify_delayed(self, event: AstrMessageEvent, text: str) -> None:
        """延迟发送掉抽提示，避免抢在主回复前。"""
        try:
            await asyncio.sleep(1.5)
            await event.send(MessageChain(chain=[Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[抽卡机] 掉抽提示发送失败: {exc}")

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=90,
    )
    async def _gift_on_message(self, event: AstrMessageEvent) -> None:
        """聊天随机掉抽数。"""
        try:
            if not (event.is_private_chat() or event.is_at_or_wake_command):
                return
            uid = str(event.get_sender_id() or "")
            if not uid:
                return
            user = await self._get_user(uid)
            user["name"] = str(event.get_sender_name() or "") or user.get("name", "")
            cooldown = max(int(self._cfg("gift_cooldown_sec", 180)), 0)
            now = time.time()
            if now - float(user.get("last_gift", 0)) < cooldown:
                return
            if random.random() >= float(self._cfg("gift_chance", 0.25)):
                return
            # 小概率多送
            roll = random.random()
            if roll < 0.005:
                n = 10
                tip = "中头彩了，塞你 10 抽"
            elif roll < 0.03:
                n = 3
                tip = "手气不错，塞你 3 抽"
            else:
                n = 1
                tip = "跟你唠得开心，顺手塞你 1 抽"
            user["last_gift"] = now
            user["draws"] = int(user.get("draws", 0)) + n
            task_msg = self._complete_task(user, "gift")
            await self._update_user(uid, user)
            tail = f"（现在 {user['draws']} 抽，/十连 走起）"
            if task_msg:
                tail += f"\n{task_msg}"
            asyncio.create_task(self._gift_notify_delayed(event, f"{tip}{tail}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[抽卡机] 掉抽数逻辑异常: {exc}")

    # ---------------- WebUI ----------------

    def _register_web_apis(self) -> None:
        routes = [
            ("/astrbot_plugin_gacha/pool", self._api_pool, ["GET"], "卡池与概率"),
            ("/astrbot_plugin_gacha/stats", self._api_stats, ["GET"], "抽卡统计与排行"),
            ("/astrbot_plugin_gacha/collection", self._api_collection, ["POST"], "我的收集图鉴"),
            ("/astrbot_plugin_gacha/pull", self._api_pull, ["POST"], "看板抽卡"),
            ("/astrbot_plugin_gacha/free_pull", self._api_free_pull, ["POST"], "看板免费一抽"),
            ("/astrbot_plugin_gacha/chars", self._api_chars, ["POST"], "我的角色养成"),
            ("/astrbot_plugin_gacha/sign", self._api_sign, ["POST"], "看板签到"),
            ("/astrbot_plugin_gacha/daily", self._api_daily, ["POST"], "每日任务"),
            ("/astrbot_plugin_gacha/shop", self._api_shop, ["POST"], "商店"),
            ("/astrbot_plugin_gacha/buy", self._api_buy, ["POST"], "购买"),
            ("/astrbot_plugin_gacha/rk", self._api_rk, ["POST"], "碎片升阶"),
            ("/astrbot_plugin_gacha/train", self._api_train, ["POST"], "喂经验书升级"),
            ("/astrbot_plugin_gacha/bt", self._api_bt, ["POST"], "突破"),
            ("/astrbot_plugin_gacha/dungeons", self._api_dungeons, ["POST"], "副本列表"),
            ("/astrbot_plugin_gacha/team", self._api_team, ["POST"], "上阵组队"),
            ("/astrbot_plugin_gacha/challenge", self._api_challenge, ["POST"], "挑战副本"),
            ("/astrbot_plugin_gacha/battle_turn", self._api_battle_turn, ["POST"], "副本选牌回合"),
            ("/astrbot_plugin_gacha/battle_quit", self._api_battle_quit, ["POST"], "退出副本战斗"),
            ("/astrbot_plugin_gacha/aff", self._api_aff, ["POST"], "摸摸互动"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[抽卡机] 注册接口 {route} 失败: {exc}")

    async def _api_pool(self):
        ssr = self._ssr_rate()
        sr = self._sr_rate()
        r_rate = max(1.0 - ssr - sr, 0.0)

        def card(c):
            return {
                "name": c["name"],
                "desc": c.get("desc", ""),
                "element": c.get("element", ""),
            }

        await self._load_up_config()
        up = str(self._cfg("up_char", UP_DEFAULT) or "").strip()
        weight = self._cfg("up_weight", UP_WEIGHT_DEFAULT)
        try:
            weight = min(max(float(weight), 0.0), 1.0)
        except Exception:
            weight = UP_WEIGHT_DEFAULT

        return jsonify(
            {
                "status": "ok",
                "pity": self._pity(),
                "rates": {"ssr": ssr, "sr": sr, "r": r_rate},
                "up": {"char": up, "weight": weight},
                "pool": {
                    "5": [card(c) for c in POOL["5"]],
                    "4": [card(c) for c in POOL["4"]],
                    "3": [card(c) for c in POOL["3"]],
                },
            }
        )

    async def _api_stats(self):
        state = await self._load_state()
        users = state.get("users", {}) or {}
        total_pulls = sum(int(u.get("total", 0)) for u in users.values())
        total_ssr = sum(int(u.get("ssr", 0)) for u in users.values())
        # 图鉴合计
        owned_sum: dict[str, int] = {}
        for u in users.values():
            for name, cnt in (u.get("owned") or {}).items():
                owned_sum[name] = owned_sum.get(name, 0) + int(cnt or 0)
        # 排行：按累计抽数
        ranking = sorted(
            (
                {
                    "name": u.get("name") or uid,
                    "draws": int(u.get("draws", 0)),
                    "total": int(u.get("total", 0)),
                    "ssr": int(u.get("ssr", 0)),
                }
                for uid, u in users.items()
            ),
            key=lambda x: x["total"],
            reverse=True,
        )[:20]
        # 非酋榜：抽得够多但还没出过五星的倒霉蛋，按累计抽数排
        pity_ranking = sorted(
            (
                {
                    "name": u.get("name") or uid,
                    "total": int(u.get("total", 0)),
                    "pity": int(u.get("pity", 0)),
                }
                for uid, u in users.items()
                if int(u.get("total", 0)) >= 10 and int(u.get("ssr", 0)) == 0
            ),
            key=lambda x: x["total"],
            reverse=True,
        )[:10]
        return jsonify(
            {
                "status": "ok",
                "users": len(users),
                "total_pulls": total_pulls,
                "total_ssr": total_ssr,
                "owned": owned_sum,
                "ranking": ranking,
                "pity_ranking": pity_ranking,
            }
        )

    async def _api_collection(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response(
                {"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"}
            )
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        owned = user.get("owned", {}) or {}
        full: list[dict[str, Any]] = []
        for star in ("5", "4", "3"):
            for c in POOL[star]:
                full.append(
                    {
                        "name": c["name"],
                        "star": star,
                        "desc": c.get("desc", ""),
                        "image": "cards/" + c.get("img", ""),
                        "count": int(owned.get(c["name"], 0) or 0),
                    }
                )
        achieved = user.get("achievements", []) or []
        self._daily_refresh(user)
        await self._update_user(uid, user)
        pity_left = self._pity() - int(user.get("pity", 0))
        raw_log = (user.get("pull_log", []) or [])[-10:]
        pull_log = [
            {"t": int(r.get("t", 0)), "star": r.get("star", "3"), "name": r.get("name", "?")}
            for r in reversed(raw_log)
        ]
        return json_response(
            {
                "status": "ok",
                "total_kinds": len(full),
                "collected_kinds": sum(1 for f in full if f["count"] > 0),
                "title": user.get("title") or "",
                "free_pull": int(user.get("free_pull", 0)),
                "pity_left": pity_left,
                "pull_log": pull_log,
                "achievements": [
                    {"id": a["id"], "name": a["name"], "desc": a["desc"], "done": a["id"] in achieved}
                    for a in ACHIEVEMENTS
                ],
                "full": full,
            }
        )

    async def _api_chars(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        stam = self._regen_stamina(user)
        await self._update_user(uid, user)
        idx = self._pool_index()
        chars = user.get("chars", {}) or {}
        char_list = []
        total_power = 0
        for name, c in sorted(chars.items()):
            entry = idx.get(name, {}) or {}
            star = entry.get("star", "3")
            img = (entry.get("char") or {}).get("img", "")
            lv = int(c.get("level", 1))
            bt = int(c.get("bt", 0))
            rk = int(c.get("rk", 0))
            aff = int(c.get("aff", 0))
            st = self._char_stats(star, lv, bt, rk, aff)
            total_power += st["power"]
            char_list.append(
                {
                    "name": name,
                    "star": star,
                    "element": (entry.get("char") or {}).get("element", ""),
                    "image": "cards/" + img if img else "",
                    "level": lv,
                    "bt": bt,
                    "max_bt": MAX_BT,
                    "bt_need_lv": (bt + 1) * 10 if bt < MAX_BT else 0,
                    "bt_need_mat": bt + 1 if bt < MAX_BT else 0,
                    "rk": rk,
                    "aff": aff,
                    "aff_max": AFF_MAX,
                    "shards": int(c.get("shards", 0)),
                    "rk_cost": 0 if rk >= MAX_RK else self._rk_cost(rk),
                    "max_rk": MAX_RK,
                    "max_lv": self._max_level(star, rk),
                    "atk": st["atk"],
                    "def": st["def"],
                    "hp": st["hp"],
                    "power": st["power"],
                    "char_exp": int(c.get("exp", 0)),
                    "need_exp": CHAR_BASE[star]["exp_lv"],
                }
            )
        mats = user.get("mats", {})
        return json_response(
            {
                "status": "ok",
                "coins": user.get("coins", 0),
                "exp": user.get("exp", 0),
                "mats": {"3": mats.get("3", 0), "4": mats.get("4", 0), "5": mats.get("5", 0)},
                "stamina": stam,
                "max_stamina": MAX_STAMINA,
                "total_power": total_power,
                "chars": char_list,
            }
        )

    async def _api_pull(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        try:
            times = int(body.get("times") or 0)
        except Exception:
            times = 0
        if times not in (1, 10):
            return json_response({"status": "error", "message": "只支持单抽或十连"})
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response(
                {"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"}
            )
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        user["name"] = str(info.get("name") or "") or user.get("name", "")
        if int(user.get("draws", 0)) < times:
            left = times - int(user.get("draws", 0))
            return json_response(
                {"status": "error", "message": f"抽数不够，还差 {left} 抽，去唠两句掉点抽数"}
            )
        user["draws"] = int(user.get("draws", 0)) - times
        results = self._pull(user, times)
        self._apply_results(user, results)
        task_msg = self._complete_task(user, "pull")
        ach_msgs = self._check_achievements(user)
        await self._update_user(uid, user)
        ssr_hits = [c for s, c in results if s == "5"]
        if ssr_hits:
            await self._notify_ssr(info, ssr_hits)
        return json_response(
            {
                "status": "ok",
                "draws": user["draws"],
                "pity_left": self._pity() - int(user["pity"]),
                "results": [
                    {"star": s, "name": c["name"], "desc": c["desc"]}
                    for s, c in results
                ],
                "ssr_hits": len(ssr_hits),
                "task_msg": task_msg,
                "ach_msgs": ach_msgs,
            }
        )

    async def _api_free_pull(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        user["name"] = str(info.get("name") or "") or user.get("name", "")
        self._daily_refresh(user)
        if int(user.get("free_pull", 0)) <= 0:
            return json_response({"status": "error", "message": "今天的免费一抽用完了，明天刷新"})
        user["free_pull"] = int(user.get("free_pull", 0)) - 1
        results = self._pull(user, 1)
        self._apply_results(user, results)
        task_msg = self._complete_task(user, "pull")
        ach_msgs = self._check_achievements(user)
        await self._update_user(uid, user)
        ssr_hits = [c for s, c in results if s == "5"]
        if ssr_hits:
            await self._notify_ssr(info, ssr_hits)
        return json_response(
            {
                "status": "ok",
                "free_pull": user["free_pull"],
                "pity_left": self._pity() - int(user["pity"]),
                "results": [
                    {"star": s, "name": c["name"], "desc": c["desc"]}
                    for s, c in results
                ],
                "ssr_hits": len(ssr_hits),
                "task_msg": task_msg,
                "ach_msgs": ach_msgs,
            }
        )

    async def _api_sign(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        user["name"] = str(info.get("name") or "") or user.get("name", "")
        signed, msg = self._do_sign(user)
        await self._update_user(uid, user)
        return json_response(
            {
                "status": "ok",
                "signed": signed,
                "message": msg,
                "draws": int(user.get("draws", 0)),
                "coins": int(user.get("coins", 0)),
                "exp": int(user.get("exp", 0)),
            }
        )

    async def _api_daily(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        self._daily_refresh(user)
        await self._update_user(uid, user)
        tasks = user["daily"]["tasks"]
        return json_response(
            {
                "status": "ok",
                "date": user["daily"].get("date", ""),
                "tasks": [
                    {"key": k, "name": info["name"], "done": bool(tasks.get(k)), "reward": _task_reward_text(info)}
                    for k, info in DAILY_TASKS.items()
                ],
            }
        )

    async def _api_shop(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        return json_response(
            {
                "status": "ok",
                "coins": int(user.get("coins", 0)),
                "items": [{"name": k, "coins": v["coins"]} for k, v in SHOP_ITEMS.items()],
            }
        )

    async def _api_buy(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        item_name = self._resolve_item(str(body.get("item") or ""))
        if not item_name:
            return json_response({"status": "error", "message": "没这商品"})
        try:
            qty = max(int(body.get("qty") or 1), 1)
        except Exception:
            qty = 1
        item = SHOP_ITEMS[item_name]
        cost = item["coins"] * qty
        user = await self._get_user(uid)
        coins = int(user.get("coins", 0))
        if coins < cost:
            return json_response({"status": "error", "message": f"肥鱼币不够，要 {cost} 币"})
        give = item["give"]
        if give.get("stamina"):
            self._regen_stamina(user)
            gain = int(give["stamina"]) * qty
            user["coins"] = coins - cost
            user["stamina"] = int(user.get("stamina", 0)) + gain
            await self._update_user(uid, user)
            return json_response({"status": "ok", "message": f"买了 {gain} 点体力", "coins": user["coins"], "stamina": user["stamina"]})
        user["coins"] = coins - cost
        if "exp" in give:
            user["exp"] = int(user.get("exp", 0)) + give["exp"] * qty
        if "mat" in give:
            mats = user.setdefault("mats", {"3": 0, "4": 0, "5": 0})
            mats[give["mat"]] = int(mats.get(give["mat"], 0)) + qty
        if "draws" in give:
            user["draws"] = int(user.get("draws", 0)) + give["draws"] * qty
        await self._update_user(uid, user)
        mats = user.get("mats", {})
        return json_response(
            {
                "status": "ok",
                "message": f"买了 {qty} 份「{item_name}」",
                "coins": int(user.get("coins", 0)),
                "exp": int(user.get("exp", 0)),
                "draws": int(user.get("draws", 0)),
                "mats": {"3": mats.get("3", 0), "4": mats.get("4", 0), "5": mats.get("5", 0)},
            }
        )

    async def _api_rk(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        name = self._resolve_name(str(body.get("name") or ""))
        if not name:
            return json_response({"status": "error", "message": "没这张卡"})
        if name not in chars:
            return json_response({"status": "error", "message": "你还没抽到这张卡"})
        idx = self._pool_index()
        star = idx[name]["star"]
        c = chars[name]
        rk = int(c.get("rk", 0))
        if rk >= MAX_RK:
            return json_response({"status": "error", "message": f"{name} 已经满阶"})
        cost = self._rk_cost(rk)
        shards = int(c.get("shards", 0))
        if shards < cost:
            return json_response({"status": "error", "message": f"碎片不够，要 {cost} 片（现在 {shards} 片）"})
        c["shards"] = shards - cost
        c["rk"] = rk + 1
        self._complete_task(user, "train")
        await self._update_user(uid, user)
        st = self._char_stats(star, int(c.get("level", 1)), int(c.get("bt", 0)), rk + 1)
        return json_response(
            {
                "status": "ok",
                "message": f"{name} 升到 {rk + 1} 阶",
                "rk": rk + 1,
                "shards": int(c.get("shards", 0)),
                "max_lv": self._max_level(star, rk + 1),
                "atk": st["atk"],
                "def": st["def"],
                "hp": st["hp"],
                "power": st["power"],
            }
        )

    async def _api_train(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        name = self._resolve_name(str(body.get("name") or ""))
        if not name:
            return json_response({"status": "error", "message": "没这张卡"})
        if name not in chars:
            return json_response({"status": "error", "message": "你还没抽到这张卡"})
        idx = self._pool_index()
        star = idx[name]["star"]
        base = CHAR_BASE[star]
        c = chars[name]
        level = int(c.get("level", 1))
        max_lv = self._max_level(star, int(c.get("rk", 0)))
        if level >= max_lv:
            return json_response({"status": "error", "message": f"{name} 已经满级（{max_lv} 级）"})
        total_exp = int(user.get("exp", 0))
        if total_exp <= 0:
            return json_response({"status": "error", "message": "没经验书，去 /签到 领，商店也能买"})
        want = total_exp
        try:
            q = int(body.get("qty") or 0)
            if q > 0:
                want = q
        except Exception:
            pass
        used = min(want, total_exp)
        if used <= 0:
            return json_response({"status": "error", "message": "要喂的经验书数量得是正数"})
        gain = used * EXP_PER_BOOK + int(c.get("exp", 0))
        while gain >= base["exp_lv"] and level < max_lv:
            gain -= base["exp_lv"]
            level += 1
        c["exp"] = gain
        c["level"] = level
        user["exp"] = total_exp - used
        task_msg = self._complete_task(user, "train")
        await self._update_user(uid, user)
        st = self._char_stats(star, level, int(c.get("bt", 0)), int(c.get("rk", 0)))
        return json_response(
            {
                "status": "ok",
                "message": f"{name} 升到 {level} 级，用了 {used} 本经验书" + (f"（{task_msg}）" if task_msg else ""),
                "level": level,
                "max_lv": max_lv,
                "exp": int(user.get("exp", 0)),
                "char_exp": gain,
                "need_exp": base["exp_lv"],
                "atk": st["atk"],
                "def": st["def"],
                "hp": st["hp"],
                "power": st["power"],
            }
        )

    async def _api_bt(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        name = self._resolve_name(str(body.get("name") or ""))
        if not name:
            return json_response({"status": "error", "message": "没这张卡"})
        if name not in chars:
            return json_response({"status": "error", "message": "你还没抽到这张卡"})
        idx = self._pool_index()
        star = idx[name]["star"]
        c = chars[name]
        bt = int(c.get("bt", 0))
        level = int(c.get("level", 1))
        if bt >= MAX_BT:
            return json_response({"status": "error", "message": f"{name} 已经满突破了"})
        need_lv = (bt + 1) * 10
        if level < need_lv:
            return json_response({"status": "error", "message": f"等级不够，{name} 要到 {need_lv} 级才能突破"})
        need_mat = bt + 1
        mats = user.setdefault("mats", {"3": 0, "4": 0, "5": 0})
        if int(mats.get(star, 0)) < need_mat:
            return json_response({"status": "error", "message": f"素材不够，要 {need_mat} 个{STAR_LABEL[star]}素材（现在 {mats.get(star, 0)} 个）"})
        mats[star] = int(mats[star]) - need_mat
        c["bt"] = bt + 1
        task_msg = self._complete_task(user, "train")
        await self._update_user(uid, user)
        st = self._char_stats(star, level, bt + 1, int(c.get("rk", 0)))
        return json_response(
            {
                "status": "ok",
                "message": f"{name} 突破到 {bt + 1} 阶" + (f"（{task_msg}）" if task_msg else ""),
                "bt": bt + 1,
                "atk": st["atk"],
                "def": st["def"],
                "hp": st["hp"],
                "power": st["power"],
                "mats": mats,
            }
        )

    async def _api_aff(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        name = self._resolve_name(str(body.get("name") or ""))
        if not name:
            return json_response({"status": "error", "message": "没这张卡"})
        if name not in chars:
            return json_response({"status": "error", "message": "你还没抽到这张卡"})
        idx = self._pool_index()
        star = idx[name]["star"]
        c = chars[name]
        today = time.strftime("%Y-%m-%d")
        pat = c.setdefault("aff_log", {})
        if pat.get(today):
            return json_response({"status": "error", "message": f"今天已经跟 {name} 互动过了，明天再来"})
        pat[today] = True
        for k in list(pat.keys()):
            if k != today:
                del pat[k]
        c["aff"] = min(AFF_MAX, int(c.get("aff", 0)) + AFF_PAT_DAILY)
        await self._update_user(uid, user)
        aff = int(c.get("aff", 0))
        st = self._char_stats(star, int(c.get("level", 1)), int(c.get("bt", 0)), int(c.get("rk", 0)), aff)
        return json_response(
            {
                "status": "ok",
                "message": f"摸了摸 {name}，好感 +{AFF_PAT_DAILY}",
                "aff": aff,
                "aff_max": AFF_MAX,
                "atk": st["atk"],
                "def": st["def"],
                "hp": st["hp"],
                "power": st["power"],
            }
        )

    async def _api_dungeons(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        stam = self._regen_stamina(user)
        await self._update_user(uid, user)
        power, detail = self._team_power(user)
        return json_response(
            {
                "status": "ok",
                "stamina": stam,
                "max_stamina": MAX_STAMINA,
                "power": power,
                "team": detail,
                "dungeons": [
                    {"id": d["id"], "name": d["name"], "desc": d["desc"], "element": d.get("element", ""), "power": d["power"], "stamina": d["stamina"], "drops": _drop_text(d)}
                    for d in DUNGEONS
                ],
            }
        )

    async def _api_team(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        chars = user.setdefault("chars", {})
        raw = body.get("names") or []
        if not isinstance(raw, list):
            return json_response({"status": "error", "message": "队伍数据不对"})
        names = []
        for p in raw:
            n = self._resolve_name(str(p or ""))
            if n and n not in names:
                names.append(n)
        names = names[:3]
        bad = [n for n in names if n not in chars]
        if bad:
            return json_response({"status": "error", "message": f"没抽到：{'、'.join(bad)}"})
        user["team"] = names
        await self._update_user(uid, user)
        power, detail = self._team_power(user)
        return json_response(
            {
                "status": "ok",
                "team": detail,
                "power": power,
                "message": f"队伍上阵：{'、'.join(names)}" if names else "队伍已清空",
            }
        )

    async def _api_challenge(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        user = await self._get_user(uid)
        d = self._find_dungeon(str(body.get("id") or ""))
        if not d:
            return json_response({"status": "error", "message": "没这副本"})
        stam = self._regen_stamina(user)
        if stam < d["stamina"]:
            return json_response({"status": "error", "message": f"体力不够，要 {d['stamina']} 点（现在 {stam} 点）"})
        _, team_detail = self._team_power(user)
        if not team_detail:
            return json_response({"status": "error", "message": "队伍空着，先上阵"})
        user["stamina"] = stam - d["stamina"]
        await self._update_user(uid, user)
        b = self._start_battle(user, d)
        self._battles[uid] = b
        return json_response(
            {
                "status": "ok",
                "battle": self._battle_state(b),
                "stamina": int(user.get("stamina", 0)),
            }
        )

    async def _api_battle_turn(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        if not uid:
            return json_response({"status": "error", "message": "绑定信息缺失，重新发 /看板绑定"})
        b = self._battles.get(uid)
        if not b or b.get("finished"):
            return json_response({"status": "error", "message": "没有进行中的战斗，先点挑战"})
        chosen = str(body.get("card") or "").strip()
        if not chosen:
            return json_response({"status": "error", "message": "还没选牌"})
        self._battle_turn(b, chosen)
        resp = {"status": "ok", "battle": self._battle_state(b)}
        if b.get("finished"):
            user = await self._get_user(uid)
            d = self._find_dungeon(b["dungeon_id"])
            if d:
                rewards = self._apply_battle_rewards(user, d, b["win"])
                b["rewards"] = rewards
            task_msg = self._complete_task(user, "dungeon")
            await self._update_user(uid, user)
            self._battles.pop(uid, None)
            resp["battle"] = self._battle_state(b)
            resp["task"] = task_msg
            resp["coins"] = int(user.get("coins", 0))
            resp["exp"] = int(user.get("exp", 0))
            resp["stamina"] = int(user.get("stamina", 0))
            resp["relics"] = user.get("relics", [])
        return json_response(resp)

    async def _api_battle_quit(self):
        body = await web_request.json(default={}) or {}
        code = str(body.get("code") or "").strip().upper()
        binds = await self.get_kv_data(BIND_KEY, None) or {}
        info = binds.get(code) if isinstance(binds, dict) else None
        if not info:
            return json_response({"status": "error", "message": "绑定码不对，去聊天里发 /看板绑定 拿一个"})
        uid = str(info.get("uid") or "")
        self._battles.pop(uid, None)
        return json_response({"status": "ok", "message": "已退出战斗，体力不退还"})

    async def _notify_ssr(self, info: dict, ssr_hits: list[dict]) -> None:
        try:
            session_str = str(info.get("session") or "")
            if not session_str:
                return
            names = "、".join(c["name"] for c in ssr_hits)
            await self.context.send_message(
                session_str,
                MessageChain(chain=[Plain(f"看板上出货了！抽到五星：{names}，去图鉴里看看。")]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[抽卡机] 看板出货通知发送失败: {exc}")
