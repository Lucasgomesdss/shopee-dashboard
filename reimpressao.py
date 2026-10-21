"""Reimpressão de etiquetas (ZPL) dos pedidos da tela "A separar".

Serve para quando o arquivo liberado pela Shopee vem com etiquetas a menos. Pega a
etiqueta que a Shopee JÁ gerou para o pedido (não faz ship_order, não muda status de
nada) e devolve um .txt com o ZPL dos pedidos marcados.
"""

import io
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, flash, redirect, request, url_for

import models

bp = Blueprint("reimpressao", __name__)

BRT = timezone(timedelta(hours=-3))
MAX_PEDIDOS = 300      # trava de segurança por download
LOTE = 50              # limite da Shopee por chamada
ESPERA_MAX = 45        # segundos esperando a Shopee preparar a etiqueta
_get_client = [None]


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


def _lote(client, sns, falhas):
    """Devolve o texto ZPL dos pedidos do lote. Pedidos com problema vão para `falhas`."""
    det = client.get_order_detail(sns).get("response", {}).get("order_list", [])
    pacotes = {}
    rastreios = {}
    for od in det:
        pkg = (od.get("package_list") or [{}])[0]
        pacotes[od["order_sn"]] = pkg.get("package_number")
        rastreios[od["order_sn"]] = pkg.get("tracking_number")
    for sn in sns:
        local = models.get_order(sn)
        if local and local.get("tracking_number"):
            rastreios[sn] = local["tracking_number"]
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
        cri = _json(_post(client, "/api/v2/logistics/create_shipping_document", {"order_list": com_tipo}))
        ok = []
        falhou = {r.get("order_sn"): r for r in (cri.get("response") or {}).get("result_list", []) if r.get("fail_error")}
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

        resp = _post(client, "/api/v2/logistics/download_shipping_document",
                     {"shipping_document_type": tipo, "order_list": prontos}, timeout=120)
        tipo_resp = resp.headers.get("Content-Type", "")
        if "json" in tipo_resp:
            erro = _json(resp)
            for o in prontos:
                falhas.append((o["order_sn"], "baixar: " + str(erro.get("message") or erro.get("error") or "falha no download")))
            continue
        saida.append(_extrair_texto(resp.content))
    return "\n".join(s for s in saida if s)


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

    client = _get_client[0]()
    if client is None:
        flash("Loja ainda não autorizada na Shopee.")
        return redirect(url_for("open_tab"))

    falhas = []
    textos = []
    try:
        for i in range(0, len(sns), LOTE):
            t = _lote(client, sns[i:i + LOTE], falhas)
            if t:
                textos.append(t)
    except Exception as e:  # rede/Shopee fora do ar
        flash(f"Não consegui buscar as etiquetas na Shopee agora: {e}")
        return redirect(url_for("open_tab"))

    if falhas:
        vistos = []
        for sn, motivo in falhas:
            vistos.append(f"{sn} ({motivo})")
        flash("Não saíram " + str(len(falhas)) + " etiqueta(s): " + "; ".join(vistos[:8]) + (" ..." if len(vistos) > 8 else ""))
    if not textos:
        return redirect(url_for("open_tab"))

    conteudo = "\n".join(textos) + "\n"
    n = len(sns) - len(falhas)
    nome = "ETIQUETAS REIMPRESSAO %d %s.txt" % (n, datetime.now(BRT).strftime("%d-%m %H%M"))
    return Response(
        conteudo,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % nome},
    )


def init_app(app, get_client):
    _get_client[0] = get_client
    app.register_blueprint(bp)
