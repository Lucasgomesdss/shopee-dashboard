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

# Login dos colaboradores: usuário fixo (SEPARADOR 1/2/3) + senha única, guardada
# só como variável de ambiente no Render (nunca no código).
SEPARADOR_USERS = ["SEPARADOR 1", "SEPARADOR 2", "SEPARADOR 3"]
SEPARADOR_PASSWORD = os.environ.get("SEPARADOR_PASSWORD", "")

models.init_db()

if USE_MOCK_DATA:
    # Roda também quando o servidor é iniciado pelo gunicorn (produção), não só localmente.
    import seed_mock
    seed_mock.run()


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
    if request.endpoint in ("set_name", "logout", "static", "shopee_callback"):
        return
    if not get_employee_name():
        return redirect(url_for("set_name", next=request.path))


@app.route("/nome", methods=["GET", "POST"])
def set_name():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if (
            SEPARADOR_PASSWORD
            and username in SEPARADOR_USERS
            and password == SEPARADOR_PASSWORD
        ):
            session["employee_name"] = username
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuário ou senha inválidos.")
    return render_template("set_name.html", users=SEPARADOR_USERS)


@app.route("/sair")
def logout():
    session.pop("employee_name", None)
    return redirect(url_for("set_name"))


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
    """Busca pedidos pendentes de separação na Shopee e traz para a fila local.

    A API da Shopee só aceita janelas de até 15 dias por chamada, então varremos os
    últimos 90 dias em blocos de 15 dias (senão pedidos mais antigos parados na fila
    ficavam de fora da sincronização). Usamos 'update_time' como referência, porque é
    quando o pedido mudou de status — não quando ele foi criado.

    Buscamos dois status: READY_TO_SHIP (pago, ainda sem etiqueta gerada) e PROCESSED
    (etiqueta já gerada, aguardando despacho). Como a etiqueta é impressa antes de o
    pedido ir pro time separar, a maioria dos pedidos chega pra separação já em
    PROCESSED — se buscássemos só READY_TO_SHIP, ficaria de fora quase tudo."""
    if USE_MOCK_DATA:
        flash("Modo demonstração ativo — os pedidos de exemplo já estão carregados.")
        return redirect(url_for("dashboard"))

    client = get_shopee_client()
    if not client:
        flash("Nenhuma loja conectada ainda. Autorize pelo Console da Shopee primeiro (veja o README).")
        return redirect(url_for("dashboard"))

    now = int(time.time())
    lookback_days = 3
    window_days = 15  # limite máximo da Shopee por chamada
    max_pages_per_window = 30  # trava de segurança: até 1500 pedidos por janela/status
    order_statuses = ["READY_TO_SHIP", "PROCESSED"]

    imported = 0
    seen_sns = set()  # todo order_sn visto em READY_TO_SHIP/PROCESSED nesta sincronização
    oldest = now - lookback_days * 24 * 3600
    for status in order_statuses:
        window_end = now
        while window_end > oldest:
            window_start = max(window_end - window_days * 24 * 3600, oldest)
            cursor = ""
            for _ in range(max_pages_per_window):
                resp = client.get_order_list(
                    window_start, window_end, cursor=cursor,
                    order_status=status, time_range_field="update_time",
                )
                if resp.get("error"):
                    flash(f"Erro da Shopee ao buscar pedidos ({status}): {resp.get('message') or resp.get('error')}")
                    return redirect(url_for("dashboard"))

                response = resp.get("response", {})
                order_list = response.get("order_list", [])
                order_sns = [o["order_sn"] for o in order_list]
                seen_sns.update(order_sns)
                if order_sns:
                    details = client.get_order_detail(order_sns).get("response", {}).get("order_list", [])
                    for od in details:
                        items = [
                            {
                                "name": it.get("item_name"),
                                "variation": it.get("model_name") or "-",
                                "quantity": it.get("model_quantity_purchased", 1),
                                "image_url": (it.get("image_info") or {}).get("image_url", ""),
                            }
                            for it in od.get("item_list", [])
                        ]
                        tracking = None
                        packages = od.get("package_list") or []
                        if packages:
                            tracking = packages[0].get("tracking_number")
                        if not tracking:
                            # Nem sempre o get_order_detail já traz o rastreio — busca direto
                            # na API de logística como reforço.
                            try:
                                tn_resp = client.get_tracking_number(od["order_sn"])
                                tracking = tn_resp.get("response", {}).get("tracking_number") or None
                            except Exception:
                                tracking = None
                        models.upsert_order(od["order_sn"], tracking, items)
                        imported += 1

                if not response.get("more"):
                    break
                next_cursor = response.get("next_cursor", "")
                if not next_cursor or next_cursor == cursor:
                    break  # evita loop infinito se a API não avançar o cursor
                cursor = next_cursor

            window_end = window_start

    flash(f"{imported} pedido(s) sincronizado(s) da Shopee.")
    return redirect(url_for("dashboard"))


   # Limpeza automática: pedidos que ainda estão na fila local (a separar/pendente)
    # mas que não apareceram em READY_TO_SHIP/PROCESSED nesta sincronização podem já
    # ter sido despachados. Confirma direto na Shopee antes de mexer em qualquer coisa,
    # pra não marcar como concluído por engano (ex: pedido antigo fora da janela de 3 dias).
    auto_completed = 0
    local_open = [sn for sn in models.list_open_order_sns() if sn not in seen_sns]
    for i in range(0, len(local_open), 50):
        batch = local_open[i:i + 50]
        try:
            details = client.get_order_detail(batch).get("response", {}).get("order_list", [])
        except Exception:
            continue
        for od in details:
            if od.get("order_status") not in ("READY_TO_SHIP", "PROCESSED"):
                models.mark_auto_completed(od["order_sn"])
                auto_completed += 1

    msg = f"{imported} pedido(s) sincronizado(s) da Shopee."
    if auto_completed:
        msg += f" {auto_completed} pedido(s) marcado(s) como concluído automaticamente (já coletado pela transportadora)."
    flash(msg)
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
    images = {}
    for it in items:
        key = (it["name"], it["variation"])
        totals[key] += it["quantity"]
        if not images.get(key):
            images[key] = it.get("image_url") or ""

    rows = [
        {"name": name, "variation": variation, "quantity": qty, "image_url": images.get((name, variation), "")}
        for (name, variation), qty in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0][0]))
    ]
    pdf_buffer = build_picking_list_pdf(rows)
    filename = f"pre-separacao-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
