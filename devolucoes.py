"""Tela Devoluções: bipar a etiqueta de um pacote devolvido e achar pedido + nota.

Fluxo (projeto "Devoluções pelo rastreio"):
  rastreio -> pedido Shopee (banco do dashboard ou lista de devoluções da Shopee)
           -> pedido de venda no Bling (conta Ana) -> nota de saída (chave, itens, cliente)

FASE 2 = SÓ CONSULTA. Nada aqui cria, emite ou altera nota no Bling, nem mexe no
estoque. A geração das notas de devolução entra na fase 3, depois do teste.

Acesso: só os logins JULIANA e LUCAS. A autorização do Bling (Conectar Bling) só LUCAS.
"""

import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, redirect, request, session, url_for

import bling_client
import models
from layout_v2 import ICONES, render_page

bp = Blueprint("devolucoes", __name__)

BRT = timezone(timedelta(hours=-3))
LIBERADOS = {"JULIANA", "LUCAS"}
DIAS_DEVOLUCOES_SHOPEE = 60   # quanto tempo para trás procurar na lista de devoluções
_get_shopee = [None]

ST_PRONTO = "pronto"
ST_NAO_ACHOU = "nao_encontrado"
ST_SEM_NOTA = "sem_nota"
ST_ERRO = "erro"

ROTULOS = {
    ST_PRONTO: ("Pronto", "ok"),
    ST_NAO_ACHOU: ("Rastreio não encontrado", "warn"),
    ST_SEM_NOTA: ("Sem nota no Bling", "warn"),
    ST_ERRO: ("Erro na consulta", "bad"),
}


# --------------------------------------------------------------------------- banco

def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devolucoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rastreio TEXT NOT NULL,
                order_sn TEXT,
                origem TEXT,
                bling_pedido_id INTEGER,
                bling_nfe_id INTEGER,
                nfe_numero TEXT,
                nfe_serie TEXT,
                nfe_chave TEXT,
                nfe_situacao INTEGER,
                cliente_nome TEXT,
                cliente_doc TEXT,
                total REAL,
                status TEXT NOT NULL,
                motivo TEXT,
                nfe_devolucao_id INTEGER,
                nfe_devolucao_numero TEXT,
                bipado_por TEXT,
                removido INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dev_order ON devolucoes(order_sn)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devolucao_itens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                devolucao_id INTEGER NOT NULL,
                codigo TEXT,
                descricao TEXT,
                unidade TEXT,
                qtd_original REAL,
                qtd_devolver REAL,
                valor REAL,
                dados_json TEXT
            )
            """
        )


def _agora():
    return datetime.utcnow().isoformat()


def _listar():
    with models.get_conn() as conn:
        devs = [dict(r) for r in conn.execute(
            "SELECT * FROM devolucoes WHERE removido = 0 ORDER BY id DESC LIMIT 300"
        ).fetchall()]
        if devs:
            ids = [d["id"] for d in devs]
            marks = ",".join("?" for _ in ids)
            itens = conn.execute(
                f"SELECT * FROM devolucao_itens WHERE devolucao_id IN ({marks}) ORDER BY id", ids
            ).fetchall()
        else:
            itens = []
    por_dev = {}
    for it in itens:
        por_dev.setdefault(it["devolucao_id"], []).append(dict(it))
    for d in devs:
        d["itens"] = por_dev.get(d["id"], [])
        d["rotulo"], d["tom"] = ROTULOS.get(d["status"], (d["status"], "warn"))
        try:
            d["quando"] = datetime.fromisoformat(d["created_at"]).replace(
                tzinfo=timezone.utc).astimezone(BRT).strftime("%d/%m %H:%M")
        except (TypeError, ValueError):
            d["quando"] = ""
    return devs


def _ja_na_lista(rastreio, order_sn):
    with models.get_conn() as conn:
        if order_sn:
            row = conn.execute(
                "SELECT id FROM devolucoes WHERE removido = 0 AND (order_sn = ? OR rastreio = ?)",
                (order_sn, rastreio),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM devolucoes WHERE removido = 0 AND rastreio = ?", (rastreio,)
            ).fetchone()
        return row is not None


def _gravar(reg, itens):
    agora = _agora()
    with models.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO devolucoes (rastreio, order_sn, origem, bling_pedido_id, bling_nfe_id,
                 nfe_numero, nfe_serie, nfe_chave, nfe_situacao, cliente_nome, cliente_doc, total,
                 status, motivo, bipado_por, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                reg["rastreio"], reg.get("order_sn"), reg.get("origem"), reg.get("bling_pedido_id"),
                reg.get("bling_nfe_id"), reg.get("nfe_numero"), reg.get("nfe_serie"),
                reg.get("nfe_chave"), reg.get("nfe_situacao"), reg.get("cliente_nome"),
                reg.get("cliente_doc"), reg.get("total"), reg["status"], reg.get("motivo"),
                reg.get("bipado_por"), agora, agora,
            ),
        )
        dev_id = cur.lastrowid
        for it in itens:
            conn.execute(
                """INSERT INTO devolucao_itens (devolucao_id, codigo, descricao, unidade,
                     qtd_original, qtd_devolver, valor, dados_json) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    dev_id, it.get("codigo"), it.get("descricao"), it.get("unidade"),
                    it.get("quantidade"), it.get("quantidade"), it.get("valor"),
                    json.dumps(it, ensure_ascii=False),
                ),
            )
    return dev_id


# ------------------------------------------------------------------------- busca

def _normalizar(rastreio):
    r = (rastreio or "").strip().upper().replace(" ", "")
    if "SPXLM" in r:
        r = r.split("SPXLM")[0]
    return r


def _buscar_nas_devolucoes_shopee(rastreio):
    """Rastreio de retorno: procura na lista de devoluções da Shopee (janelas de 15 dias)."""
    client = _get_shopee[0]() if _get_shopee[0] else None
    if client is None:
        return None
    agora = int(time.time())
    fim = agora
    limite = agora - DIAS_DEVOLUCOES_SHOPEE * 86400
    while fim > limite:
        ini = max(fim - 15 * 86400, limite)
        for pagina in range(20):
            try:
                resp = client._shop_request(
                    "GET", "/api/v2/returns/get_return_list",
                    params={"page_no": pagina, "page_size": 100,
                            "create_time_from": ini, "create_time_to": fim},
                )
            except Exception:
                return None
            corpo = resp.get("response") or {}
            lista = corpo.get("return") or corpo.get("return_list") or []
            for dev in lista:
                rastreios = {
                    _normalizar(str(dev.get(k) or ""))
                    for k in ("tracking_number", "reverse_tracking_number", "return_tracking_number")
                }
                if rastreio in rastreios and dev.get("order_sn"):
                    return dev["order_sn"]
            if not corpo.get("more"):
                break
        fim = ini
    return None


def consultar(rastreio):
    """Resolve rastreio -> pedido Shopee -> pedido Bling -> nota de saída. Só leitura."""
    reg = {"rastreio": rastreio}
    itens = []

    local = models.find_by_tracking(rastreio)
    if local:
        reg["order_sn"], reg["origem"] = local["order_sn"], "banco"
    else:
        sn = _buscar_nas_devolucoes_shopee(rastreio)
        if sn:
            reg["order_sn"], reg["origem"] = sn, "devoluções Shopee"
    if not reg.get("order_sn"):
        reg["status"] = ST_NAO_ACHOU
        reg["motivo"] = "Não está no banco do dashboard nem na lista de devoluções da Shopee."
        return reg, itens

    try:
        pedidos = bling_client.buscar_pedido_por_numero_loja(reg["order_sn"])
        if not pedidos:
            reg["status"] = ST_SEM_NOTA
            reg["motivo"] = "Pedido não encontrado no Bling (conta Ana)."
            return reg, itens
        pedido = bling_client.detalhe_pedido(pedidos[0]["id"])
        reg["bling_pedido_id"] = pedido.get("id")
        nfe_id = (pedido.get("notaFiscal") or {}).get("id")
        if not nfe_id:
            reg["status"] = ST_SEM_NOTA
            reg["motivo"] = "O pedido no Bling não tem nota de saída ligada."
            reg["cliente_nome"] = (pedido.get("contato") or {}).get("nome")
            return reg, itens
        nfe = bling_client.detalhe_nfe(nfe_id)
    except bling_client.BlingErro as e:
        reg["status"] = ST_ERRO
        reg["motivo"] = f"Bling: {e}"
        return reg, itens
    except Exception as e:  # rede fora do ar etc.
        reg["status"] = ST_ERRO
        reg["motivo"] = f"Falha de conexão com o Bling: {e}"
        return reg, itens

    contato = nfe.get("contato") or {}
    reg.update(
        bling_nfe_id=nfe.get("id"),
        nfe_numero=str(nfe.get("numero") or ""),
        nfe_serie=str(nfe.get("serie") or ""),
        nfe_chave=nfe.get("chaveAcesso") or "",
        nfe_situacao=nfe.get("situacao"),
        cliente_nome=contato.get("nome"),
        cliente_doc=contato.get("numeroDocumento"),
        total=nfe.get("valorNota"),
    )
    for it in nfe.get("itens") or []:
        itens.append({
            "codigo": it.get("codigo"),
            "descricao": it.get("descricao"),
            "unidade": it.get("unidade"),
            "quantidade": it.get("quantidade"),
            "valor": it.get("valor"),
            "cfop": it.get("cfop"),
        })

    if nfe.get("tipo") not in (1, "1", None):
        reg["status"], reg["motivo"] = ST_SEM_NOTA, "A nota ligada ao pedido não é de saída."
    elif reg["nfe_situacao"] not in bling_client.NFE_AUTORIZADAS:
        situ = bling_client.NFE_SITUACOES.get(reg["nfe_situacao"], str(reg["nfe_situacao"]))
        reg["status"], reg["motivo"] = ST_SEM_NOTA, f"Nota de saída está '{situ}', não autorizada."
    elif not reg["nfe_chave"]:
        reg["status"], reg["motivo"] = ST_SEM_NOTA, "Nota de saída sem chave de acesso."
    else:
        reg["status"] = ST_PRONTO
    return reg, itens


# ------------------------------------------------------------------------ rotas

@bp.before_request
def _somente_liberados():
    if request.endpoint == "devolucoes.bling_callback":
        return None
    if session.get("employee_name") not in LIBERADOS:
        flash("A tela Devoluções é liberada só para JULIANA e LUCAS.")
        return redirect(url_for("dashboard"))
    return None


@bp.route("/devolucoes")
def painel():
    devs = _listar()
    resumo = {
        "total": len(devs),
        "prontos": sum(1 for d in devs if d["status"] == ST_PRONTO),
        "problemas": sum(1 for d in devs if d["status"] != ST_PRONTO),
    }
    return render_page(
        "Devoluções", "devolucoes", TEMPLATE,
        devs=devs, resumo=resumo, ic=ICONES,
        bling_configurado=bling_client.configurado(),
        bling_conectado=bling_client.conectado(),
        eh_lucas=session.get("employee_name") == "LUCAS",
    )


@bp.route("/devolucoes/bipar", methods=["POST"])
def bipar():
    rastreio = _normalizar(request.form.get("rastreio"))
    if not rastreio:
        return redirect(url_for("devolucoes.painel"))
    if not bling_client.conectado():
        flash("Conecte o Bling antes de bipar.")
        return redirect(url_for("devolucoes.painel"))
    if _ja_na_lista(rastreio, None):
        flash(f"{rastreio} já está na lista.")
        return redirect(url_for("devolucoes.painel"))

    reg, itens = consultar(rastreio)
    if reg.get("order_sn") and _ja_na_lista(rastreio, reg["order_sn"]):
        flash(f"O pedido {reg['order_sn']} já está na lista (bipado com outra etiqueta).")
        return redirect(url_for("devolucoes.painel"))
    reg["bipado_por"] = session.get("employee_name")
    _gravar(reg, itens)
    if reg["status"] == ST_PRONTO:
        flash(f"{rastreio}: pedido {reg['order_sn']}, nota {reg['nfe_numero']} encontrada.")
    else:
        flash(f"{rastreio}: {reg.get('motivo')}")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/remover", methods=["POST"])
def remover(dev_id):
    with models.get_conn() as conn:
        conn.execute(
            "UPDATE devolucoes SET removido = 1, updated_at = ? WHERE id = ? AND nfe_devolucao_id IS NULL",
            (_agora(), dev_id),
        )
    flash("Removido da lista.")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/reconsultar", methods=["POST"])
def reconsultar(dev_id):
    """Para quem deu erro ou não tinha nota: consulta de novo (ex.: a nota foi emitida depois)."""
    with models.get_conn() as conn:
        row = conn.execute("SELECT * FROM devolucoes WHERE id = ? AND removido = 0", (dev_id,)).fetchone()
    if not row or row["nfe_devolucao_id"]:
        return redirect(url_for("devolucoes.painel"))
    reg, itens = consultar(row["rastreio"])
    reg["bipado_por"] = row["bipado_por"]
    with models.get_conn() as conn:
        conn.execute("UPDATE devolucoes SET removido = 1 WHERE id = ?", (dev_id,))
    _gravar(reg, itens)
    flash(f"{row['rastreio']}: consultado de novo — {ROTULOS.get(reg['status'], (reg['status'],))[0]}.")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/bling/autorizar")
def bling_autorizar():
    if session.get("employee_name") != "LUCAS":
        flash("Só o LUCAS pode conectar o Bling.")
        return redirect(url_for("devolucoes.painel"))
    if not bling_client.configurado():
        flash("Faltam BLING_CLIENT_ID e BLING_CLIENT_SECRET no Render.")
        return redirect(url_for("devolucoes.painel"))
    state = secrets.token_urlsafe(16)
    session["bling_state"] = state
    return redirect(bling_client.url_autorizacao(state))


@bp.route("/bling/callback")
def bling_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != session.pop("bling_state", None):
        flash("Retorno do Bling inválido ou expirado. Tente 'Conectar Bling' de novo.")
        return redirect(url_for("devolucoes.painel"))
    try:
        bling_client.trocar_code(code)
    except Exception as e:
        flash(f"Não consegui conectar o Bling: {e}")
        return redirect(url_for("devolucoes.painel"))
    flash("Bling (conta Ana) conectado. Já pode bipar as devoluções.")
    return redirect(url_for("devolucoes.painel"))


TEMPLATE = """
<style>
.dv-scan{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.dv-scan input{flex:1;min-width:260px;font:inherit;font-size:18px;padding:12px 16px;border:2px solid #c7ccd6;border-radius:12px;background:#fff;letter-spacing:.5px}
.dv-scan input:focus{outline:none;border-color:#fdd32b}
.dv-tag{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.dv-tag.ok{background:#d9f3e6;color:#0d6b3f}
.dv-tag.warn{background:#fff1cc;color:#7a5300}
.dv-tag.bad{background:#fde0e0;color:#9b1c1c}
.dv-row{border-top:1px solid #e3e6ec;padding:14px 0;display:grid;grid-template-columns:150px 1fr auto;gap:14px;align-items:start}
.dv-row:first-of-type{border-top:0}
.dv-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.dv-sub{font-size:12px;color:#5b6474;margin-top:3px}
.dv-itens{margin:8px 0 0;padding:0;list-style:none;font-size:13px}
.dv-itens li{padding:2px 0}
.dv-acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.dv-wrap .v2-panel{margin-bottom:18px}
.dv-aviso{background:#fff;border-left:4px solid #fdd32b;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:14px}
@media (max-width:700px){.dv-row{grid-template-columns:1fr}.dv-acts{justify-content:flex-start}}
</style>
<div class="dv-wrap" style="max-width:1100px">
  <div class="v2-h">
    <div>
      <h1>Devoluções <span class="v2-badge">fase de teste: só consulta</span></h1>
      <div class="v2-sub">Bipe a etiqueta do pacote que voltou. O sistema acha o pedido e a nota de saída no Bling. Nenhuma nota é criada nesta fase.</div>
    </div>
  </div>

  {% if not bling_configurado %}
  <div class="dv-aviso">Faltam as variáveis <b>BLING_CLIENT_ID</b> e <b>BLING_CLIENT_SECRET</b> no Render.</div>
  {% elif not bling_conectado %}
  <div class="dv-aviso">O Bling (conta Ana) ainda não está conectado.
    {% if eh_lucas %}<a class="v2-btn" style="margin-left:10px" href="{{ url_for('devolucoes.bling_autorizar') }}">Conectar Bling</a>
    {% else %}Peça para o LUCAS conectar.{% endif %}
  </div>
  {% endif %}

  <div class="v2-panel">
    <form class="dv-scan" method="post" action="{{ url_for('devolucoes.bipar') }}">
      <input name="rastreio" placeholder="Bipe ou digite o rastreio da etiqueta" autocomplete="off" autofocus {% if not bling_conectado %}disabled{% endif %}>
      <button class="v2-btn" type="submit" {% if not bling_conectado %}disabled{% endif %}>Buscar</button>
    </form>
  </div>

  <div class="v2-cards">
    <div class="v2-card"><div><div class="l">Na lista</div><div class="n">{{ resumo.total }}</div></div></div>
    <div class="v2-card" style="border-color:#1baf7a"><div><div class="l">Prontos</div><div class="n">{{ resumo.prontos }}</div></div></div>
    <div class="v2-card" style="border-color:#eb6834"><div><div class="l">Com problema</div><div class="n">{{ resumo.problemas }}</div></div></div>
  </div>

  <div class="v2-panel">
    <h3>Devoluções a gerar</h3>
    {% if not devs %}<p class="v2-sub">Nenhum pacote bipado ainda.</p>{% endif %}
    {% for d in devs %}
    <div class="dv-row">
      <div>
        <div class="dv-mono"><b>{{ d.rastreio }}</b></div>
        <div class="dv-sub">{{ d.quando }} · {{ d.bipado_por or '' }}</div>
        <div style="margin-top:6px"><span class="dv-tag {{ d.tom }}">{{ d.rotulo }}</span></div>
      </div>
      <div>
        {% if d.order_sn %}<div><b>Pedido</b> <span class="dv-mono">{{ d.order_sn }}</span> <span class="dv-sub">({{ d.origem }})</span></div>{% endif %}
        {% if d.cliente_nome %}<div class="dv-sub">{{ d.cliente_nome }}{% if d.cliente_doc %} · {{ d.cliente_doc }}{% endif %}</div>{% endif %}
        {% if d.nfe_numero %}<div class="dv-sub">Nota de saída {{ d.nfe_numero }}{% if d.nfe_serie %}/{{ d.nfe_serie }}{% endif %}{% if d.total %} · R$ {{ '%.2f'|format(d.total)|replace('.', ',') }}{% endif %}</div>
        <div class="dv-sub dv-mono">{{ d.nfe_chave }}</div>{% endif %}
        {% if d.motivo %}<div class="dv-sub" style="color:#9b1c1c">{{ d.motivo }}</div>{% endif %}
        {% if d.itens %}<ul class="dv-itens">
          {% for it in d.itens %}<li>{{ (it.qtd_original or 0)|round(0)|int }} × {{ it.descricao }} <span class="dv-sub">{{ it.codigo }}</span></li>{% endfor %}
        </ul>{% endif %}
      </div>
      <div class="dv-acts">
        {% if d.status != 'pronto' %}
        <form method="post" action="{{ url_for('devolucoes.reconsultar', dev_id=d.id) }}"><button class="v2-btn sec" type="submit">Consultar de novo</button></form>
        {% endif %}
        <form method="post" action="{{ url_for('devolucoes.remover', dev_id=d.id) }}"><button class="v2-btn sec" type="submit">Remover</button></form>
      </div>
    </div>
    {% endfor %}
  </div>
</div>
"""


def init_app(app, get_shopee_client):
    _get_shopee[0] = get_shopee_client
    bling_client.init_db()
    init_db()
    app.register_blueprint(bp)
