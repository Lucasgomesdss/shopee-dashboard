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
{% block title %}Etiquetas{% endblock %}
{% block sidebar %}{% endblock %}
{% block content %}
<style>
.layout{display:block !important}
.lt-wrap{max-width:1100px;margin:0 auto}
.lt-top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.lt-sub{font-size:13px;color:#666}
.lt-badge{font-size:12px;background:#fff3cd;color:#664d03;padding:2px 8px;border-radius:6px;margin-left:8px;font-weight:400}
.lt-update{background:#1a73e8;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer}
.lt-update:hover{background:#1558b0}
.lt-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 20px}
.lt-card{border-radius:12px;padding:14px 18px}
.lt-card span{display:block;font-size:13px;font-weight:600}
.lt-card b{display:block;font-size:38px;font-weight:700;line-height:1.2}
.lt-total{background:#fff3b0;color:#6b5600}
.lt-open{background:#ffd9b3;color:#8a3b00}
.lt-done{background:#c8efc8;color:#14551a}
.lt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.lt-lote{display:block;position:relative;border:2px solid #ddd;border-radius:12px;padding:14px 16px 14px 44px;background:#fff;cursor:pointer}
.lt-lote:hover{border-color:#1a73e8}
.lt-lote.sel{border-color:#1a73e8;background:#e8f0fe}
.lt-lote input{position:absolute;left:14px;top:16px;width:20px;height:20px;cursor:pointer}
.lt-name{display:block;font-size:15px;font-weight:700}
.lt-count{display:block;font-size:30px;font-weight:700;line-height:1.2}
.lt-note{display:block;font-size:11px;color:#666;margin-top:4px}
.lt-bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:20px}
.lt-go{background:#1a73e8;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer}
.lt-msg{font-size:13px;color:#8a3b00}
</style>
<div class="lt-wrap">
<div class="lt-top">
  <div>
    <h2 style="margin:0">Etiquetas<span class="lt-badge">Modo visualização</span></h2>
    <div class="lt-sub">{% if s.atualizado_em %}Atualizado em {{ s.atualizado_em }} · {% endif %}atualiza sozinho a cada 30 minutos</div>
  </div>
  <form method="post" action="{{ url_for('lotes.atualizar_agora') }}"><button type="submit" class="lt-update">Atualizar agora</button></form>
</div>
{% if s.erro %}<div class="flash">Não consegui atualizar agora: {{ s.erro }}</div>{% endif %}
{% if 'lotes' in s %}
<div class="lt-cards">
  <div class="lt-card lt-total"><span>Total do dia</span><b>{{ s.total }}</b></div>
  <div class="lt-card lt-open"><span>Em aberto</span><b>{{ s.em_aberto }}</b></div>
  <div class="lt-card lt-done"><span>Já processados</span><b>{{ s.processados }}</b></div>
</div>
<div class="lt-grid">
  {% for l in s.lotes %}
  <label class="lt-lote">
    <input type="checkbox" class="lt-chk" data-n="{{ l.count }}" data-nome="{{ l.label }}">
    <span class="lt-name">{{ l.label }}</span>
    <span class="lt-count">{{ l.count }}</span>
    {% if l.nota %}<span class="lt-note">{{ l.nota }}</span>{% endif %}
  </label>
  {% endfor %}
</div>
<div class="lt-bar">
  <button type="button" class="lt-go" id="lt-go">Gerar arquivo</button>
  <span id="lt-sel">Nenhum arquivo selecionado</span>
  <span class="lt-msg" id="lt-msg"></span>
</div>
<script>
(function(){
  var chks=document.querySelectorAll(".lt-chk"), sel=document.getElementById("lt-sel"), msg=document.getElementById("lt-msg");
  function upd(){
    var c=0,e=0;
    chks.forEach(function(x){
      x.parentNode.classList.toggle("sel",x.checked);
      if(x.checked){c++;e+=parseInt(x.dataset.n,10);}
    });
    sel.textContent=c?(c+(c>1?" arquivos selecionados, ":" arquivo selecionado, ")+e+" etiquetas"):"Nenhum arquivo selecionado";
    msg.textContent="";
  }
  chks.forEach(function(x){x.addEventListener("change",upd);});
  document.getElementById("lt-go").addEventListener("click",function(){
    var c=0; chks.forEach(function(x){if(x.checked)c++;});
    msg.textContent=c?"A geração ainda está desativada: o painel está em modo visualização.":"Marque pelo menos um arquivo.";
  });
})();
</script>
{% else %}
<p>Ainda sem dados. Clique em "Atualizar agora".</p>
{% endif %}
</div>
{% endblock %}
"""


@bp.route("/lotes")
def painel():
    return render_template_string(TEMPLATE, s=_ler())


@bp.route("/lotes/atualizar", methods=["POST"])
def atualizar_agora():
    atualizar()
    return redirect(url_for("lotes.painel"))


def init_app(app, get_client):
    _get_client[0] = get_client
    app.register_blueprint(bp)
    threading.Thread(target=_loop, daemon=True).start()
