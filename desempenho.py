"""Página Desempenho: pedidos separados por hora, uma linha por colaborador.

Somente leitura: usa os dados que já existem na tabela orders (employee_name e
confirmed_at, gravados quando o colaborador confirma o pedido). Não altera nada.
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template_string, request

import models

bp = Blueprint("desempenho", __name__)

BRT = timezone(timedelta(hours=-3))
# Quem aparece no gráfico (LUCAS não separa). Cores fixas por pessoa.
COLABORADORES = [
    ("JULIANA", "#2a78d6"),
    ("RAFAELA", "#eb6834"),
    ("DANIELA", "#1baf7a"),
]


def _dia_param():
    hoje = datetime.now(BRT).date()
    txt = (request.args.get("dia") or "").strip()
    try:
        return datetime.strptime(txt, "%Y-%m-%d").date()
    except ValueError:
        return hoje


def calcular(dia):
    """Conta pedidos confirmados por colaborador e hora (horário de Brasília) no dia."""
    ini = datetime(dia.year, dia.month, dia.day, tzinfo=BRT).astimezone(timezone.utc)
    fim = ini + timedelta(days=1)
    nomes = [n for n, _ in COLABORADORES]
    marks = ",".join("?" for _ in nomes)
    with models.get_conn() as conn:
        rows = conn.execute(
            "SELECT employee_name, confirmed_at FROM orders "
            "WHERE status IN ('completed','archived') AND confirmed_at IS NOT NULL "
            "AND employee_name IN (" + marks + ")",
            nomes,
        ).fetchall()
    por_hora = {n: [0] * 24 for n in nomes}
    for r in rows:
        try:
            t = datetime.fromisoformat(r["confirmed_at"])
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if not (ini <= t < fim):
            continue
        por_hora[r["employee_name"]][t.astimezone(BRT).hour] += 1
    usadas = [h for h in range(24) if any(por_hora[n][h] for n in nomes)]
    h0 = min(6, min(usadas)) if usadas else 6
    h1 = max(18, max(usadas)) if usadas else 18
    horas = list(range(h0, h1 + 1))
    series = [
        {
            "nome": n,
            "cor": c,
            "total": sum(por_hora[n]),
            "dados": [por_hora[n][h] for h in horas],
        }
        for n, c in COLABORADORES
    ]
    return {"labels": ["%02dh" % h for h in horas], "series": series}


TEMPLATE = """{% extends "base.html" %}
{% block title %}Desempenho{% endblock %}
{% block sidebar %}{% endblock %}
{% block content %}
<style>
.dp-wrap{max-width:900px}
.dp-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.dp-top h2{margin:0}
.dp-cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.dp-card{flex:1;min-width:130px;background:#fff;border:1px solid #e1e0d9;padding:12px 14px}
.dp-card .n{font-size:26px;font-weight:700}
.dp-card .l{font-size:13px;color:#52514e}
.dp-chart{position:relative;height:340px;background:#fff;border:1px solid #e1e0d9;padding:10px}
</style>
<div class="dp-wrap">
  <div class="dp-top">
    <h2>Desempenho</h2>
    <form method="get" style="margin:0">
      <input type="date" name="dia" value="{{ dia }}" onchange="this.form.submit()">
    </form>
  </div>
  <div class="dp-cards">
    {% for s in series %}
    <div class="dp-card" style="border-left:5px solid {{ s.cor }}">
      <div class="l">{{ s.nome }}</div><div class="n">{{ s.total }}</div>
    </div>
    {% endfor %}
  </div>
  <div class="dp-chart"><canvas id="dp"></canvas></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
var labels = {{ labels|tojson }};
var series = {{ series|tojson }};
new Chart(document.getElementById('dp'), {
  type: 'line',
  data: {labels: labels, datasets: series.map(function(s){return {
    label: s.nome, data: s.dados, borderColor: s.cor, backgroundColor: s.cor,
    borderWidth: 2, tension: 0.3, pointRadius: 4, pointHoverRadius: 6,
    pointBorderColor: '#fff', pointBorderWidth: 2};})},
  options: {responsive: true, maintainAspectRatio: false,
    interaction: {mode: 'index', intersect: false},
    plugins: {legend: {position: 'top'}},
    scales: {x: {grid: {display: false}},
      y: {beginAtZero: true, ticks: {precision: 0}, title: {display: true, text: 'Pedidos na hora'}}}}
});
</script>
{% endblock %}
"""


@bp.route("/desempenho")
def painel():
    dia = _dia_param()
    dados = calcular(dia)
    return render_template_string(
        TEMPLATE, dia=dia.isoformat(), labels=dados["labels"], series=dados["series"]
    )


def init_app(app):
    app.register_blueprint(bp)
