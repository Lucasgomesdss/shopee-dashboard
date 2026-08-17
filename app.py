import os
import json
import time
from datetime import datetime, timedelta
from io import BytesIO
from collections import defaultdict

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session

import models
import phrases
from shopee_client import ShopeeClient
from pdf_report import build_picking_list_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.jinja_env.filters["fromjson"] = json.loads

PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID")
PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY")
USE_MOCK_DATA = os.environ.get("USE_MOCK_DATA", "true").lower() == "true"

# Login dos colaboradores: usuário fixo (nome de cada uma) + senha única, guardada
# só como variável de ambiente no Render (nunca no código).
SEPARADOR_USERS = ["JULIANA", "LUCAS", "RAFAELA", "DANIELA"]
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


def _aggregate_items(items):
    """Agrupa uma lista de itens (name, variation, sku, quantity, image_url) somando as
    quantidades por produto/variação — usado tanto na pré-separação quanto no
    Produto Pendente."""
    totals = defaultdict(int)
    images = {}
    skus = {}
    for it in items:
        key = (it["name"], it["variation"])
        totals[key] += it["quantity"]
        if not images.get(key):
            images[key] = it.get("image_url") or ""
        if not skus.get(key):
            skus[key] = it.get("sku") or ""
    return [
        {
            "name": name,
            "variation": variation,
            "quantity": qty,
            "image_url": images.get((name, variation), ""),
            "sku": skus.get((name, variation), ""),
        }
        for (name, variation), qty in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0][0]))
    ]


@app.context_processor
def inject_employee_name():
    return {"employee_name": get_employee_name()}


@app.context_processor
def inject_sidebar_counts():
    """Deixa os contadores (a separar / falta produto / separados) disponíveis em
    todas as páginas, pra mostrar na barra lateral sem precisar repetir em cada rota."""
    return {"sidebar_counts": models.counts()}


@app.context_processor
def inject_daily_phrase():
    """Frase motivacional do dia, disponível em todas as páginas (usada na tela de
    Escanear, que é onde o time passa o dia todo)."""
    return {"daily_phrase": phrases.get_daily_phrase()}


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
    """Busca pedidos com etiqueta já gerada (status PROCESSED = 'envios processados'
    na Shopee) e traz os novos para a fila de separação local.

    A API da Shopee só aceita janelas de até 15 dias por chamada, então varremos os
    últimos dias em blocos de 15 dias. Usamos 'update_time' como referência, porque é
    quando o pedido mudou de status — não quando ele foi criado.

    Só buscamos PROCESSED (etiqueta já gerada, aguardando despacho) — pedidos ainda em
    READY_TO_SHIP (pagos mas sem etiqueta) não entram na fila de separação.

    Também pulamos pedidos Fulfilled by Shopee (FBS): o estoque desses fica no centro de
    distribuição da própria Shopee, então o nosso time nunca separa esse pedido fisicamente
    -- só os pedidos do estoque do vendedor (fulfilled_by_local_seller) entram na fila.

    Esse sync é rápido de propósito: só busca detalhe/rastreio na Shopee dos pedidos que
    ainda não conhecemos (ou que estão arquivados/sem rastreio salvo) -- pedidos já
    sincronizados antes não são reconsultados, já que o conteúdo deles não muda enquanto
    continuam PROCESSED. Sem esse filtro, uma loja com centenas de pedidos PROCESSED por
    dia batia na Shopee pra fila inteira a cada clique em Sincronizar (get_order_detail +
    fallback de rastreio pedido por pedido), o que estourava o timeout do servidor. Essa
    conferência mais pesada (ver com a Shopee quem já foi de fato coletado) fica no botão
    'Dia finalizado', pra não deixar a sincronização do dia a dia lenta."""
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
    max_pages_per_window = 30  # trava de segurança: até 1500 pedidos por janela

    imported = 0
    skipped_fbs = 0
    skipped_up_to_date = 0
    oldest = now - lookback_days * 24 * 3600
    window_end = now
    while window_end > oldest:
        window_start = max(window_end - window_days * 24 * 3600, oldest)
        cursor = ""
        for _ in range(max_pages_per_window):
            resp = client.get_order_list(
                window_start, window_end, cursor=cursor,
                order_status="PROCESSED", time_range_field="update_time",
                page_size=100,
            )
            if resp.get("error"):
                flash(f"Erro da Shopee ao buscar pedidos: {resp.get('message') or resp.get('error')}")
                return redirect(url_for("dashboard"))

            response = resp.get("response", {})
            order_list = response.get("order_list", [])
            order_sns = [o["order_sn"] for o in order_list]

            # Só busca detalhe/rastreio dos que realmente precisam (novos, arquivados ou
            # sem rastreio salvo) -- pedidos já sincronizados antes não mudam de conteúdo
            # enquanto continuam PROCESSED, então pular eles evita bater na Shopee de novo
            # pra fila inteira a cada sincronização.
            to_refresh = models.filter_needs_sync(order_sns)
            skipped_up_to_date += len(order_sns) - len(to_refresh)

            for i in range(0, len(to_refresh), 50):
                batch = to_refresh[i:i + 50]
                details = client.get_order_detail(batch).get("response", {}).get("order_list", [])
                for od in details:
                    # FBS (Fulfilled by Shopee): o estoque fica no centro de distribuição da
                    # Shopee, não no nosso -- esse pedido nunca passa pela nossa separação.
                    if od.get("fulfillment_flag") == "fulfilled_by_shopee":
                        skipped_fbs += 1
                        continue
                    items = [
                        {
                            "name": it.get("item_name"),
                            "variation": it.get("model_name") or "-",
                            "sku": it.get("model_sku") or it.get("item_sku") or "",
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
                        # Etiqueta já foi gerada (só buscamos PROCESSED), então o
                        # rastreio deveria existir — busca direto na API de
                        # logística como reforço, caso não venha no detalhe.
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

    msg = f"{imported} pedido(s) sincronizado(s) da Shopee."
    if skipped_fbs:
        msg += f" {skipped_fbs} pedido(s) Fulfilled by Shopee (estoque da Shopee) ignorado(s) -- não entram na separação."
    if skipped_up_to_date:
        msg += f" {skipped_up_to_date} pedido(s) já estavam sincronizados e foram pulados."
    flash(msg)
    return redirect(url_for("dashboard"))


@app.route("/dia-finalizado", methods=["POST"])
def dia_finalizado():
    """Conferência de fim de dia (a parte mais pesada, por isso é sob demanda e não em
    toda sincronização):

    1) Verifica com a Shopee quais pedidos já marcados como Concluídos (separados)
       deixaram de estar com etiqueta ativa (PROCESSED) -- seja porque a transportadora
       já coletou (SHIPPED/COMPLETED/TO_CONFIRM_RECEIVE), seja porque o pedido foi
       CANCELADO depois que o time já tinha separado -- e os arquiva, além de qualquer
       pedido Fulfilled by Shopee que tenha ficado marcado como concluído por engano.
       Somem da lista/contagem de Concluídos, mas continuam salvos no banco pra
       histórico. Antes só verificava SHIPPED/COMPLETED, então pedidos cancelados
       ficavam presos em Concluídos pra sempre.

    2) Verifica com a Shopee os pedidos que ainda estão em A separar/Pendente e já
       saíram do status PROCESSED (foram despachados por outro canal, cancelados,
       etc) sem que o time chegasse a bipar -- esses também são arquivados, pra que
       'A separar' não fique cheio de pedidos que na prática já saíram da Shopee.
       Isso é o que corrige a contagem de A separar ficando maior que a realidade.

    3) Também arquiva da fila de A separar/Pendente qualquer pedido Fulfilled by
       Shopee (FBS) que tenha entrado ali antes dessa checagem existir no /sync --
       pedido FBS é separado no centro de distribuição da própria Shopee, nunca pelo
       nosso time, então não deveria contar como 'a separar' aqui.

    4) Verifica os pedidos arquivados e traz de volta pra A separar qualquer um que
       tenha voltado a ter etiqueta válida (PROCESSED) na Shopee -- acontece quando a
       transportadora invalida uma etiqueta (ex: peso/medida divergente) e a Shopee
       reemite uma nova. Sem essa checagem o pedido ficava escondido do time pra
       sempre mesmo precisando ser separado de novo -- foi o que causou a divergência
       de '310 pedidos com etiqueta pronta na Shopee vs 188 no dashboard'."""
    if USE_MOCK_DATA:
        flash("Modo demonstração ativo.")
        return redirect(url_for("dashboard"))

    client = get_shopee_client()
    if not client:
        flash("Nenhuma loja conectada ainda.")
        return redirect(url_for("dashboard"))

    archived_completed = 0
    completed_sns = models.list_completed_order_sns(limit=500)
    for i in range(0, len(completed_sns), 50):
        batch = completed_sns[i:i + 50]
        try:
            details = client.get_order_detail(batch).get("response", {}).get("order_list", [])
        except Exception:
            continue
        for od in details:
            if od.get("order_status") != "PROCESSED" or od.get("fulfillment_flag") == "fulfilled_by_shopee":
                models.archive_order(od["order_sn"])
                archived_completed += 1

    archived_stale = 0
    archived_fbs = 0
    open_sns = models.list_open_order_sns(limit=500)
    for i in range(0, len(open_sns), 50):
        batch = open_sns[i:i + 50]
        try:
            details = client.get_order_detail(batch).get("response", {}).get("order_list", [])
        except Exception:
            continue
        for od in details:
            if od.get("fulfillment_flag") == "fulfilled_by_shopee":
                models.archive_order(od["order_sn"])
                archived_fbs += 1
            elif od.get("order_status") != "PROCESSED":
                models.archive_order(od["order_sn"])
                archived_stale += 1

    revived = 0
    archived_sns = models.list_archived_order_sns(limit=500)
    for i in range(0, len(archived_sns), 50):
        batch = archived_sns[i:i + 50]
        try:
            details = client.get_order_detail(batch).get("response", {}).get("order_list", [])
        except Exception:
            continue
        for od in details:
            if (
                od.get("order_status") == "PROCESSED"
                and od.get("fulfillment_flag") != "fulfilled_by_shopee"
            ):
                models.revive_order(od["order_sn"])
                revived += 1

    flash(
        f"Dia finalizado: {archived_completed} pedido(s) coletado(s) removido(s) dos concluídos, "
        f"{archived_stale} pedido(s) que já saíram da Shopee removido(s) da fila de separação, "
        f"{archived_fbs} pedido(s) Fulfilled by Shopee (estoque da Shopee) removido(s) da fila de separação, "
        f"{revived} pedido(s) que voltaram a ter etiqueta válida trazido(s) de volta pra A separar."
    )
    return redirect(url_for("dashboard"))


@app.route("/scan", methods=["GET", "POST"])
def scan():
    """Se o pedido bipado já estiver separado (status completed), mostramos ele
    normalmente -- com foto, título e SKU -- mas com um aviso de 'já separado' e sem
    os botões de ação, pra colaboradora conseguir conferir de novo sem risco de mudar
    o status por engano."""
    order = None
    if request.method == "POST":
        tracking = request.form.get("tracking_number", "")
        order = models.find_by_tracking(tracking)
        if not order:
            flash(f"Nenhum pedido encontrado para o código '{tracking}'.")
    return render_template("scan.html", order=order)


@app.route("/order/<order_sn>/complete", methods=["POST"])
def complete_order(order_sn):
    order = models.get_order(order_sn)
    if order and order["status"] == models.STATUS_COMPLETED:
        flash(f"Pedido {order_sn} já estava separado.")
        return redirect(url_for("scan"))
    models.mark_completed(order_sn, get_employee_name())
    flash(f"Pedido {order_sn} marcado como concluído.")
    return redirect(url_for("scan"))


@app.route("/order/<order_sn>/pending", methods=["POST"])
def pending_order(order_sn):
    order = models.get_order(order_sn)
    if order and order["status"] == models.STATUS_COMPLETED:
        flash(f"Pedido {order_sn} já estava separado.")
        return redirect(url_for("scan"))
    reason = request.form.get("reason", "").strip() or "Sem detalhes informados"
    models.mark_pending(order_sn, get_employee_name(), reason)
    flash(f"Pedido {order_sn} movido para pendências.")
    return redirect(url_for("scan"))


@app.route("/order/<order_sn>/missing-product", methods=["POST"])
def missing_product(order_sn):
    """O separador marca qual(is) item(ns) do pedido não foram encontrados no estoque.
    O pedido vai pra fila de Produto Pendente e os itens entram na lista agregada
    (usada pra gerar o PDF de busca em massa na expedição)."""
    order = models.get_order(order_sn)
    if not order:
        flash(f"Pedido {order_sn} não encontrado.")
        return redirect(url_for("scan"))
    if order["status"] == models.STATUS_COMPLETED:
        flash(f"Pedido {order_sn} já estava separado.")
        return redirect(url_for("scan"))

    items = json.loads(order["items_json"])
    selected = request.form.getlist("missing_item")
    missing_items = [items[int(i)] for i in selected if i.isdigit() and int(i) < len(items)]

    if not missing_items:
        flash("Selecione ao menos um item que faltou antes de confirmar.")
        return redirect(url_for("scan"))

    models.mark_missing_product(order_sn, get_employee_name(), missing_items)
    flash(f"Pedido {order_sn} movido para Produto Pendente ({len(missing_items)} item(ns) faltando).")
    return redirect(url_for("scan"))


@app.route("/order/<order_sn>/reopen", methods=["POST"])
def reopen_order(order_sn):
    models.reopen(order_sn)
    flash(f"Pedido {order_sn} voltou para a fila de separação.")
    return redirect(url_for("pending_tab"))


@app.route("/pendencias")
def pending_tab():
    orders = models.list_by_status(models.STATUS_PENDING)
    missing_rows = _aggregate_items(models.list_missing_items())
    return render_template("pending.html", orders=orders, missing_rows=missing_rows)


@app.route("/concluidos")
def completed_tab():
    orders = models.list_by_status(models.STATUS_COMPLETED)
    return render_template("completed.html", orders=orders)


@app.route("/picking-list.pdf")
def picking_list_pdf():
    rows = _aggregate_items(models.open_orders_items())
    pdf_buffer = build_picking_list_pdf(rows)
    filename = f"pre-separacao-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/produto-pendente.pdf")
def missing_products_pdf():
    rows = _aggregate_items(models.list_missing_items())
    pdf_buffer = build_picking_list_pdf(
        rows, title="Produto Pendente", subtitle="itens que faltaram na separação"
    )
    filename = f"produto-pendente-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
