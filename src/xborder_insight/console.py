"""决策台：把 `/v1/decision` 包成一张能填参数、能立刻看到结论的表单页。

为什么要有这一页：`/` 是机会清单看板，只读——它回答「哪些品值得看」，不回答
「这一台按我手上的成本到底做不做」。后者的参数（报价、售价、佣金、FBA、头程、
关税、退货率）每天都在变，让人去 `/docs` 手拼 JSON 等于没有工具。

同样不引入前端框架：校验、判定、口径全在服务端，这一页只负责传参与展示。
页面上每个字段都对应 `CostProfileInput` / `DecisionRequest` 里的一个真实字段名，
`tests/test_console.py` 拿 `/openapi.json` 校对，防止改了 schema 忘改页面。
"""

from __future__ import annotations

STYLE = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f6f6f4;color:#1c1c1a;line-height:1.6;padding:28px 20px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:20px;font-weight:500;letter-spacing:.2px}
.sub{color:#6b6b65;font-size:13px;margin-top:6px}
.nav{display:flex;gap:8px;margin:18px 0 20px}
.nav a{padding:6px 14px;border:1px solid #dcdcd6;border-radius:999px;font-size:13px;
color:#4a4a45;text-decoration:none;background:#fff}
.nav a.on{background:#1c1c1a;color:#fff;border-color:#1c1c1a}
.cols{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.form{flex:0 0 360px;min-width:320px}
.out{flex:1 1 460px;min-width:320px}
.card{background:#fff;border:1px solid #e6e6e0;border-radius:12px;padding:16px 18px;margin-bottom:12px}
.card h2{font-size:13px;font-weight:500;color:#6b6b65;margin-bottom:12px;letter-spacing:.3px}
label{display:block;font-size:12px;color:#6b6b65;margin:10px 0 4px}
label:first-of-type{margin-top:0}
input,select{width:100%;padding:7px 9px;border:1px solid #dcdcd6;border-radius:8px;
font-size:13px;color:#1c1c1a;background:#fff;font-family:inherit}
input:focus,select:focus{outline:none;border-color:#185fa5;box-shadow:0 0 0 3px #185fa51a}
button{width:100%;margin-top:16px;padding:10px;border:0;border-radius:9px;background:#1c1c1a;
color:#fff;font-size:14px;font-family:inherit;cursor:pointer}
button:hover{background:#333}
button:disabled{background:#a0a098;cursor:default}
.hint{font-size:13px;color:#8a8a82}
.verdict{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}
.verdict b{font-size:22px;font-weight:500;letter-spacing:.5px}
.verdict span{font-size:13px;color:#6b6b65}
.v-go b{color:#185fa5}.v-no b{color:#a3341f}.v-more b{color:#8a6d1f}
.warn{background:#fdf6e3;border:1px solid #ecdcb0;color:#6b5316;font-size:12px;
padding:10px 12px;border-radius:9px;margin-bottom:12px}
.kv{display:flex;flex-wrap:wrap;gap:10px 22px;margin-bottom:4px}
.kv div{font-size:12px;color:#6b6b65}
.kv em{font-style:normal;display:block;font-size:16px;color:#1c1c1a;
font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid #f0f0ea;vertical-align:top}
th{color:#8a8a82;font-weight:400}
td.n{font-variant-numeric:tabular-nums}
.pass{color:#185fa5}.fail{color:#a3341f}.unk{color:#8a6d1f}
.foot{margin-top:18px;font-size:12px;color:#8a8a82}
"""

BODY = """
<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>决策台 · 跨境选品决策引擎</title><style>__STYLE__</style></head>
<body><div class="wrap">
  <h1>跨境选品决策引擎</h1>
  <div class="sub">填成本档案 → 一次调用判定做不做。所有数由你填，系统不替你假设；缺项会让结论退回「上界」。</div>
  <div class="nav"><a href="/">机会清单</a><a class="on" href="/console">决策台</a></div>

  <div class="cols">
    <form class="form" id="f" autocomplete="off">
      <div class="card">
        <h2>商品</h2>
        <label for="category">品类（查合规规则表用）</label>
        <select id="category" name="category">
          <option value="air_purifier" selected>air_purifier 空气净化器</option>
          <option value="coffee_machine">coffee_machine 半自动咖啡机</option>
          <option value="electric_kettle">electric_kettle 电热水壶</option>
          <option value="pet_fountain">pet_fountain 宠物饮水机</option>
          <option value="robot_vacuum">robot_vacuum 扫地机器人</option>
          <option value="wireless_charger">wireless_charger 无线充电器</option>
        </select>
        <label for="asin">ASIN（只做关联，不参与计算）</label>
        <input id="asin" name="asin" value="B0PURIFIER1">
        <label for="opportunity_score">机会分（0–1，低于 0.55 判需求不足）</label>
        <input id="opportunity_score" name="opportunity_score" type="number" step="0.01" min="0" max="1" value="0.72">
      </div>

      <div class="card">
        <h2>成本档案（USD / 比率填小数）</h2>
        <label for="selling_price">售价 selling_price *</label>
        <input id="selling_price" name="selling_price" type="number" step="0.01" value="79.99">
        <label for="quoted_price">供应商报价 quoted_price *</label>
        <input id="quoted_price" name="quoted_price" type="number" step="0.01" value="24.30">
        <label for="referral_rate">佣金率 referral_rate *（0.15 = 15%）</label>
        <input id="referral_rate" name="referral_rate" type="number" step="0.001" value="0.15">
        <label for="fba_fee">FBA 配送费 fba_fee</label>
        <input id="fba_fee" name="fba_fee" type="number" step="0.01" value="11.35">
        <label for="inbound_freight">头程单件 inbound_freight</label>
        <input id="inbound_freight" name="inbound_freight" type="number" step="0.01" value="6.00">
        <label for="duty_rate">关税率 duty_rate</label>
        <input id="duty_rate" name="duty_rate" type="number" step="0.001" value="0.125">
        <label for="incoterm">贸易术语 incoterm</label>
        <select id="incoterm" name="incoterm">
          <option value="FOB" selected>FOB（头程与关税另计）</option>
          <option value="EXW">EXW</option>
          <option value="CFR">CFR（含头程）</option>
          <option value="CIF">CIF</option>
          <option value="DDP">DDP（含头程与关税）</option>
        </select>
      </div>

      <div class="card">
        <h2>退货率</h2>
        <label for="sku_key">从库里的两档退货率取（与手填互斥）</label>
        <select id="sku_key" name="sku_key">
          <option value="purifier_desk_1752" selected>purifier_desk_1752 净化器 12.6% → 17%</option>
          <option value="coffee_semi_1613">coffee_semi_1613 咖啡机 15% → 20%</option>
          <option value="">不指定，改用手填</option>
        </select>
        <label for="return_rate">手填退货率 return_rate（选了上面就忽略）</label>
        <input id="return_rate" name="return_rate" type="number" step="0.001" placeholder="留空 = 未指定（结论降级为上界）">
      </div>

      <button type="submit" id="go">算一下</button>
    </form>

    <div class="out" id="out">
      <div class="card"><div class="hint">左侧参数已按净化器现行口径预填（$79.99 / $24.30 / 15% / $11.35 / $6.00 / 12.5%）。
点「算一下」出结论；改任意一项再算，结论会跟着变。</div></div>
    </div>
  </div>

  <div class="foot">接口：<code>POST /v1/decision</code>；契约见 <a href="/docs">/docs</a>。
本页不存任何数据，参数只发往本机服务。</div>
</div>
<script>
(function () {
  var F = document.getElementById('f');
  var OUT = document.getElementById('out');
  var GO = document.getElementById('go');

  function num(name) {
    var el = F.elements[name];
    if (!el) return undefined;
    var raw = String(el.value).trim();
    if (raw === '') return undefined;
    var v = Number(raw);
    return isNaN(v) ? undefined : v;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function money(v) {
    if (v === undefined || v === null) return '—';
    return '$' + Number(v).toFixed(2);
  }

  function pct(v) {
    if (v === undefined || v === null) return '—';
    return (Number(v) * 100).toFixed(2) + '%';
  }

  function card(title) {
    var c = el('div', 'card');
    if (title) c.appendChild(el('h2', null, title));
    return c;
  }

  function render(data) {
    OUT.textContent = '';

    var v = String(data.verdict || '');
    var cls = v === 'GO' ? 'v-go' : (v === 'NO_GO' ? 'v-no' : 'v-more');
    var head = card();
    var line = el('div', 'verdict ' + cls);
    line.appendChild(el('b', null, v));
    line.appendChild(el('span', null, data.headline || ''));
    head.appendChild(line);
    OUT.appendChild(head);

    var p = data.profit || {};
    var miss = p.missing_inputs || [];
    if (miss.length) {
      var w = el('div', 'warn');
      w.textContent = '这是上界，不是结论：' + miss.join('、') +
        ' 未填，按 0 计入。这几项只要不为 0，毛利率就一定低于 ' + pct(p.gross_margin) +
        '。补齐后才算判定。';
      OUT.appendChild(w);
    }

    var box = card('利润分解');
    var kv = el('div', 'kv');
    [['毛利', money(p.gross_profit)], ['毛利率', pct(p.gross_margin)],
     ['落地成本', money(p.landed_cost)], ['佣金', money(p.referral_fee)],
     ['FBA', money(p.fba_fee)], ['关税', money(p.duty)],
     ['头程', money(p.freight)], ['退货成本', (p.returns && money(p.returns.total)) || '—']
    ].forEach(function (pair) {
      var d = el('div');
      d.appendChild(el('em', null, pair[1]));
      d.appendChild(document.createTextNode(pair[0]));
      kv.appendChild(d);
    });
    box.appendChild(kv);
    OUT.appendChild(box);

    var rs = data.returns && data.returns.stress;
    if (rs) {
      var s = card('退货压力档');
      var kv2 = el('div', 'kv');
      [['基线退货率', pct(rs.baseline_returns)], ['压力退货率', pct(rs.stress_returns)],
       ['基线毛利', pct(rs.baseline_margin)], ['压力毛利', pct(rs.stress_margin)],
       ['判定', rs.label || '—']
      ].forEach(function (pair) {
        var d = el('div');
        d.appendChild(el('em', null, pair[1]));
        d.appendChild(document.createTextNode(pair[0]));
        kv2.appendChild(d);
      });
      s.appendChild(kv2);
      if (data.returns.missing_evidence && data.returns.missing_evidence.length) {
        s.appendChild(el('div', 'hint', '证据还缺：' + data.returns.missing_evidence.join('；')));
      }
      OUT.appendChild(s);
    }

    var reasons = data.reasons || [];
    if (reasons.length) {
      var rc = card('判定理由');
      var t = el('table');
      var thead = el('tr');
      ['因子', '实测', '结果', '说明'].forEach(function (h) { thead.appendChild(el('th', null, h)); });
      t.appendChild(thead);
      reasons.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, r.factor));
        tr.appendChild(el('td', 'n', r.observed));
        var res = String(r.result || '');
        var rcls = res === 'pass' ? 'pass' : (res === 'fail' ? 'fail' : 'unk');
        tr.appendChild(el('td', rcls, res));
        tr.appendChild(el('td', null, r.detail));
        t.appendChild(tr);
      });
      rc.appendChild(t);
      OUT.appendChild(rc);
    }

    var prov = card('数据来源');
    prov.appendChild(el('div', 'hint',
      'data_provenance = ' + (data.data_provenance || '—') +
      '。这些成本是你填的，不是系统核实的；换一个口径结论就会变。'));
    OUT.appendChild(prov);
  }

  F.addEventListener('submit', function (e) {
    e.preventDefault();
    var cost = {};
    ['selling_price', 'quoted_price', 'referral_rate'].forEach(function (k) {
      var v = num(k);
      if (v !== undefined) cost[k] = v;
    });
    ['fba_fee', 'inbound_freight', 'duty_rate'].forEach(function (k) {
      var v = num(k);
      if (v !== undefined) cost[k] = v;
    });
    var incoterm = F.elements.incoterm.value;
    if (incoterm) cost.incoterm = incoterm;

    var body = {
      asin: (F.elements.asin.value || '').trim() || 'UNKNOWN',
      category: F.elements.category.value,
      opportunity_score: num('opportunity_score'),
      cost: cost
    };

    var sku = F.elements.sku_key.value;
    if (sku) {
      body.sku_key = sku;
    } else {
      var rr = num('return_rate');
      if (rr !== undefined) cost.return_rate = rr;
    }

    if (body.opportunity_score === undefined) {
      OUT.textContent = '';
      var c0 = card();
      c0.appendChild(el('div', 'warn', '机会分必填（0–1）。它是需求侧的判据，缺了只能判「需求不足」。'));
      OUT.appendChild(c0);
      return;
    }

    GO.disabled = true;
    GO.textContent = '计算中…';
    fetch('/v1/decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, status: r.status, data: d }; });
    }).then(function (res) {
      if (!res.ok) {
        OUT.textContent = '';
        var c = card('请求被拒（HTTP ' + res.status + '）');
        c.appendChild(el('div', 'warn', (res.data && (res.data.message || res.data.detail)) || '未知错误'));
        var code = res.data && res.data.code;
        if (code) c.appendChild(el('div', 'hint', '错误码：' + code));
        OUT.appendChild(c);
        return;
      }
      render(res.data);
    }).catch(function (err) {
      OUT.textContent = '';
      var c = card('连不上服务');
      c.appendChild(el('div', 'warn', String(err)));
      OUT.appendChild(c);
    }).then(function () {
      GO.disabled = false;
      GO.textContent = '算一下';
    });
  });
})();
</script>
</body></html>
"""


def render() -> str:
    """返回决策台页面。无入参，所以没有可注入的位置。"""
    return BODY.replace("__STYLE__", STYLE)
