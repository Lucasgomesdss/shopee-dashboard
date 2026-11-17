"""Fotos dos produtos da Shopee.

A API da Shopee ainda devolve os links das fotos no endereço antigo (cf.shopee.com.br),
que desde 11/2026 falha em parte das fotos (principalmente as mais novas) ou demora.
O endereço atual da Shopee (down-br.img.susercontent.com) serve todas. Aqui o painel troca
o endereço na hora de mostrar a página, sem mexer no que está gravado no banco.
Se a foto falhar no endereço novo, a página tenta de novo no antigo (ver base.html).
"""

ANTIGO = "https://cf.shopee.com.br/file/"
NOVO = "https://down-br.img.susercontent.com/file/"


def url(u):
    return (NOVO + u[len(ANTIGO):]) if u and u.startswith(ANTIGO) else u


def init_app(app):
    @app.after_request
    def _trocar_fotos(resp):
        try:
            if (resp.mimetype == "text/html" and not resp.direct_passthrough
                    and not resp.is_streamed):
                corpo = resp.get_data(as_text=True)
                if ANTIGO in corpo:
                    resp.set_data(corpo.replace(ANTIGO, NOVO))
        except Exception:
            pass
        return resp
