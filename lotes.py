"""Painel de lotes de etiquetas (ETAPA 1: somente leitura).

Le da Shopee os pedidos aguardando organizacao (READY_TO_SHIP), classifica cada um em
um lote por filtro de produto e mostra a previsao na tela /lotes.

NAO organiza envio, NAO gera etiqueta e NAO altera nada na Shopee nem na fila de
separacao do dashboard. So consulta (GET) e guarda o resultado num cache proprio.
"""

import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone

from flask import Blueprint, redirect, render_template_string, url_for

import models

bp = Blueprint("lotes", __name__)

MIN_ETIQUETAS = 30           # filtro com menos pedidos que isso vai para DIVERSOS UNITARIOS
REFRESH_SECONDS = 30 * 60    # atualizacao automatica
BRT = timezone(timedelta(hours=-3))

LABEL_MAIS_DE_1 = "DIVERSOS: MAIS DE 1 ITEM"
LABEL_UNITARIOS = "DIVERSOS UNITÁRIOS"

# Ordem de PRIORIDADE de captura: o primeiro filtro que servir fica com o pedido.
# Os especificos vem antes dos genericos (COPO ALCA REMOVIVEL e COPO 160ML antes de COPO).
# A ordem na TELA e outra: sempre do maior para o menor numero de etiquetas.
# (rotulo exibido, texto de busca)
FILTROS = [
    ("TAPETE DOBRÁVEL", "TAPETE DOBRAVEL"),
    ("GELADEIRA", "GELADEIRA"),
    ("COPO ALÇA REMOVIVEL", "COPO ALCA REMOVIVEL"),
    ("COPO 160ML", "COPO 160ML"),
    ("COPO", "COPO"),
    ("GARRAFA ALUMINIO", "GARRAFA ALUMINIO"),
    ("GEL DE FEBRE", "GEL DE FEBRE"),
    ("BOLSA TÉRMICA", "BOLSA TERMICA"),
    ("REPELENTE", "REPELENTE"),
    ("PROTETOR SOLAR", "PROTETOR SOLAR"),
    ("MAMADEIRA", "MAMADEIRA"),
    ("MORDEDOR", "MORDEDOR"),
    ("COLHER", "COLHER"),
    ("CHUPETA", "CHUPETA"),
    ("PORTA FRUTINHA", "PORTA FRUTINHA"),
    ("KIT 3 POTES", "KIT 3 POTES"),
]

_STOP = {"DE", "DA", "DO"}


def _norm(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c)).upper()
    return re.sub(r"(\d)\s+ML\b", r"\1ML", texto)


def _compilar(busca):
    tokens = [t for t in _norm(busca).split() if t not in _STOP]
    return [re.compile(r"\b" + re.escape(t)) for t in tokens]


_PADROES = [(rotulo, _compilar(busca)) for rotulo, busca in FILTROS]


def classificar(itens):
    """Devolve o rotulo do lote do pedido, ou None se nenhum filtro serviu."""
    if len(itens) > 1:
        return LABEL_MAIS_DE_1
    if not itens:
        return None
    texto = _norm("%s %s" % (itens[0].get("item_name") or "", itens[0].get("model_name") or ""))
    for rotulo, padroes in _PADROES:
        if all(p.search(texto) for p in padroes):
            return rotulo
    return None


def _listar(client, status, de, ate, campo):
    sns = []
    cursor = ""
    while True:
        res = client.get_order_list(de, ate, cursor=cursor, order_status=status, page_size=100, time_range_field=campo)
        if res.get("error"):
            raise RuntimeError("Shopee: %s %s" % (res.get("error"), res.get("message")))
        resp = res.get("response") or {}
        sns.extend(o["order_sn"] for o in resp.get("order_list", []))
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more") or not cursor:
            break
    return sns


def calcular(client):
    agora = int(time.time())
    prontos = _listar(client, "READY_TO_SHIP", agora - 14 * 86400, agora, "update_time")
    meia_noite = int(datetime.now(BRT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    processados_sn = _listar(client, "PROCESSED", meia_noite, agora, "update_time")

    contagem = Counter()
    resto = Counter()
    fbs = 0
    abertos = 0
    for i in range(0, len(prontos), 50):
        res = client.get_order_detail(prontos[i:i + 50])
        if res.get("error"):
            raise RuntimeError("Shopee: %s %s" % (res.get("error"), res.get("message")))
        for od in (res.get("response") or {}).get("order_list", []):
            if od.get("fulfillment_flag") == "fulfilled_by_shopee":
                fbs += 1
                continue
            abertos += 1
            itens = od.get("item_list") or []
            rotulo = classificar(itens)
            if rotulo is None:
                rotulo = "_RESTO"
                if itens:
                    nome = itens[0].get("item_name") or "?"
                    var = itens[0].get("model_name")
                    resto[nome + (" / " + var if var else "")] += 1
            contagem[rotulo] += 1

    processados = 0
    for i in range(0, len(processados_sn), 50):
        res = client.get_order_detail(processados_sn[i:i + 50])
        if res.get("error"):
            raise RuntimeError("Shopee: %s %s" % (res.get("error"), res.get("message")))
        for od in (res.get("response") or {}).get("order_list", []):
            if od.get("fulfillment_flag") != "fulfilled_by_shopee":
                processados += 1

    unitarios = contagem.pop("_RESTO", 0)
    sem_filtro = unitarios
    lotes = []
    absorvidos = []
    mais = contagem.pop(LABEL_MAIS_DE_1, 0)
    if mais:
        lotes.append({"label": LABEL_MAIS_DE_1, "count": mais, "nota": ""})
    for rotulo, _ in FILTROS:
        n = contagem.get(rotulo, 0)
        if not n:
            continue
        if n >= MIN_ETIQUETAS:
            lotes.append({"label": rotulo, "count": n, "nota": ""})
        else:
            absorvidos.append("%s %d" % (rotulo, n))
            unitarios += n
    if unitarios:
        partes = list(absorvidos)
        if sem_filtro:
            partes.append("sem filtro %d" % sem_filtro)
        lotes.append({"label": LABEL_UNITARIOS, "count": unitarios, "nota": "inclui: " + ", ".join(partes) if partes else ""})
    lotes.sort(key=lambda x: -x["count"])

    return {
        "lotes": lotes,
        "em_aberto": abertos,
        "processados": processados,
        "total": abertos + processados,
        "fbs": fbs,
        "soma_lotes": sum(x["count"] for x in lotes),
        "resto_top": resto.most_common(20),
        "minimo": MIN_ETIQUETAS,
        "erro": None,
    }


def _conn():
    conn = sqlite3.connect(models.DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS lotes_snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")
    return conn


def _ler():
    try:
        conn = _conn()
        try:
            row = conn.execute("SELECT data FROM lotes_snapshot WHERE id = 1").fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _salvar(snap):
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO lotes_snapshot (id, data) VALUES (1, ?)", (json.dumps(snap),))
        conn.commit()
    finally:
        conn.close()


_lock = threading.Lock()
_get_client = [None]


def atualizar():
    if not _lock.acquire(False):
        return False
    try:
        try:
            client = _get_client[0]()
            if client is None:
                raise RuntimeError("Loja ainda não autorizada na Shopee.")
            snap = calcular(client)
            snap["atualizado_em"] = datetime.now(BRT).strftime("%d/%m %H:%M")
        except Exception as e:
            snap = _ler()
            snap["erro"] = str(e)
        _salvar(snap)
        return True
    finally:
        _lock.release()


def _loop():
    time.sleep(20)
    while True:
        try:
            atualizar()
        except Exception:
            pass
        time.sleep(REFRESH_SECONDS)


TEMPLATE = """
{% extends "base.html" %}
{% block title %}Lotes de etiquetas{% endblock %}
{% block content %}
<style>
.lt-top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.lt-sub{font-size:13px;color:#666}
.lt-badge{font-size:12px;background:#fff3cd;color:#664d03;padding:2px 8px;border-radius:6px;margin-left:8px}
.lt-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:12px 0 16px}
.lt-card{background:#f5f5f5;border-radius:8px;padding:10px 14px}
.lt-card span{display:block;font-size:12px;color:#666}
.lt-card b{font-size:26px;font-weight:600}
.lt-table{width:100%;border-collapse:collapse}
.lt-table th{text-align:left;font-size:12px;color:#666;font-weight:400;padding:6px 8px;border-bottom:1px solid #ddd}
.lt-table td{padding:9px 8px;border-bottom:1px solid #eee}
.lt-n{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.lt-note{font-size:12px;color:#666}
</style>
<div class="lt-top">
  <div>
    <h2 style="margin:0">Lotes de etiquetas<span class="lt-badge">Modo visualização</span></h2>
    <div class="lt-sub">{% if s.atualizado_em %}Atualizado em {{ s.atualizado_em }} · {% endif %}atualiza sozinho a cada 30 minutos</div>
  </div>
  <form method="post" action="{{ url_for('lotes.atualizar_agora') }}"><button type="submit">Atualizar agora</button></form>
</div>
{% if s.erro %}<div class="flash">Não consegui atualizar agora: {{ s.erro }}</div>{% endif %}
{% if 'lotes' in s %}
<div class="lt-cards">
  <div class="lt-card"><span>Total do dia (em aberto + processados)</span><b>{{ s.total }}</b></div>
  <div class="lt-card"><span>Em aberto (aguardando na Shopee)</span><b>{{ s.em_aberto }}</b></div>
  <div class="lt-card"><span>Já processados hoje</span><b>{{ s.processados }}</b></div>
</div>
<table class="lt-table">
  <thead><tr><th>Arquivo</th><th class="lt-n">Etiquetas</th></tr></thead>
  <tbody>
  {% for l in s.lotes %}
    <tr><td>{{ l.label }}{% if l.nota %}<div class="lt-note">{{ l.nota }}</div>{% endif %}</td><td class="lt-n">{{ l.count }}</td></tr>
  {% endfor %}
    <tr><td class="lt-note">Soma dos arquivos</td><td class="lt-n">{{ s.soma_lotes }}</td></tr>
  </tbody>
</table>
<p class="lt-note">Filtros com menos de {{ s.minimo }} pedidos vão para DIVERSOS UNITÁRIOS. Esta tela só lê: não organiza envio nem gera etiqueta.</p>
{% if s.resto_top %}
<details><summary class="lt-note">Itens sem filtro que estão em DIVERSOS UNITÁRIOS</summary>
  <ul class="lt-note">{% for nome, n in s.resto_top %}<li>{{ n }} × {{ nome }}</li>{% endfor %}</ul>
</details>
{% endif %}
{% else %}
<p>Ainda sem dados. Clique em "Atualizar agora".</p>
{% endif %}
{% endblock %}
"""


@bp.route("/lotes")
def painel():
    return render_template_string(TEMPLATE, s=_ler())


@bp.route("/lotes/atualizar", methods=["POST"])
def atualizar_agora():
    atualizar()
    return redirect(url_for("lotes.painel"))


@bp.route("/lotes/debug-especiais")
def debug_especiais():
    from flask import jsonify
    client = _get_client[0]()
    agora = int(time.time())
    prontos = _listar(client, "READY_TO_SHIP", agora - 14 * 86400, agora, "update_time")
    por_canal = Counter()
    amostra = {}
    for i in range(0, len(prontos), 50):
        res = client.get_order_detail(prontos[i:i + 50])
        for od in (res.get("response") or {}).get("order_list", []):
            if od.get("fulfillment_flag") == "fulfilled_by_shopee":
                continue
            canal = od.get("shipping_carrier") or "?"
            por_canal[canal] += 1
            amostra.setdefault(canal, od["order_sn"])
    out = {"por_canal": dict(por_canal), "amostras": {}}
    for canal, sn in amostra.items():
        item = {"order_sn": sn}
        try:
            item["shipping_parameter"] = client._shop_request("GET", "/api/v2/logistics/get_shipping_parameter", params={"order_sn": sn})
        except Exception as e:
            item["shipping_parameter_erro"] = str(e)
        try:
            item["document_parameter"] = client._shop_request("POST", "/api/v2/logistics/get_shipping_document_parameter", json_body={"order_list": [{"order_sn": sn}]})
        except Exception as e:
            item["document_parameter_erro"] = str(e)
        out["amostras"][canal] = item
    return jsonify(out)


def init_app(app, get_client):
    _get_client[0] = get_client
    app.register_blueprint(bp)
    threading.Thread(target=_loop, daemon=True).start()
