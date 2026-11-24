"""供应商回复 → 下一步动作：七种回复对应七种动作，判定只有这一份。

为什么值得单独一组测试：这是全项目唯一「输入是别人说的话」的模块。
价格、成本、退货率都能被机器核实，**只有供应商的答复只能靠人转述**——
转述错一个字（「能改」说成「改不了」），后面所有数字都还照样算得出来、
照样漂漂亮亮，只是全错。所以这里钉的不是计算，是**七条分岔不许并线**：

| 他回什么 | 动作 | 不许发生 |
| --- | --- | --- |
| 没提箱规 | 先问箱规 | 顺带报个价（报了就要认） |
| 箱规改不了、没说原因 | 问清受限在哪 | 直接降价（纸板与模具方向相反） |
| 能做到、没报价 | 发开口价 | 报到 15% 线上（没有让步带） |
| 报的价已在签字线内 | 直接签 | 再压一轮（把钱谈成关系损耗） |
| 报的价在议价带内 | 还价 | 让到线下（让到线下就是亏） |
| 降幅不大但咬死不放 | 改提售价 | 继续还价（拿不到的价谈三轮还是拿不到） |
| 降幅超出议价带 | 先放 | 报一个 −25% 的价（对方会判断我们不懂行） |

数字全部来自 `docs/04` §33 的咖啡机口径：$63.99 / 关税 23.7% / 头程 $6.00 /
退货率 10%，A 档 FBA $11.85、B 档 $14.40。断言写死在数字上，改口径就会红。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from xborder_insight.domain import admission as adm
from xborder_insight.domain import reply as R

PRICE = 63.99
DUTY = 0.237
FREIGHT = 6.00
RETURNS = 0.10
FBA_A = 11.85
FBA_B = 14.40
FAIR = 20.00


def _coffee(fba: float | None, cogs: float = 18.50, **kw) -> adm.Candidate:
    """咖啡机候选。`cogs` 默认是他最初的报价 $18.50。"""
    return adm.Candidate(
        name="咖啡机",
        selling_price=kw.pop("price", PRICE),
        quoted_cost=cogs,
        duty_rate=DUTY,
        fba_fee=fba,
        inbound_freight=kw.pop("freight", FREIGHT),
        return_rate=kw.pop("returns", RETURNS),
        **kw,
    )


# ---------------------------------------------------------------- ① 先问箱规


def test_box_unanswered_asks_first_and_never_quotes():
    """箱规没答时不许出现任何价格：FBA 档位没定，报价只能是错的那一档。

    话术里同时钉住三件事，缺一句他就会给出一个「看着照做、其实没用」的答复：
    另两边也卡住（只压高度可能把中边放到 15）、8.0 含本数、箱内容积不用缩。
    """
    res = R.resolve(_coffee(None), R.SupplierReply())
    assert res.action == R.ACTION_REASK_BOX
    assert res.needs_external_input
    assert res.sign_price is None and res.open_price is None
    msg = res.next_message
    assert msg
    # ① 不只问最短边：另两边必须同时卡住，否则整单跳回小号大件
    assert "长边不超过 18" in msg and "中边不超过 14" in msg
    # ② 8.0 含本数——平台边界含 8.0，8.1 就掉档
    assert "8 英寸或以下" in msg and "8.0 也算达标" in msg
    # ③ 不说「容积不用缩」，他默认要压缩 15.8%，很可能直接答做不到
    assert "2015" in msg
    # ④ 一个美元数都不许有：带了就收不回来（$15.00 那次的教训）
    assert "$" not in msg


def test_box_unanswered_even_when_he_already_quoted():
    """他一边答非所问一边报价（很常见）：照样先问箱规，不接价。

    「先拿着这个价去算」是这轮最容易犯的错——箱规未定时算出来的毛利，
    FBA 用的是猜的那一档，$2.55/件的差会被静默吃掉。
    """
    res = R.resolve(_coffee(None), R.SupplierReply(counter_price=17.50))
    assert res.action == R.ACTION_REASK_BOX
    assert res.sign_price is None
    assert any("先拿到箱规再回价" in r for r in res.reasons)


# ------------------------------------------------------- ② 问清受限在哪


def test_box_refused_without_reason_asks_the_blocker_first():
    """「改不了」但没说原因，且这一轮没报价 → 只问原因，不判价。"""
    res = R.resolve(_coffee(None), R.SupplierReply(box_fits=False))
    assert res.action == R.ACTION_REASK_BLOCKER
    assert res.branch == "B"
    assert res.sign_price is None
    assert res.next_message == R.BLOCKER_QUESTION


def test_all_blockers_are_valid_and_labelled():
    """纸板、模具、缓冲三类都能往下走，但标签必须分开显示——处置方向不同。

    缓冲这一类是 2026-11-24 11:11 真出现后才加的（`docs/04` §34.8 早就写着
    「出现第三类要加枚举值，不要塞进 tooling」）。
    """
    assert set(R.BLOCKER_LABELS) == {
        R.BLOCKER_PAPERBOARD, R.BLOCKER_TOOLING, R.BLOCKER_CUSHION
    }
    for kind, label in R.BLOCKER_LABELS.items():
        reply = R.SupplierReply(box_fits=False, box_blocker=kind)
        assert reply.blocker_label == label
        res = R.resolve(_coffee(FBA_B), reply, fair_cogs=FAIR)
        assert res.action == R.ACTION_DROP


def test_cushion_blocker_gets_its_own_three_notes():
    """「卡在缓冲」不能按模具处理：换纸板没用，可试的是换材料，而材料要先有机身净尺寸。

    三句话各自改变下一步动作，少一句就会照着别人的说法走——
    最典型的是把「单侧 2 英寸」当成 ISTA 的标准（它只规定跌落高度与次数）。
    """
    res = R.resolve(
        _coffee(FBA_B),
        R.SupplierReply(box_fits=False, box_blocker=R.BLOCKER_CUSHION),
        fair_cogs=FAIR,
    )
    assert res.action == R.ACTION_DROP
    assert res.branch == "B"
    text = "".join(res.reasons)
    for note in R.BLOCKER_CUSHION_NOTES:
        assert note in res.reasons, text
    assert "不规定缓冲厚度" in text
    assert "机身净尺寸" in text
    # 不能落到「问原因」那条路上：他已经说清原因了，再问一遍等于没听
    assert res.action != R.ACTION_REASK_BLOCKER
    assert res.next_message is None


def test_blocker_without_refusal_is_rejected():
    """箱规能做（或未答）却传受限原因 → 两个字段写倒了，直接拒绝。"""
    with pytest.raises(adm.AdmissionInputInvalid, match="两个字段写倒了"):
        R.SupplierReply(box_fits=True, box_blocker=R.BLOCKER_TOOLING)


# ------------------------------------------ ②′ 佣金率是同一句话的第二个变量


def test_the_referral_rate_alone_flips_branch_b_from_drop_to_open():
    """佣金率是敏感性变量，不是待办：H1 已于 11-23 读到 15%，这里钉的是覆盖入口。

    为什么仍要钉：三条路里「佣金」最容易被误当成「还没查」而反复重推一遍。
    实测 8% 下同一句「箱规改不了」会从先放变成开口，签字线 $13.719 → $17.325
    （差 $3.61/件）。钉住它，是为了后台类目真改判、或换 listing 重新归类那天
    一分钟出结论，不是为了现在再去查（查过了，就是 15%）。
    """
    reply = R.SupplierReply(box_fits=False, box_blocker=R.BLOCKER_CUSHION)
    at_15 = R.resolve(_coffee(FBA_B), reply, fair_cogs=FAIR)
    at_8 = R.resolve(_coffee(FBA_B, referral_rate=0.08), reply, fair_cogs=FAIR)

    assert at_15.action == R.ACTION_DROP
    assert at_15.sign_price == 13.70

    assert at_8.action == R.ACTION_OPEN
    assert at_8.sign_ceiling == pytest.approx(17.325, abs=1e-3)
    assert at_8.sign_price == 17.30
    assert at_8.open_price == 16.50
    # 他的 $18.50 在 8% 下已经是 12.67%，离签字线只差 6.4%
    assert at_8.need_drop_pct is None or at_8.need_drop_pct < R.NEGOTIABLE_DROP_PCT


def test_eight_percent_plus_the_cheap_box_signs_at_his_current_price():
    """两个杠杆同时成立时，$18.50 原价就该签——一分不用降。

    单看任何一个都不够：只 8% 佣金是 12.67%，只改箱规是 9.52%，两个一起才是 **16.66%**。
    这条是「两个都不成立时先放」的反面证据：结论对参数敏感，所以 `--referral`
    与 FBA 档位都必须显式可覆盖，不能藏在默认值里。
    """
    res = R.resolve(
        _coffee(FBA_A, referral_rate=0.08),
        R.SupplierReply(box_fits=True, counter_price=18.50),
        fair_cogs=FAIR,
    )
    assert res.action == R.ACTION_ACCEPT
    assert res.reply_margin == pytest.approx(0.1666, abs=5e-4)
    # 反证：同一句话在 15% 佣金下判不了「签」
    at_15 = R.resolve(
        _coffee(FBA_A), R.SupplierReply(box_fits=True, counter_price=18.50), fair_cogs=FAIR
    )
    assert at_15.action != R.ACTION_ACCEPT


# ---------------------------------------------------------------- ③ 发开口价


def test_open_price_leaves_room_below_the_sign_line():
    """A 档、他没报价：开口 $15.00、签字线 $15.70，中间 $0.70 是让步带。"""
    res = R.resolve(_coffee(FBA_A), R.SupplierReply(box_fits=True), fair_cogs=FAIR)
    assert res.action == R.ACTION_OPEN
    assert res.branch == "A"
    assert res.sign_ceiling == pytest.approx(15.732, abs=5e-4)
    assert res.sign_price == pytest.approx(15.70)
    assert res.open_price == pytest.approx(15.00)
    assert res.sign_price - res.open_price == pytest.approx(0.70)
    assert res.sign_margin > adm.TARGET_MARGIN
    assert res.next_message and "$15.00" in res.next_message


def test_branch_is_decided_by_fba_not_by_wording():
    """同样是「能做到」，FBA 给 B 档的数就会算出 B 档的线——档位由费率定，不由措辞定。"""
    a = R.resolve(_coffee(FBA_A), R.SupplierReply(box_fits=True), fair_cogs=FAIR)
    b = R.resolve(_coffee(FBA_B), R.SupplierReply(box_fits=True), fair_cogs=FAIR)
    assert a.sign_price > b.sign_price
    assert a.sign_price - b.sign_price == pytest.approx(2.00)


def test_fba_is_required_once_the_box_is_settled():
    """箱规已定却没给 FBA → 拒绝，而不是拿默认值算一条偏掉的价格线。"""
    with pytest.raises(adm.AdmissionInputInvalid, match="对应档位"):
        R.resolve(_coffee(None), R.SupplierReply(box_fits=True))


# ---------------------------------------------------------------- ④ 直接签


def test_quote_below_sign_line_is_accepted_without_more_haggling():
    """他报 $15.20（16.05%）在签字线内 → 签，且话术里点明含彩盒与首批同价。"""
    res = R.resolve(_coffee(FBA_A), R.SupplierReply(box_fits=True, counter_price=15.20))
    assert res.action == R.ACTION_ACCEPT
    assert res.reply_margin > adm.TARGET_MARGIN
    assert res.next_message and "含单台彩盒" in res.next_message


# ---------------------------------------------------------------- ⑤ 还价


def test_quote_just_above_the_line_gets_a_counter_not_a_concession():
    """他报 $15.80（14.86%）比线高一点点 → 还价到 $15.70，不许再往下让。"""
    res = R.resolve(_coffee(FBA_A), R.SupplierReply(box_fits=True, counter_price=15.80))
    assert res.action == R.ACTION_COUNTER
    assert res.reply_margin == pytest.approx(0.1486, abs=5e-4)
    assert 0 < res.need_drop_pct <= R.NEGOTIABLE_DROP_PCT
    assert res.next_message and "$15.70" in res.next_message


# ------------------------------------------------------------ ⑥ 改提售价


def test_firm_quote_switches_from_haggling_to_repricing():
    """同一条 $15.80，他说「不再让」→ 还价拿不到，只能动售价（$64.99，+1.6%）。

    这是 `firm` 存在的全部理由：数字上两者都「在议价带内」，
    但一个还有的谈、一个已经谈完了，动作必须不同。
    """
    res = R.resolve(
        _coffee(FBA_A),
        R.SupplierReply(box_fits=True, counter_price=15.80, firm=True),
    )
    assert res.action == R.ACTION_REPRICE
    assert res.price_hike_to == pytest.approx(64.99)
    assert res.price_hike_to / PRICE - 1 <= R.PRICE_HIKE_MAX_PCT
    assert res.next_message is None, "内部动作不该带话术：对着供应商说不出口"


def test_firm_without_a_quote_is_rejected():
    """没有报价就谈不上「不让」。"""
    with pytest.raises(adm.AdmissionInputInvalid, match="firm"):
        R.SupplierReply(firm=True)


# ---------------------------------------------------------------- ⑦ 先放


def test_branch_b_at_the_old_quote_is_dropped_not_opened():
    """B 档、他报 $18.50：要降到 $13.719 得让步 25.8% → 先放，且不许报开口价。

    这是 `docs/04` §33.3 那条结论的代码版：B 档不是「压得狠一点」，是压不到。
    注意必须给出受限原因——只给「改不了」时排在最前面的是「问原因」，
    见下一条：那一问比这一轮的价更值钱。
    """
    res = R.resolve(
        _coffee(FBA_B),
        R.SupplierReply(box_fits=False, box_blocker=R.BLOCKER_TOOLING),
        fair_cogs=FAIR,
    )
    assert res.action == R.ACTION_DROP
    assert res.sign_ceiling == pytest.approx(13.719, abs=5e-4)
    assert res.sign_price == pytest.approx(13.70), "$13.72 是四舍五入即掉线的陷阱"
    assert res.need_drop_pct == pytest.approx(0.258, abs=5e-3)
    assert res.next_message is None


def test_asking_why_precedes_judging_the_price():
    """只说「改不了」、没说卡在哪时，即使价格注定谈不成，也先问原因。

    理由不是礼貌，是算术：纸板受限常有替代（换纸板档、改内衬），一旦能进
    A 档，FBA 从 $14.40 降到 $11.85、上限从 $13.719 升到 $15.732——
    这一问值 $2.00/件，而这一轮压价的空间是 $0。所以它的优先级高于判价。
    """
    res = R.resolve(_coffee(FBA_B), R.SupplierReply(box_fits=False), fair_cogs=FAIR)
    assert res.action == R.ACTION_REASK_BLOCKER
    assert res.next_message == R.BLOCKER_QUESTION
    # 但他同时报了价时，价格照样判，只是把这一问挂在结论后面
    res2 = R.resolve(
        _coffee(FBA_B),
        R.SupplierReply(box_fits=False, counter_price=17.50),
        fair_cogs=FAIR,
    )
    assert res2.action == R.ACTION_DROP
    assert res2.pending_question == R.BLOCKER_QUESTION
    assert any("箱规原因仍值得问一句" in r for r in res2.reasons)


def test_out_of_band_price_is_judged_against_this_rounds_quote():
    """降幅按**他这次报价**算，不按 $18.50 算。

    $17.50 到线是 21.6%，$18.50 到线是 25.8%——用历史报价算，会把
    「他刚让了一档」也判成「不开口」，白白丢掉已经到手的让步。
    """
    res = R.resolve(
        _coffee(FBA_B), R.SupplierReply(box_fits=False, counter_price=17.50), fair_cogs=FAIR
    )
    assert res.action == R.ACTION_DROP
    assert res.need_drop_pct == pytest.approx(0.216, abs=5e-3)


# ------------------------------------------------------------ 跨条规则


def test_stress_flip_beats_a_perfectly_good_price():
    """价格全对但压力档转负 → 先放：退货风险吃掉利润时，谈价是在谈一个假数。"""
    res = R.resolve(
        _coffee(FBA_A, returns=0.03),
        R.SupplierReply(box_fits=True),
        fair_cogs=FAIR,
        stress_returns=0.30,
    )
    assert res.action == R.ACTION_DROP
    assert res.stress is not None and not res.stress.worth_negotiating
    assert any("退货风险" in r for r in res.reasons)


def test_upper_bound_refuses_to_produce_a_price_line():
    """缺头程（上界）→ 拒绝，而不是给一条偏高的签字线。

    上界按 0 头程反解，`max_cogs` 偏高；按它报价，就是把「不知道的成本」
    算成「对方该让的价」。`H24` 在 `screen` 上关掉了这条路，这里不能绕回来。
    """
    candidate = _coffee(FBA_A, freight=None)
    assert adm.evaluate(candidate).upper_bound
    with pytest.raises(adm.AdmissionInputInvalid, match="上界口径"):
        R.resolve(candidate, R.SupplierReply(box_fits=True))


def test_every_action_has_a_label_and_the_three_waiting_states_are_marked():
    """七种动作一个都不能漏标签；「等对方」的三种必须能被外部识别为等待。"""
    assert set(R.ACTION_LABELS) == {
        R.ACTION_REASK_BOX,
        R.ACTION_REASK_BLOCKER,
        R.ACTION_OPEN,
        R.ACTION_ACCEPT,
        R.ACTION_COUNTER,
        R.ACTION_REPRICE,
        R.ACTION_DROP,
    }
    waiting = {
        a for a in R.ACTION_LABELS
        if R.Resolution(action=a, reasons=()).needs_external_input
    }
    assert waiting == {R.ACTION_REASK_BOX, R.ACTION_REASK_BLOCKER}


# ------------------------------------------------------------ 入口层（CLI）

DB = Path(__file__).resolve().parent.parent / "scripts" / "db.py"

_BASE = (
    "--name", "咖啡机", "--price", "63.99", "--duty", "0.237",
    "--freight", "6.00", "--fair", "20.00",
)


def _reply(db: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """CLI 要显式指向**隔离库**（`app_db_file` fixture）。

    `--sku` 读的是 `product_returns`，而工作区的 `data/app.sqlite3` 在 .gitignore 里：
    直接用它，本机跑着绿、干净克隆上四条全红——`test_api` 那条守门用例防的就是这个。
    """
    return subprocess.run(
        [sys.executable, str(DB), "--db", str(db), "reply", *_BASE, *argv],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize(
    "argv, code, action",
    [
        (("--sku", "coffee_semi_1613", "--cogs", "18.50"), 3, "先问箱规"),
        (("--sku", "coffee_semi_1613", "--cogs", "18.50", "--box", "no"),
         3, "问清受限在哪"),
        (("--sku", "coffee_semi_1613", "--cogs", "18.50", "--box", "yes",
          "--fba", "11.85"), 0, "发开口价"),
        (("--sku", "coffee_semi_1613", "--cogs", "18.50", "--box", "no",
          "--box-blocker", "tooling", "--fba", "14.40"), 1, "先放"),
    ],
)
def test_cli_exit_code_says_what_to_do(app_db_file, argv, code, action):
    """退出码本身就是接口：0 有动作 / 1 先放 / 3 还在等对方。

    这样「他回了我该做什么」可以直接接进脚本或别的系统，
    不必让人去 grep 中文。
    """
    proc = _reply(app_db_file, *argv)
    assert proc.returncode == code, proc.stdout + proc.stderr[-400:]
    assert f"动作：{action}" in proc.stdout


def test_cli_does_not_print_a_price_before_the_box_is_settled(app_db_file):
    """箱规没答 → 屏幕上一个美元数都不许出现，除了他报的价。"""
    proc = _reply(app_db_file, "--sku", "coffee_semi_1613", "--cogs", "18.50")
    out = proc.stdout
    assert "数字：" not in out
    assert "签字线" not in out
    assert "$18.50" in out  # 只有他报的价，用来确认「我知道你报了，但现在不接」


def test_cli_breaks_the_script_only_at_chinese_punctuation(app_db_file):
    """话术是给人**复制去发**的：断口只能落在中文标点上。

    `textwrap.wrap` 按字符数硬切，会把「（8.0」和「也算达标）」拆成两行；
    贴进微信就是两句半，收件人一眼看出是机器拼的。这里钉的是断口形状，不是宽度。
    """
    proc = _reply(app_db_file, "--sku", "coffee_semi_1613", "--cogs", "18.50")
    script = [
        ln.strip()
        for ln in proc.stdout.splitlines()
        if ln.startswith("    ") and ln.strip()
    ]
    assert len(script) >= 3
    for ln in script:
        assert ln[-1] in "，。；：、！？）】》”,.", f"断行落在标点之外：{ln!r}"
    # 断行之后仍是同一句话：拼回去逐字相同（一个字都没丢、没换）
    assert "".join(script) == R.BOX_QUESTION


def test_cli_refuses_without_a_return_rate(app_db_file):
    """没有退货率就退回，不给默认 8%：上界的签字线偏高，拿去报价必亏。"""
    proc = _reply(app_db_file, "--cogs", "18.50", "--box", "yes", "--fba", "11.85")
    assert proc.returncode == 2
    assert "需要退货率" in proc.stdout


def test_cli_refuses_without_fba_once_the_box_is_settled(app_db_file):
    proc = _reply(app_db_file, "--sku", "coffee_semi_1613", "--cogs", "18.50", "--box", "yes")
    assert proc.returncode == 2
    assert "对应档位" in proc.stdout


def test_cli_prints_the_fifteen_percent_line_to_three_decimals(app_db_file):
    """$13.719 必须印三位小数：印成 $13.72 就等于把「四舍五入即掉线」的数报出去。"""
    proc = _reply(
        app_db_file, "--sku", "coffee_semi_1613", "--cogs", "18.50",
        "--box", "no", "--box-blocker", "tooling", "--fba", "14.40",
    )
    assert "$13.719" in proc.stdout
    assert "签字线 $13.70" in proc.stdout


def test_cli_prints_the_referral_rate_and_lets_it_be_overridden(app_db_file):
    """佣金率一直有默认值——所以它必须每行都印出来，连「默认值、H1 已读到」也印。

    它是唯一一个「不填也有数、但填错就少报 $3.6/件」的参数（$13.70 对 $17.30）。
    这一行不印，读的人不会想到它可覆盖；而覆盖入口一旦失效，后台改判那天就会
    按旧口径签字。
    """
    default = _reply(
        app_db_file, "--sku", "coffee_semi_1613", "--cogs", "18.50",
        "--box", "no", "--box-blocker", "cushion", "--fba", "14.40",
    )
    assert default.returncode == 1
    assert "佣金：15%（默认值，H1 11-23 已读到 15%）" in default.stdout
    assert "签字线 $13.70" in default.stdout

    eight = _reply(
        app_db_file, "--sku", "coffee_semi_1613", "--cogs", "18.50",
        "--box", "no", "--box-blocker", "cushion", "--fba", "14.40",
        "--referral", "0.08",
    )
    assert eight.returncode == 0
    assert "佣金：8%（--referral 指定）" in eight.stdout
    assert "$17.325" in eight.stdout and "签字线 $17.30" in eight.stdout
