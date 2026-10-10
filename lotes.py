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
from layout_v2 import ICONES, render_page

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


_rodando = [False]


def _bg():
    try:
        atualizar()
    finally:
        _rodando[0] = False


TEMPLATE = """
<style>
.v2-wrap{max-width:1100px}
</style>
<div class="v2-wrap">
<div class="v2-h">
  <div>
    <h1>Etiquetas<span class="v2-badge">Modo visualização</span></h1>
    <div class="v2-sub">{% if s.atualizado_em %}Atualizado em {{ s.atualizado_em }} · {% endif %}atualiza só quando você clicar em Atualizar agora</div>
  </div>
  <form method="post" action="{{ url_for('lotes.atualizar_agora') }}"><button type="submit" class="v2-btn">{{ ic.recarregar|safe }}Atualizar agora</button></form>
</div>
{% if rodando %}<div class="v2-flash">Atualizando as etiquetas... esta página recarrega sozinha.</div><script>setTimeout(function(){location.reload()},5000)</script>{% endif %}
{% if s.erro %}<div class="v2-flash">Não consegui atualizar agora: {{ s.erro }}</div>{% endif %}
{% if 'lotes' in s %}
<div class="v2-cards">
  <div class="v2-card y"><span class="ci">{{ ic.pendente|safe }}</span><div><div class="l">Total do dia</div><div class="n">{{ s.total }}</div></div></div>
  <div class="v2-card o"><span class="ci">{{ ic.relogio|safe }}</span><div><div class="l">Em aberto</div><div class="n">{{ s.em_aberto }}</div></div></div>
  <div class="v2-card g"><span class="ci">{{ ic.concluidos|safe }}</span><div><div class="l">Já processados</div><div class="n">{{ s.processados }}</div></div></div>
</div>
<div class="v2-sec">
  <h2>Lotes disponíveis</h2>
  <div class="r"><span>{{ s.lotes|length }} lote{{ '' if s.lotes|length == 1 else 's' }}</span>
    <button type="button" class="v2-btn sec" id="lt-all">Selecionar todos</button>
    <button type="button" class="v2-btn sec" id="lt-none">Limpar</button>
  </div>
</div>
<div class="v2-list">
  {% for l in s.lotes %}
  <label class="v2-lote">
    <input type="checkbox" class="lt-chk" data-n="{{ l.count }}" data-nome="{{ l.label }}">
    <span class="v2-lt"><b>{{ l.label }}</b>{% if l.nota %}<small>{{ l.nota }}</small>{% endif %}</span>
    <span class="v2-cnt">{{ l.count }}</span>
  </label>
  {% endfor %}
</div>
<div class="v2-bar">
  <div class="t"><span id="lt-sel">Nenhum arquivo selecionado</span><b id="lt-tot">0</b> <span style="display:inline">etiquetas</span> <span class="v2-msg" id="lt-msg"></span></div>
  <button type="button" class="v2-btn" id="lt-go">{{ ic.baixar|safe }}Gerar arquivo</button>
</div>
<script>
(function(){
  var chks=document.querySelectorAll(".lt-chk"), sel=document.getElementById("lt-sel"), tot=document.getElementById("lt-tot"), msg=document.getElementById("lt-msg");
  function upd(){
    var c=0,e=0;
    chks.forEach(function(x){
      x.parentNode.classList.toggle("sel",x.checked);
      if(x.checked){c++;e+=parseInt(x.dataset.n,10);}
    });
    sel.textContent=c?(c+(c>1?" arquivos selecionados":" arquivo selecionado")):"Nenhum arquivo selecionado";
    tot.textContent=e;
    msg.textContent="";
  }
  chks.forEach(function(x){x.addEventListener("change",upd);});
  document.getElementById("lt-all").addEventListener("click",function(){chks.forEach(function(x){x.checked=true;});upd();});
  document.getElementById("lt-none").addEventListener("click",function(){chks.forEach(function(x){x.checked=false;});upd();});
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
"""


@bp.route("/lotes")
def painel():
    return render_page("Etiquetas", "etiquetas", TEMPLATE, s=_ler(), ic=ICONES, rodando=_rodando[0])


@bp.route("/lotes/atualizar", methods=["POST"])
def atualizar_agora():
    if not _rodando[0]:
        _rodando[0] = True
        threading.Thread(target=_bg, daemon=True).start()
    return redirect(url_for("lotes.painel"))


def init_app(app, get_client):
    _get_client[0] = get_client
    app.register_blueprint(bp)
