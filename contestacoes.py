"""Contestações: acompanhamento das devoluções da Shopee que o Lucas contestou.

Bipa o rastreio da devolução (ou digita o número do pedido) e a devolução entra na lista.
O sistema consulta a Shopee sozinho de tempos em tempos e separa em filtros:
precisa da sua ação, aguardando a Shopee, compensação aprovada, recusada, encerrada.

Somente leitura na Shopee (get_return_detail). Não responde, não contesta e não aceita
nada por conta própria. Tabela própria (contestacoes), separada das notas de devolução.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, redirect, request, session, url_for

import models
from layout_v2 import ICONES, render_page

bp = Blueprint("contestacoes", __name__)

BRT = timezone(timedelta(hours=-3))
LIBERADOS = {"JULIANA", "LUCAS"}
INTERVALO_ATUALIZACAO = 3 * 3600   # consulta automática a cada 3 horas
PAUSA = 1.0                        # segundos entre consultas à Shopee (sem pressa)

_get_shopee = [None]
_buscar_devolucao = [None]         # do módulo devolucoes: rastreio/pedido -> return_sn
_lock = threading.Lock()

# ------------------------------------------------------------------ textos

STATUS_PT = {
    "REQUESTED": "Solicitada pelo cliente",
    "PROCESSING": "Em andamento",
    "ACCEPTED": "Aceita (cliente reembolsado)",
    "CANCELLED": "Cancelada",
    "JUDGING": "Em análise pela Shopee",
    "SELLER_DISPUTE": "Contestada por você — Shopee analisando",
    "CLOSED": "Encerrada",
}
COMPENSACAO_PT = {
    "": "—",
    "NOT_REQUIRED": "Não se aplica",
    "PENDING_REQUEST": "Você pode pedir compensação",
    "REQUEST_SUBMITTED": "Pedido de compensação enviado",
    "REQUEST_APPROVED": "Compensação aprovada",
    "REQUEST_REJECTED": "Compensação recusada",
}
PROVA_PT = {
    "": "—",
    "NOT_NEEDED": "Não pedida",
    "PENDING": "Shopee pediu prova",
    "REQUESTED": "Shopee pediu prova",
    "UPLOADED": "Prova enviada",
    "SUBMITTED": "Prova enviada",
}
MOTIVO_PT = {
    "DAMAGED_OTHERS": "Danificado",
    "DAMAGED": "Danificado",
    "WRONG_ITEM": "Produto errado",
    "WRONG_DAMAGED_ITEM": "Produto errado ou danificado",
    "ITEM_MISSING": "Faltando item",
    "MISSING_ITEM": "Faltando item",
    "NOT_RECEIPT": "Não recebeu",
    "NON_RECEIPT": "Não recebeu",
    "DIFFERENT_DESCRIPTION": "Diferente do anúncio",
    "FUNCTIONAL_DMG": "Com defeito",
    "ITEM_WRONGDAMAGED": "Produto errado ou danificado",
    "CHANGE_MIND": "Desistência",
    "EXPECTATION_FAILED": "Não atendeu a expectativa",
}
LOGISTICA_PT = {
    "LOGISTICS_DELIVERY_DONE": "Pacote de volta entregue a você",
    "LOGISTICS_REQUEST_CANCELED": "Envio de volta cancelado",
    "LOGISTICS_PICKUP_DONE": "Pacote coletado, a caminho",
    "LOGISTICS_READY": "Aguardando o cliente postar",
    "LOGISTICS_PENDING_ARRANGE": "Aguardando o cliente postar",
}

# Filtros da tela, na ordem em que aparecem.
FILTROS = [
    ("acao", "Precisa da sua ação", "bad"),
    ("aguardando", "Aguardando a Shopee", "warn"),
    ("aprovada", "Compensação aprovada", "ok"),
    ("recusada", "Recusada", "bad"),
    ("encerrada", "Encerrada", "neutro"),
]
FILTRO_NOME = {k: n for k, n, _ in FILTROS}
FILTRO_TOM = {k: t for k, _, t in FILTROS}


def classificar(c):
    """Em qual filtro a contestação fica, a partir do que a Shopee devolveu."""
    st = c.get("status") or ""
    comp = c.get("compensacao_status") or ""
    prova = c.get("prova_status") or ""
    if comp == "REQUEST_APPROVED":
        return "aprovada"
    if comp == "REQUEST_REJECTED":
        return "recusada"
    if prova in ("PENDING", "REQUESTED") or comp == "PENDING_REQUEST":
        return "acao"
    if c.get("negociacao_status") == "PENDING_RESPOND":
        return "acao"
    if st in ("JUDGING", "SELLER_DISPUTE", "PROCESSING", "REQUESTED") or comp == "REQUEST_SUBMITTED":
        return "aguardando"
    if st == "ACCEPTED" and comp in ("", "NOT_REQUIRED"):
        return "recusada"   # cliente ficou com o reembolso e não houve compensação
    return "encerrada"


# ------------------------------------------------------------------ banco

def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contestacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_sn TEXT UNIQUE NOT NULL,
                order_sn TEXT,
                rastreio TEXT,
                status TEXT,
                motivo TEXT,
                texto_cliente TEXT,
                texto_contestacao TEXT,
                valor REAL,
                compensacao_status TEXT,
                compensacao_valor REAL,
                prova_status TEXT,
                prova_prazo INTEGER,
                prazo_vendedor INTEGER,
                negociacao_status TEXT,
                logistica_status TEXT,
                produtos TEXT,
                filtro TEXT,
                anotacao TEXT,
                adicionado_por TEXT,
                criado_em TEXT,
                consultado_em TEXT,
                mudou_em TEXT,
                erro TEXT,
                removido INTEGER DEFAULT 0
            )
            """
        )


def _agora():
    return datetime.utcnow().isoformat()


def _quando(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), BRT).strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _iso_quando(iso):
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone(BRT).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------------ Shopee

def _detalhe(client, return_sn):
    r = client._shop_request("GET", "/api/v2/returns/get_return_detail", params={"return_sn": return_sn})
    if r.get("error"):
        raise RuntimeError(r.get("message") or r.get("error"))
    return r.get("response") or {}


def _campos(d):
    """Converte o detalhe da Shopee nas colunas da tabela (sem dados pessoais do cliente)."""
    comp = d.get("seller_compensation") or {}
    prova = d.get("seller_proof") or {}
    neg = d.get("negotiation") or {}
    disputa = d.get("dispute_text_reason") or d.get("dispute_reason") or []
    if isinstance(disputa, list):
        disputa = " · ".join(str(x) for x in disputa if x)
    produtos = []
    for it in d.get("item") or []:
        nome = it.get("name") or it.get("item_name") or ""
        var = it.get("variation_name") or it.get("model_name") or ""
        qtd = it.get("amount") or it.get("quantity") or 1
        if nome:
            produtos.append(f"{qtd} × {nome}" + (f" ({var})" if var else ""))
    c = {
        "order_sn": d.get("order_sn"),
        "rastreio": (d.get("tracking_number") or "").upper() or None,
        "status": d.get("status") or "",
        "motivo": d.get("reason") or "",
        "texto_cliente": d.get("text_reason") or "",
        "texto_contestacao": disputa or "",
        "valor": d.get("refund_amount"),
        "compensacao_status": comp.get("seller_compensation_status") or "",
        "compensacao_valor": comp.get("compensation_amount") or 0,
        "prova_status": prova.get("seller_proof_status") or "",
        "prova_prazo": prova.get("seller_evidence_deadline") or None,
        "prazo_vendedor": d.get("return_seller_due_date") or None,
        "negociacao_status": neg.get("negotiation_status") or "",
        "logistica_status": d.get("reverse_logistics_status") or "",
        "produtos": json.dumps(produtos, ensure_ascii=False),
    }
    c["filtro"] = classificar(c)
    return c


def _gravar(return_sn, campos, adicionado_por=None):
    agora = _agora()
    with models.get_conn() as conn:
        antes = conn.execute(
            "SELECT filtro, status, compensacao_status, prova_status FROM contestacoes WHERE return_sn = ?",
            (return_sn,),
        ).fetchone()
        mudou = (not antes) or any(
            (antes[k] or "") != (campos.get(k) or "") for k in ("filtro", "status", "compensacao_status", "prova_status")
        )
        if antes:
            sets = ", ".join(f"{k} = ?" for k in campos)
            conn.execute(
                f"UPDATE contestacoes SET {sets}, consultado_em = ?, erro = NULL, removido = 0"
                + (", mudou_em = ?" if mudou else "")
                + " WHERE return_sn = ?",
                (*campos.values(), agora, *([agora] if mudou else []), return_sn),
            )
        else:
            cols = ["return_sn", *campos.keys(), "adicionado_por", "criado_em", "consultado_em", "mudou_em"]
            conn.execute(
                f"INSERT INTO contestacoes ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (return_sn, *campos.values(), adicionado_por, agora, agora, agora),
            )


def _escolher_return_sn(client, candidatos):
    """Um pedido pode ter mais de uma devolução (ex.: uma cancelada e outra ativa).
    Fica com a que não está cancelada; se todas estiverem, a mais recente."""
    detalhes = []
    for rs in candidatos:
        try:
            detalhes.append((rs, _detalhe(client, rs)))
        except Exception:
            continue
        time.sleep(0.3)
    if not detalhes:
        return None, None
    ativos = [x for x in detalhes if (x[1].get("status") or "") != "CANCELLED"]
    escolha = max(ativos or detalhes, key=lambda x: x[1].get("update_time") or 0)
    return escolha


def adicionar(texto, quem):
    """Recebe rastreio da devolução ou número do pedido e põe na lista. Devolve mensagem."""
    texto = (texto or "").strip().upper().replace(" ", "")
    if not texto:
        return "Digite ou bipe o rastreio da devolução."
    client = _get_shopee[0]() if _get_shopee[0] else None
    if client is None:
        return "Loja Shopee não conectada."
    candidatos = _buscar_devolucao[0](texto) if _buscar_devolucao[0] else []
    if not candidatos:
        return (f"{texto}: não achei devolução na Shopee com esse rastreio ou pedido. "
                "Confira se é o rastreio da DEVOLUÇÃO (não o de ida) e se é da loja Ana Baby.")
    rs, det = _escolher_return_sn(client, candidatos)
    if not rs:
        return f"{texto}: a Shopee não respondeu agora. Tente de novo em instantes."
    _gravar(rs, _campos(det), quem)
    c = _campos(det)
    return f"{texto}: adicionada — {FILTRO_NOME.get(c['filtro'], c['filtro'])} ({STATUS_PT.get(c['status'], c['status'])})."


def atualizar_todas():
    """Consulta de novo na Shopee tudo que ainda não está encerrado."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        client = _get_shopee[0]() if _get_shopee[0] else None
        if client is None:
            return False
        with models.get_conn() as conn:
            rows = [r["return_sn"] for r in conn.execute(
                "SELECT return_sn FROM contestacoes WHERE removido = 0 "
                "AND (filtro <> 'encerrada' OR consultado_em < ?)",
                ((datetime.utcnow() - timedelta(days=3)).isoformat(),),
            ).fetchall()]
        for rs in rows:
            try:
                _gravar(rs, _campos(_detalhe(client, rs)))
            except Exception as e:
                with models.get_conn() as conn:
                    conn.execute("UPDATE contestacoes SET erro = ? WHERE return_sn = ?", (str(e)[:200], rs))
            time.sleep(PAUSA)
        with models.get_conn() as conn:
            conn.execute(
                "INSERT INTO devolucoes_meta (chave, valor) VALUES ('contest_atualizado', ?) "
                "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                (_agora(),),
            )
        return True
    finally:
        _lock.release()


def _loop():
    time.sleep(120)   # deixa o servidor subir antes
    while True:
        try:
            atualizar_todas()
        except Exception:
            pass
        time.sleep(INTERVALO_ATUALIZACAO)


# ------------------------------------------------------------------ tela

def _liberado():
    if not session.get("employee_name"):
        return redirect(url_for("login"))
    if session.get("employee_name") not in LIBERADOS:
        flash("A tela Contestações é liberada só para JULIANA e LUCAS.")
        return redirect(url_for("dashboard"))
    return None


def _listar(filtro):
    with models.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM contestacoes WHERE removido = 0 ORDER BY mudou_em DESC, id DESC LIMIT 500"
        ).fetchall()]
        meta = conn.execute("SELECT valor FROM devolucoes_meta WHERE chave = 'contest_atualizado'").fetchone()
    contagem = {k: 0 for k, _, _ in FILTROS}
    for c in rows:
        contagem[c["filtro"]] = contagem.get(c["filtro"], 0) + 1
        c["status_pt"] = STATUS_PT.get(c["status"], c["status"] or "—")
        c["motivo_pt"] = MOTIVO_PT.get(c["motivo"], (c["motivo"] or "").replace("_", " ").capitalize())
        c["comp_pt"] = COMPENSACAO_PT.get(c["compensacao_status"] or "", c["compensacao_status"])
        c["prova_pt"] = PROVA_PT.get(c["prova_status"] or "", c["prova_status"])
        c["log_pt"] = LOGISTICA_PT.get(c["logistica_status"] or "", (c["logistica_status"] or "").replace("LOGISTICS_", "").replace("_", " ").lower())
        c["filtro_nome"] = FILTRO_NOME.get(c["filtro"], c["filtro"])
        c["tom"] = FILTRO_TOM.get(c["filtro"], "neutro")
        c["prazo"] = _quando(c["prova_prazo"] or (c["prazo_vendedor"] if c["filtro"] == "acao" else None))
        prazo_ts = c["prova_prazo"] or (c["prazo_vendedor"] if c["filtro"] == "acao" else None)
        c["prazo_perto"] = bool(prazo_ts and int(prazo_ts) - time.time() < 2 * 86400)
        c["mudou"] = _iso_quando(c["mudou_em"])
        c["consultado"] = _iso_quando(c["consultado_em"])
        try:
            c["produtos_lista"] = json.loads(c["produtos"] or "[]")
        except ValueError:
            c["produtos_lista"] = []
    if filtro in contagem:
        rows = [c for c in rows if c["filtro"] == filtro]
    return rows, contagem, _iso_quando(meta["valor"]) if meta else ""


TEMPLATE = """
<style>
.ct-scan{display:flex;gap:10px;flex-wrap:wrap}
.ct-scan input{flex:1;min-width:260px;font:inherit;font-size:18px;padding:12px 16px;border:2px solid #c7ccd6;border-radius:12px;background:#fff}
.ct-scan input:focus{outline:none;border-color:#fdd32b}
.ct-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.ct-tabs a{padding:8px 14px;border-radius:999px;background:#fff;border:1px solid #d5d9e1;color:#1f2633;text-decoration:none;font-size:14px}
.ct-tabs a.on{background:#1f2633;color:#fff;border-color:#1f2633}
.ct-tabs b{margin-left:6px}
.ct-row{border-top:1px solid #e3e6ec;padding:14px 0;display:grid;grid-template-columns:200px 1fr 220px;gap:16px;align-items:start}
.ct-row:first-of-type{border-top:0}
.ct-tag{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.ct-tag.ok{background:#d9f3e6;color:#0d6b3f}.ct-tag.warn{background:#fff1cc;color:#7a5300}
.ct-tag.bad{background:#fde0e0;color:#9b1c1c}.ct-tag.neutro{background:#eceff3;color:#4a5262}
.ct-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.ct-sub{font-size:12px;color:#5b6474;margin-top:3px}
.ct-q{font-size:13px;margin-top:6px;padding:6px 10px;background:#f6f7f9;border-radius:8px}
.ct-prazo{color:#9b1c1c;font-weight:700}
.ct-nota{width:100%;font:inherit;font-size:13px;padding:6px 8px;border:1px solid #c7ccd6;border-radius:8px;min-height:54px}
@media(max-width:800px){.ct-row{grid-template-columns:1fr}}
</style>
<div style="max-width:1100px">
  <div class="v2-h"><div>
    <h1>Contestações</h1>
    <div class="v2-sub">Bipe o rastreio da devolução (ou digite o número do pedido). O sistema consulta a Shopee sozinho a cada 3 horas{% if atualizado %} · última consulta {{ atualizado }}{% endif %}.</div>
  </div>
  <form method="post" action="{{ url_for('contestacoes.atualizar') }}"><button class="v2-btn sec" type="submit">{{ ic.recarregar|safe }} Atualizar agora</button></form>
  </div>
  <div class="v2-panel">
    <form class="ct-scan" method="post" action="{{ url_for('contestacoes.adicionar_rota') }}">
      <input name="codigo" placeholder="Rastreio da devolução ou número do pedido" autocomplete="off" autofocus>
      <button class="v2-btn" type="submit">Acompanhar</button>
    </form>
  </div>
  <div class="ct-tabs">
    <a href="{{ url_for('contestacoes.painel') }}" class="{% if not filtro %}on{% endif %}">Todas<b>{{ total }}</b></a>
    {% for k, nome, tom in filtros %}
    <a href="{{ url_for('contestacoes.painel', f=k) }}" class="{% if filtro == k %}on{% endif %}">{{ nome }}<b>{{ contagem[k] }}</b></a>
    {% endfor %}
  </div>
  <div class="v2-panel">
    {% if not itens %}<p class="v2-sub">Nada aqui{% if filtro %} neste filtro{% endif %}.</p>{% endif %}
    {% for c in itens %}
    <div class="ct-row">
      <div>
        <div class="ct-mono"><b>{{ c.rastreio or '—' }}</b></div>
        <div class="ct-sub">Pedido <span class="ct-mono">{{ c.order_sn }}</span></div>
        <div style="margin-top:6px"><span class="ct-tag {{ c.tom }}">{{ c.filtro_nome }}</span></div>
        <div class="ct-sub">mudou em {{ c.mudou }}</div>
      </div>
      <div>
        <div><b>{{ c.status_pt }}</b>{% if c.valor %} · R$ {{ '%.2f'|format(c.valor)|replace('.', ',') }}{% endif %}</div>
        {% for p in c.produtos_lista %}<div class="ct-sub">{{ p }}</div>{% endfor %}
        <div class="ct-q"><b>Cliente ({{ c.motivo_pt }}):</b> {{ c.texto_cliente or '—' }}</div>
        {% if c.texto_contestacao %}<div class="ct-q"><b>Sua contestação:</b> {{ c.texto_contestacao }}</div>{% endif %}
        <div class="ct-sub">Compensação: <b>{{ c.comp_pt }}</b>{% if c.compensacao_valor %} (R$ {{ '%.2f'|format(c.compensacao_valor)|replace('.', ',') }}){% endif %} · Prova: {{ c.prova_pt }} · {{ c.log_pt }}</div>
        {% if c.prazo %}<div class="ct-sub {% if c.prazo_perto %}ct-prazo{% endif %}">Prazo para você: {{ c.prazo }}</div>{% endif %}
        {% if c.erro %}<div class="ct-sub" style="color:#9b1c1c">Última consulta falhou: {{ c.erro }}</div>{% endif %}
      </div>
      <div>
        <form method="post" action="{{ url_for('contestacoes.anotar', cid=c.id) }}">
          <textarea class="ct-nota" name="anotacao" placeholder="Anotação (ex.: mandei foto do produto)">{{ c.anotacao or '' }}</textarea>
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="v2-btn sec" type="submit" style="padding:4px 10px">Salvar nota</button>
            <button class="v2-btn sec" type="submit" formaction="{{ url_for('contestacoes.remover', cid=c.id) }}" style="padding:4px 10px" onclick="return confirm('Tirar da lista de acompanhamento?')">Tirar da lista</button>
          </div>
        </form>
        <div class="ct-sub">consultado {{ c.consultado }}</div>
      </div>
    </div>
    {% endfor %}
  </div>
</div>
"""


@bp.route("/contestacoes")
def painel():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    filtro = request.args.get("f") or ""
    itens, contagem, atualizado = _listar(filtro)
    return render_page(
        "Contestações", "contestacoes", TEMPLATE,
        itens=itens, contagem=contagem, total=sum(contagem.values()), filtros=FILTROS,
        filtro=filtro if filtro in contagem else "", atualizado=atualizado, ic=ICONES,
    )


@bp.route("/contestacoes/adicionar", methods=["POST"])
def adicionar_rota():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    try:
        flash(adicionar(request.form.get("codigo"), session.get("employee_name")))
    except Exception as e:
        flash(f"Não consegui consultar a Shopee agora: {e}")
    return redirect(url_for("contestacoes.painel"))


@bp.route("/contestacoes/atualizar", methods=["POST"])
def atualizar():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    if _lock.locked():
        flash("Já está atualizando. Recarregue a página em alguns instantes.")
    else:
        threading.Thread(target=atualizar_todas, daemon=True).start()
        flash("Atualizando em segundo plano. Recarregue a página em 1 ou 2 minutos.")
    return redirect(url_for("contestacoes.painel"))


@bp.route("/contestacoes/<int:cid>/anotar", methods=["POST"])
def anotar(cid):
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    with models.get_conn() as conn:
        conn.execute("UPDATE contestacoes SET anotacao = ? WHERE id = ?",
                     ((request.form.get("anotacao") or "").strip()[:1000], cid))
    flash("Anotação salva.")
    return redirect(request.referrer or url_for("contestacoes.painel"))


@bp.route("/contestacoes/<int:cid>/remover", methods=["POST"])
def remover(cid):
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    with models.get_conn() as conn:
        conn.execute("UPDATE contestacoes SET removido = 1 WHERE id = ?", (cid,))
    flash("Tirada da lista.")
    return redirect(request.referrer or url_for("contestacoes.painel"))


def init_app(app, get_shopee_client, buscar_devolucao):
    _get_shopee[0] = get_shopee_client
    _buscar_devolucao[0] = buscar_devolucao
    init_db()
    app.register_blueprint(bp)
    threading.Thread(target=_loop, daemon=True).start()
