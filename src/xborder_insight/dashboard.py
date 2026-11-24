"""单页看板：把分析结果渲染成一屏可读的机会清单。

刻意不引入前端框架：这个项目要证明的是后端的抽取可靠性与评测能力，
前端只需清晰呈现结果，多一寸框架都是干扰项。
"""

from __future__ import annotations

from html import escape

from .schemas import AnalysisReport

STYLE = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f6f6f4;color:#1c1c1a;line-height:1.6;padding:32px 20px}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:20px;font-weight:500;letter-spacing:.2px}
.sub{color:#6b6b65;font-size:13px;margin-top:6px}
.nav{display:flex;gap:18px;margin:16px 0 0;border-bottom:1px solid #e6e6e0}
.navlink{font-size:13px;color:#8a8a82;text-decoration:none;padding:0 0 7px;
border-bottom:2px solid transparent;margin-bottom:-1px}
.navlink.on{color:#1c1c1a;border-bottom-color:#1c1c1a}
.tabs{display:flex;gap:8px;margin:22px 0 18px;flex-wrap:wrap}
.tab{padding:6px 14px;border:1px solid #dcdcd6;border-radius:999px;font-size:13px;
color:#4a4a45;text-decoration:none;background:#fff}
.tab.on{background:#1c1c1a;color:#fff;border-color:#1c1c1a}
.stats{display:flex;gap:28px;padding:16px 20px;background:#fff;
border:1px solid #e6e6e0;border-radius:12px;margin-bottom:22px;flex-wrap:wrap}
.stat b{display:block;font-size:18px;font-weight:500}
.stat span{font-size:12px;color:#8a8a82}
.card{background:#fff;border:1px solid #e6e6e0;border-radius:12px;padding:18px 20px;margin-bottom:12px}
.head{display:flex;align-items:baseline;gap:10px}
.rank{font-size:12px;color:#a0a098;font-variant-numeric:tabular-nums}
.name{font-size:15px;font-weight:500}
.score{margin-left:auto;font-size:13px;color:#185fa5;font-variant-numeric:tabular-nums}
.bar{height:3px;background:#eeece6;border-radius:2px;margin:10px 0 12px;overflow:hidden}
.bar i{display:block;height:100%;background:#185fa5}
.meta{display:flex;gap:18px;font-size:12px;color:#6b6b65;margin-bottom:10px}
.meta em{font-style:normal;color:#1c1c1a}
blockquote{font-size:13px;color:#4a4a45;padding:6px 0 6px 12px;
border-left:2px solid #e0e0da;margin-top:4px}
.foot{margin-top:22px;font-size:12px;color:#8a8a82}
"""


def _card(idx: int, theme) -> str:
    width = min(100.0, theme.opportunity_score * 100)
    evidence = "".join(
        f"<blockquote>{escape(text)}</blockquote>" for text in theme.evidence
    )
    return f"""
<div class="card">
  <div class="head">
    <span class="rank">{idx:02d}</span>
    <span class="name">{escape(theme.display_name)}</span>
    <span class="score">{theme.opportunity_score:.3f}</span>
  </div>
  <div class="bar"><i style="width:{width:.1f}%"></i></div>
  <div class="meta">
    <span>提及 <em>{theme.mention_count}</em> 条</span>
    <span>负向占比 <em>{theme.negative_ratio:.0%}</em></span>
    <span>证据强度 <em>{theme.severity:.2f}</em></span>
    <span>细分场景 <em>{theme.scene_count}</em></span>
  </div>
  {evidence}
</div>"""


def render(report: AnalysisReport, datasets: list[str], current: str) -> str:
    tabs = "".join(
        f'<a class="tab{" on" if name == current else ""}" href="/?dataset={escape(name)}">'
        f"{escape(name)}</a>"
        for name in datasets
    )
    cards = "".join(_card(i, t) for i, t in enumerate(report.themes, start=1))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>跨境选品决策引擎</title><style>{STYLE}</style></head>
<body><div class="wrap">
  <h1>跨境选品决策引擎</h1>
  <div class="sub">竞品评论 → 结构化抽取 → 机会分排序。机会分 = 0.55×需求广度 + 0.35×负向占比 + 0.10×证据强度</div>
  <div class="nav">
    <a class="navlink on" href="/">机会清单</a>
    <a class="navlink" href="/console">决策台</a>
  </div>
  <div class="tabs">{tabs}</div>
  <div class="stats">
    <div class="stat"><b>{report.review_count}</b><span>评论样本</span></div>
    <div class="stat"><b>{report.parse_success_rate:.0%}</b><span>契约解析成功率</span></div>
    <div class="stat"><b>{len(report.themes)}</b><span>机会主题</span></div>
    <div class="stat"><b>{report.elapsed_ms} ms</b><span>端到端耗时</span></div>
    <div class="stat"><b>{report.llm_calls}</b><span>模型调用次数</span></div>
  </div>
  {cards}
  <div class="foot">数据为人工构造的合成样本，不来自任何平台抓取。指标可复现：python scripts/run_eval.py</div>
</div></body></html>"""
