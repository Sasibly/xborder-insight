"""聚类评测的主题簇定义与逐条归属（v2，单一事实源）。

## 为什么是 v2

v1 用「下标对」显式列同主题对（`SAME_PAIRS`），有两个硬缺陷：
1. 成本 O(n²)：51 条要判 120 对，扩到 216 条要判 2344 对。
2. 新增即漏判：追加一条会在组内新增 k 对，这些对不在清单里，于是默认判成
   「不同主题」。若它其实是同主题，就是凭空造出 k 个漏检，而 F1 只是轻微下降，
   看起来像正常波动，不会报警。

v2 改为「每条 evidence 归属一个主题簇」，人工成本 O(n)，pairs 由归属无损派生。

## 簇的粒度（唯一判定标准）

    「同一条改进行动，能同时解决这两条 evidence 反映的问题吗？」

同簇 ⇔ 答案是「能」。由这条标准推出：
1. **极性不同必然不同簇**。提升 vs 维持是两种行动。
2. **簇 = 一个部件/环节的一个改进项**。同一部件上的不同失效方式归同簇——
   「外壳开裂」和「外壳有毛刺」由同一次结构/用料/模具改动解决。
3. **不同部件 → 不同簇**，哪怕 taxonomy label 相同。
   「按键失灵」与「触点腐蚀」需要不同工程改动。
4. **笼统无法定位到部件的抱怨**（"做工差"、"不值"）归入该 label 的 `generic_*` 簇。
5. **多失效混杂的单条 evidence，取第一个可定位的具体失效。**
   如「做工一般，盖子扣不紧，边缘有毛刺」记为 `housing`（首个具体项）。

第 2 条与 v1 的差异是有意的：v1 的 `flimsy_build` 把「连接器裂」「内部松」
「盖扣不紧」「有毛刺」塞进同一个簇，这 4 项需要不同的工程改动，违反了它自己
声明的规则（"不同部件、不同失效方式 → 不同组"）。粗簇的后果是聚类只要按极性
分两组就满分，评测对语义理解零区分力。

## 评测分组

组 = **(dataset, label)**，不是 label。

生产里 `analyze_reviews` 一次只处理一个 dataset，聚类天然限定在单品类内。
按 label 跨品类分组会把「无线充充电慢」和「咖啡机出水慢」放进同一组，
这是生产不会发生的场景。扩到 6 品类后这个隐含假设才暴露出来。

跨 dataset 的对不参与评测：它们不在同一组，永远不会被聚类合并。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "eval" / "cluster_evidence.jsonl"
PAIRS = ROOT / "data" / "eval" / "cluster_pairs.jsonl"

# 簇 id -> 语义（簇 id 只在同 label 内唯一，全局键是 "label/theme"）
THEME_SPEC: dict[str, dict[str, str]] = {
    "charging_speed": {
        "slow_charge": "无线充：充电慢/未达宣传速度 —— 提升充电功率与稳定性",
        "fast_charge": "无线充：充电快 —— 维持充电速度",
        "slow_flow": "咖啡机：出水/流量过慢过小 —— 提升泵压与流道",
        "slow_warmup": "咖啡机：预热/就绪耗时过长 —— 缩短预热时间",
        "slow_grind": "咖啡机：研磨耗时过长 —— 提升研磨效率",
        "slow_boil": "电水壶：烧水慢 —— 提升加热功率",
        "fast_boil": "电水壶：烧水快 —— 维持加热效率",
        "slow_recharge": "扫地机：回充/充电耗时长 —— 提升充电功率",
        "wet_mop": "扫地机：拖后地面过湿 —— 改进出水量控制",
        "weak_suction": "扫地机：吸力弱 —— 提升吸力",
    },
    "heat": {
        "overheat": "机身/接触面发热烫手、过热断充 —— 改善散热",
        "cool_running": "运行不烫手 —— 维持散热表现",
        "room_heating": "排风导致环境升温 —— 改进排风热管理",
        "burnt_smell": "受热产生焦糊/塑料异味 —— 改进耐温材料或温控",
        "heater_failure": "不加热/加热失效/接触不良 —— 提升加热元件可靠性",
        "keep_warm_ok": "保温功能有效 —— 维持保温表现",
    },
    "build_quality": {
        "housing": "外壳/盖/铰链/支架/轮轴等结构件开裂、变形、松动、扣不紧、毛刺 —— 改进结构强度与模具精度",
        "electrical": "按键、开关、触点、温控等电气件失效或腐蚀 —— 提升电气件可靠性",
        "surface": "生锈、涂层脱落 —— 改进表面处理与防锈材质",
        "generic_build": "笼统的做工/用料差、廉价感，无法定位到具体部件 —— 整体提升用料与制造水平",
        "connector": "线缆连接器开裂 —— 提升连接器强度",
        "internal": "内部结构松散 —— 提高装配公差",
        "filter_mesh": "可拆洗滤网薄、缝线撕裂 —— 加厚加固滤网",
        "seal": "密封胶圈变形 —— 改进密封件材质",
        "odor": "塑料味 —— 改进材质或去味工艺",
        "dust_trap": "外壳缝隙积灰无法清理 —— 改进缝隙设计",
        "solid_build": "做工扎实 —— 维持用料水平",
    },
    "noise": {
        "generic_noise": "整体运行噪音大、扰人 —— 综合降噪",
        "rattle": "咔咔、共振、敲击类异响 —— 消除松动与共振",
        "whistle": "高频啸叫 —— 改进风道/出风结构",
        "crackle": "加热时爆裂声 —— 处理加热管结垢与电气接触",
        "fan_noise": "风扇噪音 —— 改进风扇与风道",
        "pump_noise": "泵噪音 —— 改进泵体减振",
        "grinder_noise": "研磨噪音 —— 改进研磨机构降噪",
        "motor_noise": "电机嗡嗡声 —— 改进电机减振",
        "quiet_operation": "运行安静 —— 维持噪音水平",
    },
    "cleaning": {
        "hard_to_clean": "难拆难洗、耗时、边角发霉 —— 降低清洁负担",
        "debris_buildup": "毛絮/碎屑积聚 —— 改进结构减少积存",
        "pod_stuck": "胶囊卡在腔内难以取出 —— 改进胶囊腔脱模",
        "water_pooling": "机器底部积水 —— 改进排水结构",
        "scale_buildup": "水垢结块难清除 —— 改进加热面防垢",
        "ineffective_cleaning": "清洁效果差（拖布只抹不吸、吸力不足、边角够不到）—— 提升清洁能力",
        "easy_to_clean": "清洗方便 —— 维持清洁便利性",
    },
    "pump_life": {
        "pump_failure": "泵早期失效、只出蒸汽不出水 —— 提升泵寿命",
        "motor_failure": "电机烧毁/停转 —— 提升电机寿命",
        "fan_failure": "风扇停转、轴承失效 —— 提升风扇寿命",
        "coil_failure": "线圈烧毁 —— 提升线圈耐热与电气可靠性",
        "grinder_failure": "研磨机构停转 —— 提升研磨机构寿命",
        "filter_clog": "滤芯堵塞快 —— 改进滤芯结构或更换周期",
        "power_failure": "无法开机/断电 —— 提升电源与主板可靠性",
        "button_wear": "按键手感失效、失去段落感 —— 提升按键寿命",
        "charging_failure": "完全无法充电 —— 改进充电回路",
        "battery_degradation": "电池续航骤降 —— 改进电芯或电量管理",
        "parts_availability": "耗材磨损后原厂缺货、第三方装不上 —— 改进耗材供应与通用性",
        "generic_death": "笼统的「用不久就坏了」，无法定位部件 —— 整体提升可靠性",
    },
    "size_fit": {
        "too_big": "体积/占地偏大、放不下 —— 缩小体积或改结构",
        "too_small": "容量/面积偏小 —— 增大尺寸",
        "case_blocks": "带壳无法充电/贴合 —— 提升带壳识别距离",
        "filter_incompatible": "第三方滤芯尺寸不合、装不到位 —— 统一滤芯规格",
        "pod_incompatible": "第三方胶囊装不进 —— 统一胶囊规格",
        "lid_closure": "盖子扣不紧 —— 改进盖锁结构",
    },
    "leakage": {
        "seal_leak": "底部/接缝/接口/密封圈渗漏 —— 改进密封结构",
        "overflow": "水位失控溢出 —— 改进水位控制",
        "group_head_drip": "冲煮头静置滴水 —— 改进阀门密封",
        "spout_drip": "壶嘴倒水后滴漏 —— 改进壶嘴结构",
        "lid_pops_open": "倒水时盖子弹开 —— 改进盖锁",
    },
    "value": {
        "overpriced": "整体定价偏高、性价比不足 —— 调整定价或提升感知价值",
        "filter_tco": "耗材单价与更换频率导致长期成本高 —— 降低耗材 TCO",
        "good_value": "价格合适、性价比好 —— 维持定价策略",
    },
    "compatibility": {
        "case_incompatible": "带壳不兼容 —— 提升带壳识别距离",
        "port_loose": "接口松动、碰触断电 —— 改进接口结构",
        "consumable_incompatible": "第三方耗材尺寸不合、被机器拒识 —— 统一耗材规格或放宽识别",
        "lock_in": "只能用原厂耗材 —— 开放或兼容第三方",
        "app_connectivity": "App 掉线、掉绑定、需重复配对 —— 提升连接稳定性",
        "app_defect": "App 功能缺失（无重置键）—— 补齐功能",
        "wifi_band": "不支持 5GHz 配网失败 —— 支持双频",
        "works_with_device": "机型兼容良好 —— 维持兼容性",
    },
}

# label -> {theme: [组内下标]}；组内下标 = 该 label 在 evidence 文件中的出现顺序（0 起）
THEME_ASSIGN: dict[str, dict[str, list[int]]] = {
    "charging_speed": {
        "slow_charge": [0, 2, 4, 5, 6, 7],
        "fast_charge": [1, 3, 8],
        "slow_flow": [9, 10, 11],
        "slow_warmup": [12],
        "slow_grind": [13],
        "slow_boil": [14, 16, 18],
        "fast_boil": [15, 17, 19],
        "slow_recharge": [20, 21],
        "wet_mop": [22],
        "weak_suction": [23],
    },
    "heat": {
        "overheat": [0, 2, 3, 4, 5, 7, 10, 11, 12, 15, 20, 21],
        "cool_running": [1, 14],
        "room_heating": [6],
        "burnt_smell": [8, 18],
        "heater_failure": [9, 13, 16, 19],
        "keep_warm_ok": [17],
    },
    "build_quality": {
        "housing": [5, 6, 7, 11, 14, 21, 26, 29, 30],
        "electrical": [15, 20, 22, 23, 25],
        "surface": [10, 16, 18, 19],
        "generic_build": [4, 12, 13, 17, 28],
        "connector": [0],
        "internal": [2],
        "filter_mesh": [8],
        "seal": [27],
        "odor": [24],
        "dust_trap": [9],
        "solid_build": [1, 3],
    },
    "noise": {
        "generic_noise": [3, 5, 7, 10, 15, 17, 19, 22, 23, 24],
        "rattle": [6, 9, 18, 26],
        "whistle": [11, 12],
        "crackle": [20],
        "fan_noise": [0],
        "pump_noise": [1, 2, 16],
        "grinder_noise": [14],
        "motor_noise": [25],
        "quiet_operation": [4, 8, 13, 21, 27],
    },
    "cleaning": {
        "hard_to_clean": [0, 1, 3, 4, 6, 7, 8, 9, 12, 17],
        "debris_buildup": [2],
        "pod_stuck": [11],
        "water_pooling": [13],
        "scale_buildup": [14, 15, 16],
        "ineffective_cleaning": [18, 19, 20, 21],
        "easy_to_clean": [5, 10],
    },
    "pump_life": {
        "pump_failure": [1, 2, 9, 10],
        "motor_failure": [4, 8, 15],
        "fan_failure": [5, 6],
        "coil_failure": [0],
        "grinder_failure": [11],
        "filter_clog": [3],
        "power_failure": [7],
        "button_wear": [12],
        "charging_failure": [16],
        "battery_degradation": [18],
        "parts_availability": [20],
        "generic_death": [13, 14, 17, 19],
    },
    "size_fit": {
        "too_big": [0, 1, 5, 7, 8, 10, 11, 13, 15, 16, 17, 18],
        "too_small": [3, 14],
        "case_blocks": [2],
        "filter_incompatible": [4, 6],
        "pod_incompatible": [9],
        "lid_closure": [12],
    },
    "leakage": {
        "seal_leak": [0, 1, 3, 4, 5, 7, 8, 9, 10, 11, 15, 17, 18],
        "overflow": [2, 16],
        "group_head_drip": [6],
        "spout_drip": [13, 14],
        "lid_pops_open": [12],
    },
    "value": {
        "overpriced": [0, 1, 4, 6, 10, 12, 14, 15],
        "filter_tco": [2, 3, 5, 8],
        "good_value": [7, 9, 11, 13],
    },
    "compatibility": {
        "consumable_incompatible": [4, 7, 10, 13],
        "app_connectivity": [5, 6, 11],
        "wifi_band": [9, 12],
        "case_incompatible": [0],
        "works_with_device": [1],
        "port_loose": [2],
        "app_defect": [3],
        "lock_in": [8],
    },
}


def load_evidence() -> list[dict]:
    return [
        json.loads(line)
        for line in EVIDENCE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def apply_themes(evidence: list[dict]) -> list[dict]:
    """按 THEME_ASSIGN 写入 theme 字段；返回新列表，不修改入参。"""
    position: dict[str, int] = {}
    out: list[dict] = []
    for row in evidence:
        label = row["label"]
        idx = position.get(label, 0)
        position[label] = idx + 1
        assign = THEME_ASSIGN.get(label, {})
        theme = next((t for t, idxs in assign.items() if idx in idxs), None)
        if theme is None:
            raise ValueError(f"{row['eid']}（{label} 组内第 {idx} 条）没有 theme 归属")
        out.append({**row, "theme": theme})
    return out


def group_key(row: dict) -> str:
    """生产聚类分组键：dataset 是外层（一次只处理一个品类），label+polarity 是内层。

    极性必须进键：向量度量的是语义主题，分不出「充电快」和「充电慢」——
    实测这两条余弦相似度 0.872，是本数据里最高的跨极性对。极性由分组键隔离，
    不是靠阈值分开。
    """
    return f"{row['dataset']}::{row['label']}::{row['polarity']}"


def derive_pairs(evidence: list[dict]) -> list[dict]:
    """按 (dataset, label) 组内派生出全部组合对。

    全组合而非抽样：派生零人工成本，抽样唯一作用是省标注钱——这里不花钱。
    抽样会人为改变正负比例，让新旧口径不可比，也把抽样噪声混进 F1。
    """
    groups: dict[str, list[int]] = {}
    for idx, row in enumerate(evidence):
        groups.setdefault(group_key(row), []).append(idx)

    out: list[dict] = []
    for gk, members in groups.items():
        if len(members) < 2:
            continue
        rows = [evidence[i] for i in members]
        label = rows[0]["label"]
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                out.append(
                    {
                        "group": gk,
                        "dataset": a["dataset"],
                        "label": label,
                        "a_idx": i,
                        "b_idx": j,
                        "a": a["evidence"],
                        "b": b["evidence"],
                        "a_polarity": a["polarity"],
                        "b_polarity": b["polarity"],
                        "a_theme": a["theme"],
                        "b_theme": b["theme"],
                        "same": a["theme"] == b["theme"],
                    }
                )
    return out


def validate(evidence: list[dict]) -> list[str]:
    """结构校验。返回问题列表，空表示通过。

    这几种错误都属于「不校验就会静默出错」：
    - theme 缺失或不在 THEME_SPEC → pair 判定失去意义
    - 跨极性同簇 → 违反唯一标准的第 1 条
    - 下标重复 / 漏覆盖 → 有 evidence 会拿不到 theme，或被错误归类
    """
    problems: list[str] = []

    for label, assign in THEME_ASSIGN.items():
        seen: dict[int, str] = {}
        for theme, idxs in assign.items():
            if theme not in THEME_SPEC.get(label, {}):
                problems.append(f"{label}/{theme} 不在 THEME_SPEC")
            for i in idxs:
                if i in seen:
                    problems.append(f"{label} 组内第 {i} 条被重复归属：{seen[i]} 与 {theme}")
                seen[i] = theme
        expected = sum(1 for r in evidence if r["label"] == label)
        if seen and len(seen) != expected:
            missing = sorted(set(range(expected)) - set(seen))
            problems.append(f"{label} 应 {expected} 条，实际归属 {len(seen)} 条，缺 {missing[:8]}")

    for label in {r["label"] for r in evidence}:
        if label not in THEME_ASSIGN:
            problems.append(f"{label} 没有任何 theme 归属定义")

    pol: dict[tuple[str, str], set[str]] = {}
    for row in evidence:
        pol.setdefault((row["label"], row.get("theme")), set()).add(row["polarity"])
    for (label, theme), ps in pol.items():
        if theme and len(ps) > 1:
            problems.append(f"{label}/{theme} 跨极性同簇：{sorted(ps)}")

    return problems


def main() -> int:
    raw = load_evidence()
    problems = validate(raw)
    if problems:
        print("校验未通过：")
        for p in problems:
            print("  -", p)
        return 1

    evidence = apply_themes(raw)
    EVIDENCE.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in evidence) + "\n", encoding="utf-8"
    )
    pairs = derive_pairs(evidence)
    PAIRS.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in pairs) + "\n", encoding="utf-8"
    )

    total = len(pairs)
    same = sum(1 for r in pairs if r["same"])
    n_themes = sum(len(v) for v in THEME_ASSIGN.values())
    print(f"evidence {len(evidence)} 条 · 主题簇 {n_themes} 个")
    print(f"pairs {total} 对：同主题 {same} · 不同主题 {total - same}（正例占 {same / total * 100:.1f}%）")

    by_label: dict[str, list[int]] = {}
    for r in pairs:
        stat = by_label.setdefault(r["label"], [0, 0])
        stat[0 if r["same"] else 1] += 1
    for label, (s, d) in sorted(by_label.items(), key=lambda x: -(x[1][0] + x[1][1])):
        print(f"  {label:16s} 同 {s:3d} · 异 {d:3d}")

    cross = sum(1 for r in pairs if r["a_polarity"] != r["b_polarity"])
    print(f"  跨极性对 {cross} 对（应为 0：极性已在标注层隔离）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
