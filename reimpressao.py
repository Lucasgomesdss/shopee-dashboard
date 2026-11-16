"""Reimpressão de etiquetas (ZPL) dos pedidos da tela "A separar".

Serve para quando o arquivo liberado pela Shopee vem com etiquetas a menos. Pega a
etiqueta que a Shopee JÁ gerou para o pedido (não faz ship_order, não muda status de
nada) e devolve um .txt com o ZPL dos pedidos marcados.
"""

import io
import json
import logging
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, flash, jsonify, redirect, request, session, url_for

import models
from layout_v2 import ICONES, render_page

log = logging.getLogger(__name__)

bp = Blueprint("reimpressao", __name__)

BRT = timezone(timedelta(hours=-3))
MAX_PEDIDOS = 300      # trava de segurança por download
LOTE = 50              # limite da Shopee por chamada
ESPERA_MAX = 45        # segundos esperando a Shopee preparar a etiqueta
_get_client = [None]
_trava_inicio = threading.Lock()


def _post(client, path, body, timeout=60):
    ts = int(time.time())
    sign = client._sign(path, ts, f"{client.access_token}{client.shop_id}")
    query = {
        "partner_id": client.partner_id,
        "timestamp": ts,
        "sign": sign,
        "shop_id": client.shop_id,
        "access_token": client.access_token,
    }
    return client.session.post(client.host + path, params=query, json=body, timeout=timeout)


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {"error": "resposta_invalida", "message": resp.text[:200]}


def _extrair_texto(conteudo):
    """O download pode vir como ZIP (um arquivo por etiqueta) ou como texto direto."""
    if conteudo[:2] == b"PK":
        partes = []
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            for nome in sorted(z.namelist()):
                if nome.endswith("/"):
                    continue
                partes.append(z.read(nome).decode("utf-8", errors="replace").strip())
        return "\n".join(p for p in partes if p)
    return conteudo.decode("utf-8", errors="replace").strip()


def _entrada(sn, pacote, rastreio):
    e = {"order_sn": sn}
    if pacote:
        e["package_number"] = pacote
    if rastreio:
        e["tracking_number"] = rastreio
    return e


def _rastreio_shopee(client, sn):
    """Rastreio completo direto da Shopee (inclui o sufixo SPXLM dos pedidos de retirada)."""
    try:
        r = client._shop_request("GET", "/api/v2/logistics/get_tracking_number", params={"order_sn": sn})
        return ((r.get("response") or {}).get("tracking_number") or "").strip()
    except Exception:
        return ""


def _lote(client, sns, falhas, rastreios_frescos=None):
    """Devolve o texto ZPL dos pedidos do lote. Pedidos com problema vão para `falhas`.

    Rastreio usado para pedir a etiqueta, nesta ordem:
      1) `rastreios_frescos` (lido da Shopee logo depois de organizar o envio);
      2) o que vier no get_order_detail;
      3) get_tracking_number na hora.
    Nunca usa o rastreio do banco local: lá ele fica cortado de propósito (sem o
    sufixo SPXLM dos pedidos de retirada, para a bipagem funcionar) e pode estar
    vazio logo depois de organizar — a Shopee recusa os dois casos."""
    rastreios_frescos = rastreios_frescos or {}
    det = client.get_order_detail(sns).get("response", {}).get("order_list", [])
    pacotes = {}
    rastreios = {}
    canais = {}
    for od in det:
        pkg = (od.get("package_list") or [{}])[0]
        pacotes[od["order_sn"]] = pkg.get("package_number")
        # A Shopee não baixa junto etiquetas de canais diferentes (ex.: retirada x entrega).
        canais[od["order_sn"]] = pkg.get("logistics_channel_id") or od.get("shipping_carrier") or ""
        rastreios[od["order_sn"]] = (pkg.get("tracking_number") or "").strip()
    for sn in sns:
        if rastreios_frescos.get(sn):
            rastreios[sn] = rastreios_frescos[sn]
        elif sn in pacotes and not rastreios.get(sn):
            rastreios[sn] = _rastreio_shopee(client, sn)
            time.sleep(0.2)
    for sn in sns:
        if sn not in pacotes:
            falhas.append((sn, "pedido não encontrado na Shopee"))
    base = [_entrada(sn, pk, rastreios.get(sn)) for sn, pk in pacotes.items()]
    if not base:
        return ""

    # Tipo de documento: só aceitamos térmico (ZPL).
    par = _json(_post(client, "/api/v2/logistics/get_shipping_document_parameter", {"order_list": base}))
    por_tipo = {}
    if par.get("error") and not (par.get("response") or {}).get("result_list"):
        for e in base:
            falhas.append((e["order_sn"], "parâmetro: " + str(par.get("message") or par.get("error"))))
        return ""
    for r in (par.get("response") or {}).get("result_list", []):
        sn = r.get("order_sn")
        if r.get("fail_error") or r.get("fail_message"):
            falhas.append((sn, "parâmetro: " + str(r.get("fail_message") or r.get("fail_error"))))
            continue
        sel = r.get("selectable_shipping_document_type") or []
        tipo = "THERMAL_AIR_WAYBILL" if "THERMAL_AIR_WAYBILL" in sel else None
        if not tipo:
            sug = r.get("suggest_shipping_document_type") or ""
            tipo = sug if sug.startswith("THERMAL") else next((t for t in sel if t.startswith("THERMAL")), None)
        if not tipo:
            falhas.append((sn, "a Shopee não oferece etiqueta térmica (ZPL) para este pedido"))
            continue
        por_tipo.setdefault(tipo, []).append(_entrada(sn, pacotes.get(sn), rastreios.get(sn)))

    saida = []
    for tipo, ordens in por_tipo.items():
        com_tipo = [dict(o, shipping_document_type=tipo) for o in ordens]
        for o in com_tipo:
            log.warning("etiqueta: pedindo %s rastreio=%s pacote=%s tipo=%s",
                        o["order_sn"], o.get("tracking_number") or "(nenhum)", o.get("package_number") or "-", tipo)
        cri = _json(_post(client, "/api/v2/logistics/create_shipping_document", {"order_list": com_tipo}))
        ok = []
        falhou = {r.get("order_sn"): r for r in (cri.get("response") or {}).get("result_list", []) if r.get("fail_error")}
        if cri.get("error") or falhou:
            log.warning("etiqueta: create_shipping_document recusou: erro=%s msg=%s falhas=%s",
                        cri.get("error"), cri.get("message"),
                        {k: (v.get("fail_error"), v.get("fail_message")) for k, v in falhou.items()})
        if cri.get("error") and not (cri.get("response") or {}).get("result_list"):
            for o in ordens:
                falhas.append((o["order_sn"], "criar: " + str(cri.get("message") or cri.get("error"))))
            continue
        for o in ordens:
            if o["order_sn"] in falhou:
                falhas.append((o["order_sn"], "criar: " + str(falhou[o["order_sn"]].get("fail_message") or falhou[o["order_sn"]].get("fail_error"))))
            else:
                ok.append(o)
        if not ok:
            continue

        # espera a Shopee deixar as etiquetas prontas
        prontos = []
        ini = time.time()
        pendentes = list(ok)
        while pendentes and time.time() - ini < ESPERA_MAX:
            res = _json(_post(client, "/api/v2/logistics/get_shipping_document_result",
                              {"order_list": [dict(o, shipping_document_type=tipo) for o in pendentes]}))
            estado = {r.get("order_sn"): r for r in (res.get("response") or {}).get("result_list", [])}
            novos = []
            for o in pendentes:
                st = (estado.get(o["order_sn"]) or {}).get("status")
                if st == "READY":
                    prontos.append(o)
                elif st == "FAILED":
                    falhas.append((o["order_sn"], (estado[o["order_sn"]].get("fail_message") or "falha ao preparar a etiqueta")))
                else:
                    novos.append(o)
            pendentes = novos
            if pendentes:
                time.sleep(2)
        for o in pendentes:
            falhas.append((o["order_sn"], "a Shopee demorou para preparar a etiqueta"))
        if not prontos:
            continue

        grupos = {}
        for o in prontos:
            grupos.setdefault(canais.get(o["order_sn"], ""), []).append(o)
        for grupo in grupos.values():
            saida.extend(_baixar(client, tipo, grupo, falhas))
    return "\n".join(s for s in saida if s)


def _baixar(client, tipo, ordens, falhas):
    """Baixa o ZPL de um grupo do mesmo canal. Se a Shopee ainda recusar baixar junto,
    tenta um pedido por vez para não perder o grupo inteiro."""
    for _ in range(12):
        resp = _post(client, "/api/v2/logistics/download_shipping_document",
                     {"shipping_document_type": tipo, "order_list": ordens}, timeout=120)
        tipo_resp = resp.headers.get("Content-Type", "")
        if "json" in tipo_resp and "rocessing" in resp.text:
            time.sleep(5)  # a Shopee ainda está montando o arquivo
            continue
        break
    if "json" not in tipo_resp:
        return [_extrair_texto(resp.content)]
    erro = _json(resp)
    msg = str(erro.get("message") or erro.get("error") or "falha no download")
    if len(ordens) > 1 and "together" in msg:
        log.warning("etiqueta: canais misturados, baixando um por vez (%d pedidos)", len(ordens))
        saida = []
        for o in ordens:
            saida.extend(_baixar(client, tipo, [o], falhas))
        return saida
    for o in ordens:
        falhas.append((o["order_sn"], "baixar: " + msg))
    return []


def baixar_varios(client, sns, falhas, rastreios_frescos=None, aviso=None):
    """ZPL de muitos pedidos: divide em levas de 50 e pede as levas à Shopee ao mesmo
    tempo (antes era uma depois da outra e 132 pedidos levavam ~3,5 min). A ordem do
    arquivo final segue a ordem de `sns`. Uma leva com erro não derruba as outras."""
    levas = [sns[i:i + LOTE] for i in range(0, len(sns), LOTE)]
    resultado = [None] * len(levas)
    prontas = [0]
    trava = threading.Lock()

    def uma(i):
        f = []
        try:
            t = _lote(client, levas[i], f, rastreios_frescos)
        except Exception as e:  # rede/Shopee: marca a leva inteira e segue
            log.warning("etiqueta: leva %s falhou: %s", i + 1, e)
            t = ""
            f = [(sn, "erro ao falar com a Shopee: %s" % e) for sn in levas[i]]
        resultado[i] = (t, f)
        with trava:
            prontas[0] += len(levas[i])
            if aviso:
                try:
                    aviso(prontas[0], len(sns))
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=min(3, len(levas) or 1)) as ex:
        list(ex.map(uma, range(len(levas))))
    textos = []
    for t, f in resultado:
        falhas.extend(f)
        if t:
            textos.append(t)
    return "\n".join(textos)


# ------------------------------------------------------------------ trabalho em segundo plano

BATIMENTO_MORTO = 240   # sem sinal de vida há mais que isso = servidor reiniciou no meio


def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reimpressao_trabalhos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,          -- rodando, concluido, erro
                pedidos TEXT,                  -- json: order_sn na ordem marcada
                total INTEGER DEFAULT 0,
                prontos INTEGER DEFAULT 0,
                qtd INTEGER DEFAULT 0,
                falhas TEXT DEFAULT '[]',
                etapa TEXT,
                nome TEXT,
                conteudo TEXT,
                iniciado_por TEXT,
                criado_em TEXT,
                batimento TEXT,
                fim_em TEXT
            )
            """
        )


def _agora():
    return datetime.utcnow().isoformat()


def _upd(tid, **campos):
    campos["batimento"] = _agora()
    if "falhas" in campos and not isinstance(campos["falhas"], str):
        campos["falhas"] = json.dumps(campos["falhas"], ensure_ascii=False)
    sets = ", ".join(f"{k} = ?" for k in campos)
    with models.get_conn() as conn:
        conn.execute(f"UPDATE reimpressao_trabalhos SET {sets} WHERE id = ?", (*campos.values(), tid))


def _ativo():
    limite = (datetime.utcnow() - timedelta(seconds=BATIMENTO_MORTO)).isoformat()
    with models.get_conn() as conn:
        conn.execute(
            "UPDATE reimpressao_trabalhos SET status = 'erro', etapa = 'Parou no meio (o servidor reiniciou). Tente de novo.' "
            "WHERE status = 'rodando' AND batimento < ?", (limite,),
        )
        row = conn.execute(
            "SELECT id FROM reimpressao_trabalhos WHERE status = 'rodando' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def _rodar(tid, sns):
    falhas = []
    try:
        client = _get_client[0]()
        if client is None:
            raise RuntimeError("Loja ainda não autorizada na Shopee.")
        _upd(tid, etapa="Pedindo as etiquetas à Shopee")

        def aviso(feitos, total):
            _upd(tid, prontos=feitos, etapa=f"Preparando as etiquetas na Shopee ({feitos} de {total})")

        texto = baixar_varios(client, sns, falhas, aviso=aviso)
        vistos = set()
        falhas = [list(f) for f in falhas if not (f[0] in vistos or vistos.add(f[0]))]
        qtd = len(sns) - len(falhas)
        if not texto:
            _upd(tid, status="erro", etapa="Nenhuma etiqueta saiu", qtd=0, falhas=falhas, fim_em=_agora())
            return
        nome = "ETIQUETAS REIMPRESSAO %d %s.txt" % (qtd, datetime.now(BRT).strftime("%d-%m %H%M"))
        _upd(tid, status="concluido", etapa="Pronto", qtd=qtd, prontos=len(sns), falhas=falhas,
             nome=nome, conteudo=texto + "\n", fim_em=_agora())
    except Exception as e:
        log.warning("etiqueta: reimpressão %s parou: %s", tid, e)
        _upd(tid, status="erro", etapa=f"Parou: {e}", falhas=falhas, fim_em=_agora())


def _trabalho(tid):
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT id, status, total, prontos, qtd, falhas, etapa, nome, iniciado_por, criado_em, fim_em "
            "FROM reimpressao_trabalhos WHERE id = ?", (tid,),
        ).fetchone()
    if not row:
        return None
    t = dict(row)
    t["falhas_l"] = json.loads(t["falhas"] or "[]")
    try:
        t["quando"] = datetime.fromisoformat(t["criado_em"]).replace(tzinfo=timezone.utc).astimezone(BRT).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        t["quando"] = ""
    return t


def ultima_reimpressao():
    """Para a tela A separar: a reimpressão mais recente das últimas 12 horas."""
    try:
        _ativo()
        limite = (datetime.utcnow() - timedelta(hours=12)).isoformat()
        with models.get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM reimpressao_trabalhos WHERE criado_em >= ? ORDER BY id DESC LIMIT 1", (limite,)
            ).fetchone()
        return _trabalho(row["id"]) if row else None
    except Exception:
        return None


@bp.route("/etiquetas/reimprimir", methods=["POST"])
def reimprimir():
    sns = [s.strip() for s in request.form.getlist("sn") if s.strip()]
    sns = list(dict.fromkeys(sns))
    if not sns:
        flash("Marque pelo menos um pedido para baixar a etiqueta.")
        return redirect(url_for("open_tab"))
    if len(sns) > MAX_PEDIDOS:
        flash(f"Marque no máximo {MAX_PEDIDOS} pedidos por vez.")
        return redirect(url_for("open_tab"))
    if not re.fullmatch(r"[A-Za-z0-9]+", "".join(sns)):
        flash("Pedido inválido na seleção.")
        return redirect(url_for("open_tab"))

    # Só pedidos que estão na fila de separação (a separar / pendente) deste sistema.
    with models.get_conn() as conn:
        marks = ",".join("?" for _ in sns)
        validos = {
            r["order_sn"]
            for r in conn.execute(
                f"SELECT order_sn FROM orders WHERE order_sn IN ({marks}) AND status IN ('to_separate','pending')",
                sns,
            ).fetchall()
        }
    sns = [s for s in sns if s in validos]
    if not sns:
        flash("Os pedidos marcados não estão mais em A separar.")
        return redirect(url_for("open_tab"))
    if _get_client[0]() is None:
        flash("Loja ainda não autorizada na Shopee.")
        return redirect(url_for("open_tab"))

    with _trava_inicio:
        ativo = _ativo()
        if ativo:
            flash("Já tem uma reimpressão em andamento — acompanhe aqui. Quando terminar, faça a próxima.")
            return redirect(url_for("reimpressao.acompanhar", tid=ativo))
        with models.get_conn() as conn:
            tid = conn.execute(
                "INSERT INTO reimpressao_trabalhos (status, pedidos, total, etapa, iniciado_por, criado_em, batimento) "
                "VALUES ('rodando', ?, ?, 'Começando', ?, ?, ?)",
                (json.dumps(sns), len(sns), session.get("employee_name") or "", _agora(), _agora()),
            ).lastrowid
    db = models.caminho_db()   # Ana Baby ou Utility: o trabalho roda no banco da loja da tela

    def _rodar_na_loja():
        with models.usar_db(db):
            _rodar(tid, sns)

    threading.Thread(target=_rodar_na_loja, daemon=True).start()
    return redirect(url_for("reimpressao.acompanhar", tid=tid))


@bp.route("/etiquetas/reimpressao/<int:tid>")
def acompanhar(tid):
    _ativo()
    t = _trabalho(tid)
    if not t:
        flash("Reimpressão não encontrada.")
        return redirect(url_for("open_tab"))
    return render_page("Reimpressão de etiquetas", "", TEMPLATE, t=t, ic=ICONES)


@bp.route("/etiquetas/reimpressao/<int:tid>/estado")
def estado(tid):
    _ativo()
    t = _trabalho(tid)
    if not t:
        return jsonify({"status": "sumiu"}), 404
    return jsonify({k: t[k] for k in ("status", "total", "prontos", "qtd", "etapa")})


@bp.route("/etiquetas/reimpressao/<int:tid>/arquivo")
def arquivo(tid):
    with models.get_conn() as conn:
        row = conn.execute("SELECT nome, conteudo FROM reimpressao_trabalhos WHERE id = ?", (tid,)).fetchone()
    if not row or not row["conteudo"]:
        flash("O arquivo desta reimpressão não está disponível.")
        return redirect(url_for("reimpressao.acompanhar", tid=tid))
    return Response(
        row["conteudo"],
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % (row["nome"] or "ETIQUETAS.txt").replace('"', "")},
    )


TEMPLATE = """
<div style="max-width:760px">
<div class="v2-h"><div><h1>Reimpressão de etiquetas</h1>
<div class="v2-sub">Pedida {{ t.quando }}{% if t.iniciado_por %} por {{ t.iniciado_por }}{% endif %} · {{ t.total }} pedido(s)</div></div>
<a class="v2-btn sec" href="{{ url_for('open_tab') }}">Voltar para A separar</a></div>

<div class="v2-panel" id="rp-box">
{% if t.status == 'rodando' %}
  <h2 style="margin:0 0 6px">Buscando as etiquetas na Shopee…</h2>
  <p class="v2-sub" id="rp-etapa">{{ t.etapa }}</p>
  <div style="height:12px;background:#eceff3;border-radius:999px;overflow:hidden;margin:12px 0">
    <div id="rp-barra" style="height:100%;width:{{ ((t.prontos or 0) * 100 / (t.total or 1))|round|int }}%;background:#f5c518;transition:width .4s"></div></div>
  <p class="v2-sub">Pode deixar esta página aberta: o arquivo baixa sozinho quando ficar pronto (leva ~1 minuto a cada 50 pedidos). Se fechar, ele fica guardado na tela A separar.</p>
  <script>
  (function(){
    function ver(){
      fetch("{{ url_for('reimpressao.estado', tid=t.id) }}",{cache:"no-store"}).then(function(r){return r.json();}).then(function(j){
        if(j.status==="rodando"){
          document.getElementById("rp-etapa").textContent=j.etapa||"";
          document.getElementById("rp-barra").style.width=Math.round((j.prontos||0)*100/(j.total||1))+"%";
          setTimeout(ver,3000);
        } else { location.reload(); }
      }).catch(function(){ setTimeout(ver,5000); });
    }
    setTimeout(ver,3000);
  })();
  </script>
{% elif t.status == 'concluido' %}
  <h2 style="margin:0 0 6px;color:#0d6b3f">✔ {{ t.qtd }} etiqueta(s) prontas</h2>
  <p class="v2-sub">{{ t.nome }}</p>
  <a class="v2-btn" id="rp-baixar" href="{{ url_for('reimpressao.arquivo', tid=t.id) }}" style="margin-top:10px">{{ ic.baixar|safe }}Baixar arquivo (.txt)</a>
  <script>
  (function(){
    var k="rp-baixado-{{ t.id }}", ja=false;
    try{ ja=sessionStorage.getItem(k)==="1"; sessionStorage.setItem(k,"1"); }catch(e){}
    if(!ja){ setTimeout(function(){ location.href=document.getElementById("rp-baixar").href; },400); }
  })();
  </script>
{% else %}
  <h2 style="margin:0 0 6px;color:#9b1c1c">A reimpressão não terminou</h2>
  <p>{{ t.etapa }}</p>
  <a class="v2-btn" href="{{ url_for('open_tab') }}">Voltar e tentar de novo</a>
{% endif %}
{% if t.falhas_l %}
  <details style="margin-top:14px" {% if t.status != 'concluido' or t.falhas_l|length < 6 %}open{% endif %}>
    <summary><b>{{ t.falhas_l|length }} pedido(s) sem etiqueta</b></summary>
    <ul style="margin:8px 0 0 18px">{% for f in t.falhas_l[:100] %}<li><span style="font-family:monospace">{{ f[0] }}</span> — {{ f[1] }}</li>{% endfor %}</ul>
  </details>
{% endif %}
</div>
</div>
"""


def init_app(app, get_client):
    _get_client[0] = get_client
    init_db()
    app.jinja_env.globals["ultima_reimpressao"] = ultima_reimpressao
    app.register_blueprint(bp)
