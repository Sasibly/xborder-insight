"""市场典型规格（assumed 档）：假设不能冒充事实，分歧不能取均值。

这组用例守的是四件事，每一件都对应一种「看起来有结论、其实没有」的错法：

1. **把 assumed 当 verified**。画像文件写 `status: "assumed"` 才允许读；
   改成 `verified` 直接报错——这批数是品类推断单品的假设，不是任何一个
   真实 SKU 的实测值，一旦被下游当成事实，就会变成「用假数把区间变窄」。
2. **裕度不足却给结论**。18×14×8 的 5% 是 0.9 / 0.7 / 0.4 英寸，比纸箱公差
   和卷尺读数误差都小。落在这个带宽里的判定换把尺子就翻转，所以必须标 tight。
3. **两台机型结论相反还取平均**。净化器一台超 27.5%、一台余 9.0%，
   平均出一个「差不多」是纯粹的虚构——正确输出是 DIVERGENT + 必须实测。
4. **与 `package` 层口径不一致**。两处都要按长度降序比对三边，
   否则会出现「这边判超、那边判过」的自相矛盾。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xborder_insight.domain import package as pkg
from xborder_insight.domain import profiles as prof
from xborder_insight.errors import InputInvalid, ParamUnverified


# ---------------------------------------------------------------- 画像本身


def test_default_profile_covers_the_six_categories():
    """六个品类各两台机型，缺一台就没有第二个样本来检假设稳不稳。"""
    reports = prof.load_reports()
    keys = [r.spec.key for r in reports]
    assert keys == [
        "air_purifier",
        "coffee_machine",
        "electric_kettle",
        "pet_fountain",
        "robot_vacuum",
        "wireless_charger",
    ]
    for report in reports:
        assert len(report.variants) >= 2, f"{report.spec.key} 只有一台机型，无法检验假设稳定性"
        for variant in report.variants:
            assert variant.spec.source_url, f"{variant.spec.model} 缺来源链接"


def test_profile_must_declare_assumed(tmp_path: Path):
    """把 status 改成 verified 必须被拒：这批数不是实测值。"""
    payload = json.loads(prof.DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    payload["status"] = "verified"
    target = tmp_path / "bad.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "assumed" in exc.value.message


def test_missing_file_is_reported_not_silently_empty(tmp_path: Path):
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(tmp_path / "nope.json")
    assert "不存在" in exc.value.message


def _write(tmp_path: Path, categories, *, status: str = "assumed", **extra) -> Path:
    payload = {"status": status, "categories": categories, **extra}
    target = tmp_path / "profile.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _variant(**overrides):
    base = {
        "model": "x",
        "dims_in": [10, 10, 5],
        "weight_oz": 20,
        "has_lithium_battery": False,
    }
    base.update(overrides)
    return base


def test_battery_flag_cannot_be_a_string(tmp_path: Path):
    """含锂这一列没有第三态：'unknown' 不是合法值。"""
    target = _write(
        tmp_path,
        [{"key": "k", "display_name": "k", "variants": [_variant(has_lithium_battery="unknown")]}],
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "布尔值" in exc.value.message


def test_dims_must_be_three_numbers(tmp_path: Path):
    target = _write(
        tmp_path,
        [{"key": "k", "display_name": "k", "variants": [_variant(dims_in=[10, 5])]}],
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "三个数值" in exc.value.message


def test_duplicate_category_key_is_rejected(tmp_path: Path):
    target = _write(
        tmp_path,
        [
            {"key": "k", "display_name": "a", "variants": [_variant()]},
            {"key": "k", "display_name": "b", "variants": [_variant()]},
        ],
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "重复" in exc.value.message


# ---------------------------------------------------------------- 裕度


def test_edge_margins_sort_before_comparing():
    """18×8×14 必须按 18/14/8 与上限 18/14/8 比，不能按书写顺序比。

    不排序的话会把 8 拿去和上限 14 比、14 拿去和上限 8 比，判成「超限」——
    方向正好反过来（本该通过）。
    """
    edges = prof.edge_margins((18, 8, 14))
    assert [e.value for e in edges] == [18, 14, 8]
    assert [e.limit for e in edges] == [18, 14, 8]
    assert all(e.ratio == 0 for e in edges)


def test_edge_margin_sign_means_exceeded():
    """正数=有余量，负数=超出。方向不能反，反了会把超限的安全余量读成超标。"""
    edges = prof.edge_margins((10.2, 10.2, 15.4))
    worst = min(edges, key=lambda e: e.ratio)
    assert worst.value == 10.2
    assert worst.limit == 8
    assert worst.ratio < 0
    assert worst.exceeded


def test_tight_band_is_five_percent():
    """差 4% 算紧，差 6% 不算。阈值写死是为了让「临界」这个词有确定含义。"""
    assert prof.edge_margins((17.5, 10, 5))[0].tight  # 18 的 2.8%
    assert not prof.edge_margins((16.5, 10, 5))[0].tight  # 18 的 8.3%


@pytest.mark.parametrize(
    "dims",
    [
        (18, 14, 8),
        (18.01, 14, 8),
        (10.2, 10.2, 15.4),
        (7.28, 14.56, 11.22),
        (9.45, 8.27, 9.45),
        (13.39, 5.51, 14.92),
    ],
)
def test_size_verdict_agrees_with_package_layer(dims):
    """两层的「装不装得进」必须同口径，否则同一台货会得到两个答案。"""
    fits_here = prof.edge_margins(dims)[-1].ratio >= 0 and all(
        e.ratio >= 0 for e in prof.edge_margins(dims)
    )
    fits_there = pkg.fits_standard(dims)
    assert fits_here is fits_there


# ---------------------------------------------------------------- 聚合判定


def _report(tmp_path: Path, variants) -> prof.CategoryReport:
    spec = prof.load_profile(
        _write(tmp_path, [{"key": "k", "display_name": "k", "variants": variants}])
    ).categories[0]
    return prof.build_category(spec)


def test_divergent_when_two_machines_disagree(tmp_path: Path):
    """一台装得进、一台装不进 → 必须判 DIVERGENT，不能取平均。"""
    report = _report(
        tmp_path,
        [
            _variant(model="fits", dims_in=[10, 10, 5], weight_oz=20),
            _variant(model="exceeds", dims_in=[16, 12, 10], weight_oz=20),
        ],
    )
    assert report.size_divergent
    assert report.verdict == prof.VERDICT_DIVERGENT
    assert "量箱子" in report.headline()


def test_all_exceed_clear_gives_hard_no_quote(tmp_path: Path):
    report = _report(
        tmp_path,
        [
            _variant(model="a", dims_in=[20, 15, 12], weight_oz=20),
            _variant(model="b", dims_in=[16, 15, 12], weight_oz=20),
        ],
    )
    assert report.verdict == prof.VERDICT_NOT_STANDARD


def test_all_exceed_but_one_is_tight_is_not_a_hard_no(tmp_path: Path):
    """一台只超 1.2% 时不能给死结论：换同系列另一款就可能翻进来。"""
    report = _report(
        tmp_path,
        [
            _variant(model="barely", dims_in=[14.17, 14.17, 7.87], weight_oz=140),
            _variant(model="clearly", dims_in=[12.8, 12.4, 9.8], weight_oz=140),
        ],
    )
    assert report.verdict == prof.VERDICT_NOT_STANDARD_TIGHT
    assert "只超" in report.headline()


def test_fits_but_tight_is_quoted_with_a_warning(tmp_path: Path):
    report = _report(
        tmp_path,
        [_variant(model="a", dims_in=[13.39, 5.51, 14.92], weight_oz=91)],
    )
    assert report.verdict == prof.VERDICT_QUOTE_TIGHT
    assert "量一次" in report.headline()


def test_battery_divergence_blocks_category_level_fill(tmp_path: Path):
    """同品类既有无电池款又有含锂款时，product_class 不能按品类统一。"""
    report = _report(
        tmp_path,
        [
            _variant(model="corded", dims_in=[10, 10, 5], has_lithium_battery=False),
            _variant(model="wireless", dims_in=[10, 10, 5], has_lithium_battery=True),
        ],
    )
    assert report.battery_divergent
    assert report.verdict == prof.VERDICT_QUOTE
    assert "逐 SKU 确认" in report.headline()
    classes = {v.product_class for v in report.variants}
    assert classes == {"non_apparel", "dangerous_goods"}


# ---------------------------------------------------------------- 与包装层衔接


def test_exceeding_variant_routes_to_bulky_not_standard_price(tmp_path: Path):
    """超限机型走 package.classify 必须落在小号大件，而不是被当成标准件算便宜价。"""
    report = _report(tmp_path, [_variant(model="a", dims_in=[15.4, 10.2, 10.2])])
    result = pkg.classify(report.variants[0].as_facts())
    assert result.size_tiers == ("small_bulky",)
    assert report.bulky_quotable is True


def test_billable_weight_takes_the_larger_of_unit_and_dim_weight(tmp_path: Path):
    """18×14×8 级别的箱子，计费重量由体积重决定，不是实重。"""
    report = _report(tmp_path, [_variant(model="a", dims_in=[18, 14, 8], weight_oz=32)])
    variant = report.variants[0]
    assert variant.spec.weight_oz == 32
    assert variant.dim_weight_oz == pytest.approx(pkg.dimensional_weight_oz((18, 14, 8)))
    assert variant.billable_oz == pytest.approx(variant.dim_weight_oz)
    assert variant.billable_oz > 200  # 14.5 lb ≈ 232 oz


# ---------------------------------------------------------------- 实测档（measured）


def test_measured_profile_requires_evidence(tmp_path: Path):
    """「实测」两个字本身不构成证据：必须写清谁、怎么量的。

    没有这一栏，一条实测记录和一条猜测在文件里长得完全一样，
    而下游只会看见 status=measured 就当成事实用。
    """
    target = _write(
        tmp_path,
        [{"key": "k", "variants": [_variant(measured_at="2026-11-21", battery_confirmed=False)]}],
        status="measured",
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "evidence" in exc.value.message


def test_measured_variant_requires_measured_at(tmp_path: Path):
    target = _write(
        tmp_path,
        [{"key": "k", "variants": [_variant(battery_confirmed=False)]}],
        status="measured",
        evidence="用户口述",
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "measured_at" in exc.value.message


def test_measured_variant_must_state_battery_confirmed_explicitly(tmp_path: Path):
    """留空会被当成「已确认不含锂」——那是把未知当成通过，必须拒绝。"""
    target = _write(
        tmp_path,
        [{"key": "k", "variants": [_variant(measured_at="2026-11-21")]}],
        status="measured",
        evidence="用户口述",
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "battery_confirmed" in exc.value.message


def test_assumed_profile_leaves_battery_confirmed_true(tmp_path: Path):
    """假设档不需要这个标记：它本来就没打算当事实，否则每行都要多一列噪音。"""
    profile = prof.load_profile(_write(tmp_path, [{"key": "k", "variants": [_variant()]}]))
    assert profile.status == prof.PROFILE_ASSUMED
    assert profile.categories[0].variants[0].battery_confirmed is True
    assert profile.exact_weight is False


def test_measured_profile_collapses_to_single_point(tmp_path: Path):
    """实测档的实重是量出来的，不再表示「不确定会不会更轻」→ 不再从 1 lb 起算。"""
    profile = prof.load_profile(
        _write(
            tmp_path,
            [{"key": "k", "variants": [_variant(
                measured_at="2026-11-21", battery_confirmed=False,
            )]}],
            status="measured",
            evidence="用户口述",
        )
    )
    assert profile.measured is True
    assert profile.exact_weight is True
    assert [v.model for v in profile.unconfirmed_battery] == ["x"]
    assert "实测" in profile.label


def test_repo_measured_profile_matches_what_was_measured():
    """仓库里那份实测画像必须与 11-21 收到的四台一致，且口径是「包装后」。

    机器人按保守侧取含锂：同品类两台在售机型都是 2600 mAh Li-ion，
    带基站机型不存在无电池款。取 false 会把每件成本少算 $1.28。
    """
    profile = prof.load_profile(prof.MEASURED_PROFILE_PATH)
    assert profile.measured
    assert [c.key for c in profile.categories] == [
        "air_purifier",
        "coffee_machine",
        "electric_kettle",
        "robot_vacuum",
    ]
    dims = {
        c.key: c.variants[0].dims_in for c in profile.categories
    }
    assert dims["air_purifier"] == (24.0, 15.0, 15.0)
    assert dims["coffee_machine"] == (16.0, 13.0, 9.5)
    assert dims["electric_kettle"] == (10.5, 9.2, 8.4)
    assert dims["robot_vacuum"] == (22.0, 18.0, 14.0)

    vacuum = next(c for c in profile.categories if c.key == "robot_vacuum")
    assert vacuum.variants[0].has_lithium_battery is True
    # 全部机型都还没核过 BOM，一台都不许标成已确认
    assert len(profile.unconfirmed_battery) == 4

    reports = prof.load_reports(prof.MEASURED_PROFILE_PATH)
    assert all(r.bulky_quotable for r in reports), "四台都应收敛到小号大件，不该报缺"


# ---------------------------------------------------------------- 实测档（measured）


def test_measured_profile_requires_evidence(tmp_path: Path):
    """「实测」两个字本身不构成证据：必须写清谁、怎么量的。

    没有这一栏，一条实测记录和一条猜测在文件里长得完全一样，
    而下游只会看见 status=measured 就当成事实用。
    """
    target = _write(
        tmp_path,
        [{"key": "k", "variants": [_variant(measured_at="2026-11-21", battery_confirmed=False)]}],
        status="measured",
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "evidence" in exc.value.message


def test_measured_variant_requires_measured_at(tmp_path: Path):
    target = _write(
        tmp_path,
        [{"key": "k", "variants": [_variant(battery_confirmed=False)]}],
        status="measured",
        evidence="用户口述",
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "measured_at" in exc.value.message


def test_measured_variant_must_state_battery_confirmed_explicitly(tmp_path: Path):
    """留空会被当成「已确认不含锂」——那是把未知当成通过，必须拒绝。"""
    target = _write(
        tmp_path,
        [{"key": "k", "variants": [_variant(measured_at="2026-11-21")]}],
        status="measured",
        evidence="用户口述",
    )
    with pytest.raises(InputInvalid) as exc:
        prof.load_profile(target)
    assert "battery_confirmed" in exc.value.message


def test_assumed_profile_leaves_battery_confirmed_true(tmp_path: Path):
    """假设档不需要这个标记：它本来就没打算当事实，否则每行都要多一列噪音。"""
    profile = prof.load_profile(_write(tmp_path, [{"key": "k", "variants": [_variant()]}]))
    assert profile.status == prof.PROFILE_ASSUMED
    assert profile.categories[0].variants[0].battery_confirmed is True
    assert profile.exact_weight is False


def test_measured_profile_collapses_to_single_point(tmp_path: Path):
    """实测档的实重是量出来的，不再表示「不确定会不会更轻」→ 不再从 1 lb 起算。"""
    profile = prof.load_profile(
        _write(
            tmp_path,
            [{"key": "k", "variants": [_variant(
                measured_at="2026-11-21", battery_confirmed=False,
            )]}],
            status="measured",
            evidence="用户口述",
        )
    )
    assert profile.measured is True
    assert profile.exact_weight is True
    assert [v.model for v in profile.unconfirmed_battery] == ["x"]
    assert "实测" in profile.label


def test_repo_measured_profile_matches_what_was_measured():
    """仓库里那份实测画像必须与 11-21 收到的四台一致，且口径是「包装后」。

    机器人按保守侧取含锂：同品类两台在售机型都是 2600 mAh Li-ion，
    带基站机型不存在无电池款。取 false 会把每件成本少算 $1.28。
    """
    profile = prof.load_profile(prof.MEASURED_PROFILE_PATH)
    assert profile.measured
    assert [c.key for c in profile.categories] == [
        "air_purifier",
        "coffee_machine",
        "electric_kettle",
        "robot_vacuum",
    ]
    dims = {
        c.key: c.variants[0].dims_in for c in profile.categories
    }
    assert dims["air_purifier"] == (24.0, 15.0, 15.0)
    assert dims["coffee_machine"] == (16.0, 13.0, 9.5)
    assert dims["electric_kettle"] == (10.5, 9.2, 8.4)
    assert dims["robot_vacuum"] == (22.0, 18.0, 14.0)

    vacuum = next(c for c in profile.categories if c.key == "robot_vacuum")
    assert vacuum.variants[0].has_lithium_battery is True
    # 全部机型都还没核过 BOM，一台都不许标成已确认
    assert len(profile.unconfirmed_battery) == 4

    reports = prof.load_reports(prof.MEASURED_PROFILE_PATH)
    assert all(r.bulky_quotable for r in reports), "四台都应收敛到小号大件，不该报缺"
