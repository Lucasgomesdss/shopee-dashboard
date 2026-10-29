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


CAMPOS_DETALHE = "item_list,shipping_carrier,package_list,fulfillment_flag,invoice_data"


def detalhes(client, sns):
    """Detalhe dos pedidos incluindo a nota fiscal (invoice_data) — até 50 por chamada."""
    res = client._shop_request("GET", "/api/v2/order/get_order_detail",
                               params={"order_sn_list": ",".join(sns), "response_optional_fields": CAMPOS_DETALHE})
    if res.get("error"):
        raise RuntimeError("Shopee: %s %s" % (res.get("error"), res.get("message")))
    return (res.get("response") or {}).get("order_list", [])


def tem_nota(od):
    """A Shopee só deixa organizar o envio depois que a nota fiscal chegou nela."""
    inv = od.get("invoice_data") or {}
    return bool(inv.get("number") or inv.get("access_key"))


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
    membros = {}   # rótulo -> [(chave de ordenação, order_sn)]
    fbs = 0
    abertos = 0
    sem_nf = 0
    for i in range(0, len(prontos), 50):
        for od in detalhes(client, prontos[i:i + 50]):
            if od.get("fulfillment_flag") == "fulfilled_by_shopee":
                fbs += 1
                continue
            abertos += 1
            if not tem_nota(od):
                sem_nf += 1      # fica fora dos lotes até faturar
                continue
            itens = od.get("item_list") or []
            rotulo = classificar(itens)
            chave = " | ".join(
                "%s %s" % (_norm(i.get("item_name")), _norm(i.get("model_name"))) for i in itens
            )
            if rotulo is None:
                rotulo = "_RESTO"
                if itens:
                    nome = itens[0].get("item_name") or "?"
                    var = itens[0].get("model_name")
                    resto[nome + (" / " + var if var else "")] += 1
            contagem[rotulo] += 1
            membros.setdefault(rotulo, []).append((chave, od["order_sn"]))

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
    pedidos = {LABEL_UNITARIOS: list(membros.get("_RESTO", []))}
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
            pedidos[rotulo] = list(membros.get(rotulo, []))
        else:
            absorvidos.append("%s %d" % (rotulo, n))
            unitarios += n
            pedidos[LABEL_UNITARIOS].extend(membros.get(rotulo, []))
    if mais:
        pedidos[LABEL_MAIS_DE_1] = list(membros.get(LABEL_MAIS_DE_1, []))
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
        "sem_nf": sem_nf,
        "soma_lotes": sum(x["count"] for x in lotes),
        "resto_top": resto.most_common(20),
        "minimo": MIN_ETIQUETAS,
        "erro": None,
        # Pedidos de cada lote, ordenados por produto (mesmo produto fica junto no arquivo).
        "pedidos": {k: [sn for _, sn in sorted(v)] for k, v in pedidos.items() if v},
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
    <h1>Etiquetas{% if not envio.pode %}<span class="v2-badge">Modo visualização</span>{% endif %}</h1>
    <div class="v2-sub">{% if s.atualizado_em %}Atualizado em {{ s.atualizado_em }} · {% endif %}atualiza sozinho de segunda a sábado às 6:50 · ou clique em Atualizar agora</div>
  </div>
  <form method="post" action="{{ url_for('lotes.atualizar_agora') }}"><button type="submit" class="v2-btn">{{ ic.recarregar|safe }}Atualizar agora</button></form>
</div>
{% if rodando %}<div class="v2-flash">Atualizando as etiquetas... esta página recarrega sozinha.</div><script>setTimeout(function(){location.reload()},5000)</script>{% endif %}
{% if s.erro %}<div class="v2-flash">Não consegui atualizar agora: {{ s.erro }}</div>{% endif %}
{% set t = envio.trabalho %}
{% if t %}
<div class="v2-flash" style="{% if t.status == 'erro' %}border-color:#e05252{% endif %}">
  <b>{% if t.status == 'rodando' %}Organizando envio…{% elif t.status == 'concluido' %}Último envio organizado{% else %}O último envio parou{% endif %}</b>
  — {{ t.etapa }} · {{ t.n_org }} de {{ t.total }} pedido(s) organizados{% if t.iniciado_por %} · por {{ t.iniciado_por }}{% endif %}
  {% if t.falhas_l %}<details style="margin-top:6px"><summary>{{ t.falhas_l|length }} pedido(s) com problema</summary>
    <ul style="margin:6px 0 0 18px">{% for f in t.falhas_l[:60] %}<li><span style="font-family:monospace">{{ f[0] }}</span> — {{ f[1] }}</li>{% endfor %}</ul></details>{% endif %}
  {% if t.status == 'erro' and t.n_org and envio.pode %}<form method="post" action="{{ url_for('lotes_envio.refazer', tid=t.id) }}" style="margin-top:8px"><button class="v2-btn sec" type="submit">Gerar arquivo dos já organizados</button></form>{% endif %}
</div>
{% if envio.ativo %}<script>setTimeout(function(){location.reload()},5000)</script>{% endif %}
{% endif %}
{% if 'lotes' in s %}
<div class="v2-cards">
  <div class="v2-card y"><span class="ci">{{ ic.pendente|safe }}</span><div><div class="l">Total do dia</div><div class="n">{{ s.total }}</div></div></div>
  <div class="v2-card o"><span class="ci">{{ ic.relogio|safe }}</span><div><div class="l">Em aberto</div><div class="n">{{ s.em_aberto }}</div></div></div>
  <div class="v2-card g"><span class="ci">{{ ic.concluidos|safe }}</span><div><div class="l">Já processados</div><div class="n">{{ s.processados }}</div></div></div>
</div>
<div class="v2-sec">
  <h2>Lotes disponíveis <small style="font-weight:400;font-size:13px;color:#5b6474">· um lote por vez</small></h2>
  <div class="r"><span>{{ s.lotes|length }} lote{{ '' if s.lotes|length == 1 else 's' }}</span>
    <button type="button" class="v2-btn sec" id="lt-none">Limpar</button>
  </div>
</div>
{% if s.sem_nf %}<div class="v2-flash" style="margin-bottom:10px">{{ s.sem_nf }} pedido(s) ainda <b>sem nota fiscal na Shopee</b> — ficam fora dos lotes e entram sozinhos quando faturar.</div>{% endif %}
<div class="v2-list">
  {% for l in s.lotes %}
  <label class="v2-lote">
    <input type="radio" class="lt-chk" name="lote" value="{{ l.label }}" form="lt-form" data-n="{{ l.count }}" data-nome="{{ l.label }}">
    <span class="v2-lt"><b>{{ l.label }}</b>{% if l.nota %}<small>{{ l.nota }}</small>{% endif %}</span>
    <span class="v2-cnt">{{ l.count }}</span>
  </label>
  {% endfor %}
</div>
<form id="lt-form" method="post" action="{{ url_for('lotes_envio.organizar') }}"></form>
<div class="v2-bar">
  <div class="t"><span id="lt-sel">Nenhum arquivo selecionado</span><b id="lt-tot">0</b> <span style="display:inline">etiquetas</span> <span class="v2-msg" id="lt-msg"></span></div>
  {% if envio.pode %}<label style="font-size:13px;display:flex;align-items:center;gap:6px">Quantidade (teste)
    <input type="number" name="limite" form="lt-form" min="1" placeholder="todos" style="width:80px;font:inherit;padding:6px 8px;border:1px solid #c7ccd6;border-radius:8px"></label>{% endif %}
  <button type="button" class="v2-btn" id="lt-go" {% if envio.ativo %}disabled{% endif %}>{{ ic.baixar|safe }}Organizar envio e gerar arquivo</button>
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
    sel.textContent=c?"1 lote selecionado":"Nenhum lote selecionado";
    tot.textContent=e;
    msg.textContent="";
  }
  chks.forEach(function(x){x.addEventListener("change",upd);});
  document.getElementById("lt-none").addEventListener("click",function(){chks.forEach(function(x){x.checked=false;});upd();});
  document.getElementById("lt-go").addEventListener("click",function(){
    var c=0,e=0; chks.forEach(function(x){if(x.checked){c++;e+=parseInt(x.dataset.n,10);}});
    if(!c){msg.textContent="Marque pelo menos um lote.";return;}
    {% if not envio.pode %}msg.textContent="Entre com o seu nome para organizar o envio.";return;{% endif %}
    var lim=document.querySelector("input[name=limite]"); var n=(lim&&lim.value)?Math.min(parseInt(lim.value,10),e):e;
    if(confirm("Organizar o envio de "+n+" pedido(s) na Shopee e gerar o arquivo da Zebra?\n\nIsso muda o status na Shopee para 'A enviar/processado' e NÃO tem volta.")){
      document.getElementById("lt-go").disabled=true; document.getElementById("lt-form").submit();
    }
  });
})();
</script>
{% else %}
<p>Ainda sem dados. Clique em "Atualizar agora".</p>
{% endif %}
{% if envio.arquivos %}
<div class="v2-sec"><h2>Arquivos gerados</h2></div>
<div class="v2-list">
  {% for a in envio.arquivos %}
  <div class="v2-lote" style="cursor:default"><span class="v2-lt"><b>{{ a.lote }}</b><small>{{ a.quando }} · {{ a.nome }}</small></span>
    <span class="v2-cnt">{{ a.qtd }}</span>
    <a class="v2-btn sec" href="{{ url_for('lotes_envio.baixar', aid=a.id) }}">{{ ic.baixar|safe }}Baixar .txt</a></div>
  {% endfor %}
</div>
{% endif %}
</div>
"""


@bp.route("/lotes")
def painel():
    import lotes_envio
    return render_page("Etiquetas", "etiquetas", TEMPLATE, s=_ler(), ic=ICONES, rodando=_rodando[0],
                       envio=lotes_envio.resumo_tela())


@bp.route("/lotes/atualizar", methods=["POST"])
def atualizar_agora():
    if not _rodando[0]:
        _rodando[0] = True
        threading.Thread(target=_bg, daemon=True).start()
    return redirect(url_for("lotes.painel"))


def init_app(app, get_client):
    _get_client[0] = get_client
    app.register_blueprint(bp)
    import reimpressao
    reimpressao.init_app(app, get_client)
    import lotes_envio
    lotes_envio.init_app(app, get_client)
