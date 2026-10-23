"""Serviço PRÓPRIO das Devoluções (separado do dashboard de separação).

Roda em outro serviço do Render, com banco próprio (disco próprio em /var/data) e
endereço próprio. Se este serviço travar, a bipagem da separação não é afetada.

Start command no Render:
    gunicorn devolucoes_app:app --timeout 600 --workers 1 --threads 4

Variáveis de ambiente deste serviço:
    SECRET_KEY            texto aleatório (sessão de login)
    SEPARADOR_PASSWORD    a mesma senha do dashboard
    BLING_CLIENT_ID / BLING_CLIENT_SECRET   app "DescFlow Devoluções" (conta Ana)
    SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY  mesmos do dashboard (assinatura das chamadas)
    DASHBOARD_URL         https://shopee-dashboard-bqjj.onrender.com
    INTERNO_TOKEN         o mesmo texto secreto configurado no dashboard

A Shopee: o token continua sendo renovado SÓ pelo dashboard. Este serviço pede o token
atual ao dashboard (/interno/shopee-token), assim os dois nunca brigam pela renovação.
"""

import os
import time

import requests
from flask import Flask, flash, redirect, render_template_string, request, session, url_for

import models
from shopee_client import ShopeeClient

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave")

PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID")
PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY")
DASHBOARD_URL = (os.environ.get("DASHBOARD_URL") or "").rstrip("/")
INTERNO_TOKEN = os.environ.get("INTERNO_TOKEN", "")
SENHA = os.environ.get("SEPARADOR_PASSWORD", "")
USUARIOS = ["JULIANA", "LUCAS"]

models.init_db()

_token = {"valor": None, "ate": 0.0}


def get_shopee_client():
    """ShopeeClient com o token atual, pedido ao dashboard (que é quem renova)."""
    if not (PARTNER_ID and PARTNER_KEY and DASHBOARD_URL and INTERNO_TOKEN):
        return None
    if not _token["valor"] or time.time() > _token["ate"]:
        try:
            r = requests.get(
                f"{DASHBOARD_URL}/interno/shopee-token",
                headers={"X-Interno": INTERNO_TOKEN}, timeout=15,
            )
            dados = r.json() if r.status_code == 200 else None
        except Exception:
            dados = None
        if not dados or not dados.get("access_token"):
            return None
        _token["valor"] = dados
        # usa por no máximo 10 min (ou até 5 min antes de expirar)
        _token["ate"] = min(time.time() + 600, float(dados.get("expires_at") or 0) - 300)
    t = _token["valor"]
    client = ShopeeClient(PARTNER_ID, PARTNER_KEY, t["shop_id"])
    client.access_token = t["access_token"]
    return client


# ------------------------------------------------------------------------ layout

LAYOUT = """<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ v2_title }} · Ana Baby</title>
<link rel="stylesheet" href="{{ url_for('static', filename='v2.css') }}?v=1">
<style>
.dv-top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;background:#1c2230;color:#e8ebf2;padding:12px 22px}
.dv-top a{color:#d5d9e4;text-decoration:none;font-size:14px}
.dv-top b{font-size:16px}
</style></head>
<body class="v2" style="display:block">
<div class="dv-top"><b>Devoluções · Ana Baby</b>
  <span>{{ session.get('employee_name', '') }} · {% if dashboard_url %}<a href="{{ dashboard_url }}">Dashboard de separação</a> · {% endif %}<a href="{{ url_for('sair') }}">Sair</a></span></div>
<main class="v2-main" style="max-width:1160px;margin:0 auto">
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="v2-flash">{{ m }}</div>{% endfor %}{% endwith %}
"""
LAYOUT_FIM = "</main></body></html>"


def render(title, active, content, **ctx):
    return render_template_string(
        LAYOUT + content + LAYOUT_FIM, v2_title=title, v2_active=active,
        dashboard_url=DASHBOARD_URL, **ctx
    )


LOGIN = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Entrar · Devoluções</title>
<link rel="stylesheet" href="{{ url_for('static', filename='v2.css') }}?v=1"></head>
<body class="v2" style="display:block"><main style="max-width:380px;margin:70px auto;padding:0 16px">
<div class="v2-panel"><h3 style="font-size:20px">Devoluções · Ana Baby</h3>
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="v2-flash">{{ m }}</div>{% endfor %}{% endwith %}
<form method="post" style="display:flex;flex-direction:column;gap:10px">
<select name="username" required style="font:inherit;font-size:17px;padding:10px;border:1px solid #c7ccd6;border-radius:10px">
<option value="" disabled selected>Selecione seu usuário</option>
{% for u in usuarios %}<option value="{{ u }}">{{ u }}</option>{% endfor %}</select>
<input type="password" name="password" placeholder="Senha" required style="font:inherit;font-size:17px;padding:10px;border:1px solid #c7ccd6;border-radius:10px">
<button class="v2-btn" type="submit" style="justify-content:center;padding:12px">Entrar</button></form></div></main></body></html>"""


@app.route("/entrar", methods=["GET", "POST"])
def entrar():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        if SENHA and u in USUARIOS and request.form.get("password", "") == SENHA:
            session["employee_name"] = u
            return redirect(request.args.get("next") or url_for("devolucoes.painel"))
        flash("Usuário ou senha inválidos.")
    return render_template_string(LOGIN, usuarios=USUARIOS)


@app.route("/sair")
def sair():
    session.pop("employee_name", None)
    return redirect(url_for("entrar"))


@app.route("/")
def inicio():
    return redirect(url_for("devolucoes.painel"))


@app.before_request
def exigir_login():
    if request.endpoint in ("entrar", "sair", "static", "devolucoes.bling_callback"):
        return None
    if session.get("employee_name") not in USUARIOS:
        return redirect(url_for("entrar", next=request.path))
    return None


import devolucoes  # noqa: E402
import migracao  # noqa: E402

devolucoes.init_app(app, get_shopee_client, render=render, home="entrar")
migracao.init_app(app, DASHBOARD_URL, INTERNO_TOKEN)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=True)
