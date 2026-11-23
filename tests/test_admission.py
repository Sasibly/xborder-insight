"""候选准入（`docs/04` §18.7）：把「这个品能不能做」变成一条命令。

门禁守两件事：

1. **算出来的数与文档表一致**。五台的采购上限（$16.74 / $14.90 / ...）是
   `docs/07` H21 谈判桌上要用的数，它们不能随代码重构悄悄漂。
2. **缺项不给默认值**。这是本模块存在的主要风险：候选往往缺税率、缺 FBA，
   一旦填个「大概 12.5%」放行，毛利率会静默高 1–11 个点，输出上没有任何痕迹。

FBA 值是固定的六位小数（来源：`python scripts/db.py fba-envelope --dims ... --exact-weight`），
不在这里重算——重算会把两个测试绑死在一起，改一处两边都红。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import admission as adm

# (name, price, cogs, duty, fba) —— 五台的已知量，与 tests/test_five_sku_margins.py 同源
FIVE = [
    ("air_purifier", 79.99, 32.50, 0.125, 23.83605),
    ("coffee_machine", 63.99, 18.50, 0.237, 14.39685),
    ("electric_kettle", 29.99, 6.80, 0.152, 10.85715),
    ("robot_vacuum", 199.99, 118.00, 0.125, 25.51275),
    ("pet_fountain", 45.99, 14.50, 0.125, 9.36675),
]


def _cand(name, price, cogs, duty, fba, **kw):
    return adm.Candidate(
        name=name,
        selling_price=price,
        quoted_cost=cogs,
        duty_rate=duty,
        fba_fee=fba,
        **kw,
    )


@pytest.mark.parametrize(
    ("name", "price", "cogs", "duty", "fba", "max_cogs"),
    [
        ("air_purifier", 79.99, 32.50, 0.125, 23.83605, 16.74),
        ("coffee_machine", 63.99, 18.50, 0.237, 14.39685, 14.90),
        ("electric_kettle", 29.99, 6.80, 0.152, 10.85715, 1.13),
        ("robot_vacuum", 199.99, 118.00, 0.125, 25.51275, 79.85),
        ("pet_fountain", 45.99, 14.50, 0.125, 9.36675, 11.05),
    ],
)
def test_max_cogs_matches_the_negotiation_table(name, price, cogs, duty, fba, max_cogs):
    """§17 / H21 那五张谈判目标价，必须与命令算出来的一致（±1 分）。

    这些数是拿到供应商桌上说的，差一分都会被对方抓住。
    """
    result = adm.evaluate(_cand(name, price, cogs, duty, fba))
    assert result.verdict == adm.VERDICT_FAIL
    assert result.max_cogs == pytest.approx(max_cogs, abs=0.01)


@pytest.mark.parametrize(("name", "price", "cogs", "duty", "fba"), FIVE)
def test_solution_put_back_gives_exactly_target(name, price, cogs, duty, fba):
    """把解代回去必须正好踩在 15%——不能是 15.3% 或者 14.8%。

    这条是解本身的正确性校验：二分只要区间或收敛条件写错，
    上面那条「与文档一致」可能一起错，这一条不会。
    """
    result = adm.evaluate(_cand(name, price, cogs, duty, fba))
    assert result.max_cogs is not None
    at_max = adm.evaluate(_cand(name, price, result.max_cogs, duty, fba))
    assert at_max.gross_margin == pytest.approx(adm.TARGET_MARGIN, abs=1e-6)


def test_referral_drop_flips_coffee_machine_only():
    """佣金 8% 时咖啡机 15.02% 转 pass——这正是 H1 的全部价值。

    其余四台在 8% 下仍然 fail，所以「唯一值得谈的那台」这个判断成立。
    """
    flipped = []
    for name, price, cogs, duty, fba in FIVE:
        result = adm.evaluate(
            _cand(name, price, cogs, duty, fba, referral_rate=0.08)
        )
        if result.verdict == adm.VERDICT_PASS:
            flipped.append(name)
            assert result.gross_margin == pytest.approx(0.1502, abs=1e-4)
    assert flipped == ["coffee_machine"]


def test_missing_duty_is_need_info_not_a_guess():
    """不给税率 → need_info，并明确说要先归类。

    这条防的是「顺手填 12.5%」：净化器 12.5% 与 15.9% 差 $1.11/件，
    而输出上只会显示一个正常的两层小数毛利率。
    """
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=18.0, duty_rate=None,
                      fba_fee=10.0)
    )
    assert result.verdict == adm.VERDICT_NEED_INFO
    assert any("duty_rate" in item for item in result.missing)
    assert result.gross_margin is None and result.max_cogs is None


def test_missing_fba_is_need_info():
    """不给 FBA 也不给占比 → need_info。FBA 是每件 $2.4–8.6 的量级，不能默认 0。"""
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=18.0, duty_rate=0.125)
    )
    assert result.verdict == adm.VERDICT_NEED_INFO
    assert any("FBA" in item for item in result.missing)


def test_fba_ratio_is_a_valid_shortcut():
    """只有「品类典型 FBA 占比」时也能算——报价阶段通常就是这个状态。

    $59.99 × 15% = $9.00 的 FBA，采购 $14 时毛利 24.4%：这一台是**能做**的。
    它同时说明闸一的 20% 比例段（$59.99 × 20% = $12.00）确实保守——
    真实上限是 $18.90（占售价 31.5%，正好是 §18.3 表「$60 × FBA 15%」那格），
    所以闸一只是粗筛，不是判据。
    """
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=14.0,
                      duty_rate=0.125, fba_ratio=0.15)
    )
    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.2438, abs=1e-3)


def test_all_missing_reported_at_once():
    """缺项一次报全，不要挤牙膏——候选通常一次缺三四样。

    四类：售价、采购、税率、FBA（后者可由 fba_ratio 顶替，但这里两者都没给）。
    """
    result = adm.evaluate(adm.Candidate(name="x", selling_price=None,
                                        quoted_cost=None, duty_rate=None))
    assert result.verdict == adm.VERDICT_NEED_INFO
    assert len(result.missing) == 4


@pytest.mark.parametrize(
    ("price", "cogs", "verdict", "band", "limit"),
    [
        (63.99, 18.50, adm.VERDICT_FAIL, "amount", 18.30),
        (63.99, 18.30, adm.VERDICT_PASS, "amount", 18.30),
        (59.00, 18.30, adm.VERDICT_PASS, "amount", 18.30),
        (58.99, 18.30, adm.VERDICT_FAIL, "ratio", 11.798),
        (40.00, 8.00, adm.VERDICT_PASS, "ratio", 8.00),
        (39.99, 5.00, adm.VERDICT_NEED_INFO, "size_first", None),
        (30.00, 1.20, adm.VERDICT_NEED_INFO, "size_first", None),
    ],
)
def test_gate_three_bands(price, cogs, verdict, band, limit):
    """闸一的三段与两个边界（$59 / $40）——`docs/04` §18.7 那张分段表。

    边界值本身也测：$59.00 落「金额段」，$58.99 落「比例段」。
    分界写错一位，就会有一整档候选被判错，而这种错不会报错。
    """
    gate = adm.screening_gate(price, cogs)
    assert gate.verdict == verdict
    assert gate.band == band
    if limit is None:
        assert gate.limit is None
    else:
        assert gate.limit == pytest.approx(limit, abs=0.01)


def test_gate_would_wrongly_kill_a_workable_candidate():
    """闸一判 fail、闸二判 pass 的样本：$80 售价 + $20 采购 + FBA $20.09。

    结论必须以闸二为准，并且 `notes` 要把这件事说出来——两道闸打架时，
    读者会挑自己爱听的那个，所以不能只留两个结论了事。

    这个样本的自纠史写在 `docs/04` §18.7 末尾：最初算成 FBA $8.09，
    是把「目标毛利额」在成本侧又扣了一次，实际 $8.09 时毛利是 **30%**，
    15% 对应的 FBA 上限是 **$20.09**。差的那 $12 正是 15% × $80。
    """
    result = adm.evaluate(_cand("sample", 80.0, 20.0, 0.125, 20.09))
    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.15, abs=5e-4)
    assert result.gate is not None and result.gate.verdict == adm.VERDICT_FAIL
    assert any("误杀" in note for note in result.notes)


def test_gate_min_price_constant_is_derived_not_guessed():
    """$59 这个分界是从 $18.30 反解出来的，不是拍的。

    改了 `GATE_AMOUNT` 就必须同步重算它，否则金额段会放进做不成的候选。
    这条把两者的关系钉住。
    """
    needed = adm.min_price_for_cogs(
        adm.GATE_AMOUNT, 8.95, 0.125
    )
    assert needed == pytest.approx(adm.GATE_AMOUNT_MIN_PRICE, abs=0.25)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_zero_or_negative_input_is_invalid_not_need_info(bad):
    """0 与负数不是「还判不了」，是输入不成立——两者要分开，否则会被当成待补数据。"""
    with pytest.raises(adm.AdmissionInputInvalid):
        adm.evaluate(
            adm.Candidate(name="x", selling_price=bad, quoted_cost=10.0,
                          duty_rate=0.125, fba_fee=8.0)
        )


def test_overrun_equals_margin_gap():
    """fail 时「超预算点数」与「毛利缺口」是同一个数（§18.2 的恒等式）。"""
    for name, price, cogs, duty, fba in FIVE:
        result = adm.evaluate(_cand(name, price, cogs, duty, fba))
        assert result.overrun_pts == pytest.approx(
            (adm.TARGET_MARGIN - result.gross_margin) * 100, abs=1e-9
        )


# --- CSV 批量：三种「空」必须分开 -------------------------------------------

def test_parse_rows_accepts_english_and_chinese_headers():
    """列名不敏感：`price` / `售价`、`duty` / `税率`、`23.7%` / `0.237` 都收。

    批量入口的输入是人手填的表，写法一定不统一。要求统一格式的结果是
    大家回去手工改表，然后改错——不如在解析层容忍。
    """
    rows = [
        {"name": "a", "price": "$63.99", "cogs": "18.50", "duty": "23.7%", "fba": "14.40"},
        {"name": "b", "售价": "1,280.00", "采购价": "20", "税率": "0.125", "fba占比": "15%"},
    ]
    candidates, problems = adm.parse_rows(rows)
    assert problems == []
    assert candidates[0].selling_price == pytest.approx(63.99)
    assert candidates[0].duty_rate == pytest.approx(0.237)
    assert candidates[1].selling_price == pytest.approx(1280.0)
    assert candidates[1].fba_ratio == pytest.approx(0.15)
    assert candidates[1].duty_rate == pytest.approx(0.125)


def test_empty_cell_is_unknown_not_an_error():
    """空格 = 「我不知道」→ need_info；不是格式错。

    两者混同的后果：一张表填错了几格，批量跑完得到一片 need_info，
    看起来像数据没准备好，实际是表填错了——不会有人回头看第二遍。
    """
    rows = [{"name": "x", "price": "45", "cogs": "9", "duty": "", "fba": ""}]
    candidates, problems = adm.parse_rows(rows)
    assert problems == []
    assert candidates[0].duty_rate is None
    result = adm.evaluate(candidates[0])
    assert result.verdict == adm.VERDICT_NEED_INFO


def test_bad_number_is_reported_per_line_and_skipped():
    """解析不了 → 报行号并跳过该行。行号按表格算（表头是第 1 行），改表时能直接定位。"""
    rows = [
        {"name": "ok", "price": "45", "cogs": "9"},
        {"name": "bad", "price": "abc", "cogs": "9"},
    ]
    candidates, problems = adm.parse_rows(rows)
    assert [c.name for c in candidates] == ["ok"]
    assert len(problems) == 1
    assert problems[0].line == 3
    assert "数值格式错" in problems[0].message


def test_missing_required_column_raises():
    """整列缺失是表结构问题，抛错而不是逐行 need_info——否则会得到一片「还判不了」。"""
    with pytest.raises(adm.AdmissionInputInvalid):
        adm.parse_rows([{"name": "x", "cogs": "9"}])


def test_explicit_fba_wins_over_ratio():
    """同时给 fba 与 fba_ratio 时以 fba 为准：前者是量过的，后者是品类典型值。"""
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=14.0,
                      duty_rate=0.125, fba_fee=5.0, fba_ratio=0.30)
    )
    assert result.gross_margin > 0.25  # 用 0.30 占比（$18）时只有 9.4%


# ---------- 广告空间线：两条线不能混用（`docs/04` §22） -----------------------
#
# 15% 线回答「这单还剩多少」，20% 线回答「剩下的够不够把货卖出去」。
# 盈亏平衡 ACOS 数值上等于毛利率（`profit.py`），所以后者严格更紧——
# 拿 15% 线的上限去开口，谈成的价仍然只是「有毛利、投不出量」。
#
# 这两个 FBA 是 §21.3 的现行口径（净化器新箱 $11.35、咖啡机 A 口径 $14.40），
# 与上面 FIVE 里 11-21 那组（净化器还按 24×15×15 = $23.84）不是同一批数，
# 刻意分开写：混用会让 §17 的历史表和 §21 的现行表互相打脸。

CURRENT = [
    ("air_purifier", 79.99, 32.50, 0.125, 11.35, 27.60, 24.12),
    ("coffee_machine", 63.99, 18.50, 0.237, 14.40, 14.90, 12.36),
]


@pytest.mark.parametrize(
    ("name", "price", "cogs", "duty", "fba", "cap_15", "cap_20"), CURRENT
)
def test_ad_space_line_is_lower_than_the_15_percent_line(
    name, price, cogs, duty, fba, cap_15, cap_20
):
    """两条线的上限都必须与 §22 表一致，且 20% 线严格更低。

    差值是谈判的全部余地：净化器 $3.48、咖啡机 $2.54。若某次改动让这个差
    变成 0（例如把 AD_SPACE_FLOOR 误设成 TARGET_MARGIN），这条会红。
    """
    result = adm.evaluate(_cand(name, price, cogs, duty, fba))
    assert result.max_cogs == pytest.approx(cap_15, abs=0.01)
    assert result.max_cogs_ad_space == pytest.approx(cap_20, abs=0.01)
    assert result.max_cogs_ad_space < result.max_cogs
    assert result.max_cogs - result.max_cogs_ad_space == pytest.approx(
        cap_15 - cap_20, abs=0.02
    )


@pytest.mark.parametrize(("name", "price", "cogs", "duty", "fba"), [
    ("air_purifier", 79.99, 32.50, 0.125, 11.35),
    ("coffee_machine", 63.99, 18.50, 0.237, 14.40),
])
def test_ad_space_solution_put_back_gives_exactly_floor(name, price, cogs, duty, fba):
    """代回去必须正好踩在 20%，与 15% 线那条同一套校验。"""
    result = adm.evaluate(_cand(name, price, cogs, duty, fba))
    assert result.max_cogs_ad_space is not None
    at_cap = adm.evaluate(_cand(name, price, result.max_cogs_ad_space, duty, fba))
    assert at_cap.gross_margin == pytest.approx(adm.AD_SPACE_FLOOR, abs=1e-6)


def test_ad_space_floor_is_the_same_number_decision_uses():
    """`admission.AD_SPACE_FLOOR` 与 `decision.MIN_BREAKEVEN_ACOS` 必须同值。

    两处各写一个数，迟早会一个改 20%、一个留 15%——那时命令说「过得去」、
    决策说「NO-GO」，而两边都自称按 `docs/04` 算的。
    """
    from xborder_insight.domain import decision

    assert adm.AD_SPACE_FLOOR == decision.MIN_BREAKEVEN_ACOS


def test_passing_15_percent_without_ad_space_is_flagged():
    """净化器谈到 $25.00（开口价）：过 15% 线，但离 20% 线还差 $0.88。

    这条钉的是「开口价该报多少」这个判断本身——它翻过两次
    （−48.5% → −15.1% → 现在这条 20% 线要求 −25.8%），所以要有测试守着。
    """
    result = adm.evaluate(_cand("air_purifier", 79.99, 25.00, 0.125, 11.35))
    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.1874, abs=1e-4)
    assert result.gross_margin < adm.AD_SPACE_FLOOR
    assert result.max_cogs_ad_space == pytest.approx(24.12, abs=0.01)


def test_purifier_opening_24_clears_both_lines():
    """H22 拍板后的开口价 $24.00：两条线都过，余量只有 $0.12。

    开口价翻过三次（$16.74 → $25.00 → $24.00），每次都是因为上一个锚点
    回答了错的问题。这条把现行锚点钉住：改它必须同时改 `docs/07` H21 的
    让步阶梯，否则文档与命令会说两个价。
    """
    result = adm.evaluate(_cand("air_purifier", 79.99, 24.00, 0.125, 11.35))
    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.2018, abs=1e-4)
    assert result.gross_margin >= adm.AD_SPACE_FLOOR
    assert result.max_cogs_ad_space == pytest.approx(24.12, abs=0.01)
    assert result.max_cogs_ad_space - 24.00 == pytest.approx(0.12, abs=0.01)


def test_signature_line_27_60_is_inside_the_15_percent_line_only():
    """签字线 $27.60 是 15% 线的上限：过 15%、不过 20%。

    这一格是「签了也不投广告」的定义。若哪天有人把 $27.60 当开口价报出去，
    谈成后 ACOS 上限就是 15.00%，比这条测试的断言还低——那时先红的是这条。
    """
    result = adm.evaluate(_cand("air_purifier", 79.99, 27.60, 0.125, 11.35))
    assert result.gross_margin == pytest.approx(0.15, abs=1e-3)
    assert result.gross_margin < adm.AD_SPACE_FLOOR


def test_coffee_box_shrink_moves_it_back_to_standard():
    """咖啡机最短边 9.5 英寸 > 8 → 小号大件 $14.40；收到 8 英寸内 → 大号标准件 $11.85。

    $18.50 的采购价一分不动，毛利 7.90% → 11.88%，两条线从 $14.90/$12.36
    上移到 $16.92/$14.38。这是「不改单价」的杠杆（`docs/04` §23）：
    20% 线的需降幅从 −33.2% 回到 −22.3%，也就是从「出局」回到「议价带内」。

    两个 FBA 都由 `fba-envelope` 给出（§23.5 有命令），这里只钉结论——
    重算会把费率表与准入测试绑死。
    """
    bulky = adm.evaluate(_cand("coffee", 63.99, 18.50, 0.237, 14.40))
    boxed = adm.evaluate(_cand("coffee", 63.99, 18.50, 0.237, 11.85))

    assert bulky.gross_margin == pytest.approx(0.0790, abs=1e-4)
    assert boxed.gross_margin == pytest.approx(0.1188, abs=1e-4)

    assert bulky.max_cogs == pytest.approx(14.90, abs=0.01)
    assert boxed.max_cogs == pytest.approx(16.92, abs=0.01)
    assert bulky.max_cogs_ad_space == pytest.approx(12.36, abs=0.01)
    assert boxed.max_cogs_ad_space == pytest.approx(14.38, abs=0.01)

    # 换段之后 20% 线落回「15–25% 可谈区间」：这个杠杆的全部意义在这一条断言里
    assert (1 - boxed.max_cogs_ad_space / 18.50) < 0.25


def test_purifier_oversized_box_still_passes_15_percent_at_24():
    """箱子超标（FBA $14.00）时，$24.00 仍是 16.86%——能签，只是没广告空间。

    「箱子没守住」推翻的是 $27.60，不是 $24.00。这两件事经常被合成一句
    「价格得重议」，结果把 $1.29 的余地白让出去（签字线 $27.60 → $25.29）。
    同理 $27.60 在超标口径下只有 11.69%，落笔即亏 3.31 个点。
    """
    result = adm.evaluate(_cand("air_purifier", 79.99, 24.00, 0.125, 14.00))

    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.1686, abs=1e-4)
    assert result.gross_margin >= adm.TARGET_MARGIN
    assert result.gross_margin < adm.AD_SPACE_FLOOR

    # 超标不是「整条线作废」，是整条下移约 $2.30
    assert result.max_cogs == pytest.approx(25.30, abs=0.01)
    assert result.max_cogs_ad_space == pytest.approx(21.82, abs=0.01)

    at_signature = adm.evaluate(_cand("air_purifier", 79.99, 27.60, 0.125, 14.00))
    assert at_signature.gross_margin == pytest.approx(0.1169, abs=1e-4)
    assert at_signature.verdict != adm.VERDICT_PASS
