import os
import json
import time
from datetime import datetime, timedelta
from io import BytesIO
from collections import defaultdict

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session

import models
from shopee_client import ShopeeClient
from pdf_report import build_picking_list_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.jinja_env.filters["fromjson"] = json.loads

PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID")
PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY")
USE_MOCK_DATA = os.environ.get("USE_MOCK_DATA", "true").lower() == "true"

models.init_db()


def get_shopee_client():
    """Monta um ShopeeClient com o access_token válido mais recente, renovando
    automaticamente pelo refresh_token se estiver perto de expirar."""
    token_row = models.get_shopee_token()
    if not token_row:
        return None

    client = ShopeeClient(PARTNER_ID, PARTNER_KEY, token_row["shop_id"])

    # Renova com folga de 5 minutos antes de expirar
    if time.time() > token_row["expires_at"] - 300:
        result = client.refresh_access_token(token_row["refresh_token"], token_row["shop_id"])
        if "access_token" not in result:
            return None  # refresh falhou (token expirado há mais de 30 dias, ex.) — precisa reautorizar
        models.save_shopee_token(
            token_row["shop_id"], result["access_token"], result["refresh_token"], result.get("expire_in", 14400)
        )
        client.access_token = result["access_token"]
    else:
        client.access_token = token_row["access_token"]

    return client


def get_employee_name():
    return session.get("employee_name", "")


@app.context_processor
def inject_employee_name():
    return {"employee_name": get_employee_name()}


@app.before_request
def ensure_employee_name():
    if request.endpoint in ("set_name", "static", "shopee_callback"):
        return
    if not get_employee_name():
        return redirect(url_for("set_name", next=request.path))


@app.route("/nome", methods=["GET", "POST"])
def set_name():
    if request.method == "POST":
        name = request.form.get("employee_name", "").strip()
        if name:
            session["employee_name"] = name
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Digite seu nome para continuar.")
    return render_template("set_name.html")


@app.route("/")
def dashboard():
    c = models.counts()
    token_row = models.get_shopee_token()
    return render_template(
        "dashboard.html",
        counts=c,
        employee_name=get_employee_name(),
        mock_mode=USE_MOCK_DATA,
        shopee_connected=bool(token_row),
        shopee_shop_id=token_row["shop_id"] if token_row else None,
        credentials_configured=bool(PARTNER_ID and PARTNER_KEY),
    )


@app.route("/shopee/callback")
def shopee_callback():
    """Endereço que a Shopee chama depois que você clica em 'Authorize' no Console
    (ou quando o vendedor autoriza pelo link, no caso de apps ERP). Recebe 'code' e
    'shop_id' na URL e troca isso por um access_token, que fica salvo no banco."""
    code = request.args.get("code")
    shop_id = request.args.get("shop_id")
    if not code or not shop_id:
        flash("Retorno da Shopee incompleto (faltou 'code' ou 'shop_id').")
        return redirect(url_for("dashboard"))

    if not (PARTNER_ID and PARTNER_KEY):
        flash("Configure SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY antes de autorizar a loja.")
        return redirect(url_for("dashboard"))

    client = ShopeeClient(PARTNER_ID, PARTNER_KEY)
    result = client.get_access_token(code, shop_id)
    if "access_token" not in result:
        flash(f"Falha ao autorizar: {result.get('message') or result}")
        return redirect(url_for("dashboard"))

    models.save_shopee_token(shop_id, result["access_token"], result["refresh_token"], result.get("expire_in", 14400))
    flash(f"Loja {shop_id} conectada com sucesso! Já pode sincronizar os pedidos.")
    return redirect(url_for("dashboard"))


@app.route("/sync", methods=["POST"])
def sync():
    """Busca pedidos 'Pronto para envio' na Shopee e traz para a fila local de separação."""
    if USE_MOCK_DATA:
        flash("Modo demonstração ativo — os pedidos de exemplo já estão carregados.")
        return redirect(url_for("dashboard"))

    client = get_shopee_client()
    if not client:
        flash("Nenhuma loja conectada ainda. Autorize pelo Console da Shopee primeiro (veja o README).")
        return redirect(url_for("dashboard"))

    now = int(time.time())
    time_from = now - 15 * 24 * 3600  # últimos 15 dias
    cursor = ""
    imported = 0
    while True:
        resp = client.get_order_list(time_from, now, cursor=cursor, order_status="READY_TO_SHIP")
        response = resp.get("response", {})
        order_list = response.get("order_list", [])
        order_sns = [o["order_sn"] for o in order_list]
        if order_sns:
            details = client.get_order_detail(order_sns).get("response", {}).get("order_list", [])
            for od in details:
                items = [
                    {
                        "name": it.get("item_name"),
                        "variation": it.get("model_name") or "-",
                        "quantity": it.get("model_quantity_purchased", 1),
                    }
                    for it in od.get("item_list", [])
                ]
                tracking = None
                packages = od.get("package_list") or []
                if packages:
                    tracking = packages[0].get("tracking_number")
                models.upsert_order(od["order_sn"], tracking, items)
                imported += 1
        if not response.get("more"):
            break
        cursor = response.get("next_cursor", "")

    flash(f"{imported} pedido(s) sincronizado(s) da Shopee.")
    return redirect(url_for("dashboard"))


@app.route("/scan", methods=["GET", "POST"])
def scan():
    order = None
    if request.method == "POST":
        tracking = request.form.get("tracking_number", "")
        order = models.find_by_tracking(tracking)
        if not order:
            flash(f"Nenhum pedido encontrado para o código '{tracking}'.")
        elif order["status"] == models.STATUS_COMPLETED:
            flash("Este pedido já foi separado e concluído anteriormente.")
            order = None
    return render_template("scan.html", order=order)


@app.route("/order/<order_sn>/complete", methods=["POST"])
def complete_order(order_sn):
    models.mark_completed(order_sn, get_employee_name())
    flash(f"Pedido {order_sn} marcado como concluído.")
    return redirect(url_for("scan"))


@app.route("/order/<order_sn>/pending", methods=["POST"])
def pending_order(order_sn):
    reason = request.form.get("reason", "").strip() or "Sem detalhes informados"
    models.mark_pending(order_sn, get_employee_name(), reason)
    flash(f"Pedido {order_sn} movido para pendências.")
    return redirect(url_for("scan"))


@app.route("/order/<order_sn>/reopen", methods=["POST"])
def reopen_order(order_sn):
    models.reopen(order_sn)
    flash(f"Pedido {order_sn} voltou para a fila de separação.")
    return redirect(url_for("pending_tab"))


@app.route("/pendencias")
def pending_tab():
    orders = models.list_by_status(models.STATUS_PENDING)
    return render_template("pending.html", orders=orders)


@app.route("/concluidos")
def completed_tab():
    orders = models.list_by_status(models.STATUS_COMPLETED)
    return render_template("completed.html", orders=orders)


@app.route("/picking-list.pdf")
def picking_list_pdf():
    items = models.open_orders_items()
    totals = defaultdict(int)
    for it in items:
        key = (it["name"], it["variation"])
        totals[key] += it["quantity"]

    rows = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0][0]))
    pdf_buffer = build_picking_list_pdf(rows)
    filename = f"pre-separacao-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


if __name__ == "__main__":
    if USE_MOCK_DATA:
        import seed_mock
        seed_mock.run()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
