"""Layout novo (menu lateral com ícones) usado só nas páginas Etiquetas e Desempenho.

As demais páginas continuam com o base.html de sempre. Nenhuma função muda: os links
do menu são os mesmos de antes.
"""

from flask import render_template_string

_I = {
    "separar": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "escanear": '<path d="M4 5v14M8 5v14M12 5v14M16 5v14M20 5v14"/>',
    "pendente": '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
    "concluidos": '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 12l3 3 5-6"/>',
    "desempenho": '<path d="M5 20V10M12 20V4M19 20v-7"/>',
    "etiquetas": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.5"/>',
    "sair": '<path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M16 8l4 4-4 4M20 12H9"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "recolher": '<path d="M15 6l-6 6 6 6"/>',
    "recarregar": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "relogio": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "baixar": '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
}


def _svg(nome):
    return (
        '<svg class="v2-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        + _I[nome]
        + "</svg>"
    )


ICONES = {k: _svg(k) for k in _I}

CSS = """
*{box-sizing:border-box}
body.v2{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#d3d7de;color:#1b2230}
.v2-shell{display:flex;min-height:100vh}
.v2-side{width:250px;flex:none;background:#1c2230;color:#e8ebf2;display:flex;flex-direction:column;padding:16px 12px;position:sticky;top:0;height:100vh;transition:width .15s}
.v2-brand{display:flex;align-items:center;gap:10px;padding:6px 8px 16px;border-bottom:1px solid #2b3345;margin-bottom:12px;color:#fff;text-decoration:none;font-weight:700;font-size:17px;line-height:1.2}
.v2-logo{flex:none;width:34px;height:34px;border-radius:9px;background:#fdd32b;color:#1c2230;display:flex;align-items:center;justify-content:center}
.v2-logo .v2-ic{width:20px;height:20px}
.v2-nav{display:flex;flex-direction:column;gap:4px;flex:1}
.v2-nav a{display:flex;align-items:center;gap:12px;padding:11px 12px;border-radius:10px;color:#d5d9e4;text-decoration:none;font-size:15px;white-space:nowrap}
.v2-nav a:hover{background:#262e42}
.v2-nav a.on{background:#3a3520;color:#ffd84a;box-shadow:inset 3px 0 0 #fdd32b;font-weight:600}
.v2-ic{width:22px;height:22px;flex:none}
.v2-foot{border-top:1px solid #2b3345;padding-top:12px;display:flex;flex-direction:column;gap:4px}
.v2-user{display:flex;align-items:center;gap:10px;padding:8px 12px;font-weight:600;font-size:14px}
.v2-av{flex:none;width:30px;height:30px;border-radius:50%;background:#4a5268;display:flex;align-items:center;justify-content:center;font-size:13px}
.v2-foot a{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:10px;color:#d5d9e4;text-decoration:none;font-size:14px}
.v2-foot a:hover{background:#262e42}
.v2-col{background:none;border:0;color:#9aa3b8;cursor:pointer;display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:10px;font-size:14px;font-family:inherit}
.v2-col:hover{background:#262e42}
body.v2-min .v2-side{width:76px}
body.v2-min .v2-txt{display:none}
body.v2-min .v2-brand{justify-content:center;padding-left:0;padding-right:0}
body.v2-min .v2-nav a,body.v2-min .v2-foot a,body.v2-min .v2-col,body.v2-min .v2-user{justify-content:center}
body.v2-min .v2-col .v2-ic{transform:rotate(180deg)}
.v2-main{flex:1;min-width:0;padding:22px 28px 110px}
.v2-top{display:none}
.v2-h{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:18px}
.v2-h h1{margin:0;font-size:26px;font-weight:800;letter-spacing:-.5px}
.v2-sub{font-size:13px;color:#4a5262;margin-top:4px}
.v2-badge{font-size:12px;background:#fff3cd;color:#664d03;padding:2px 8px;border-radius:7px;margin-left:10px;font-weight:600;vertical-align:middle}
.v2-btn{display:inline-flex;align-items:center;gap:8px;background:#fdd32b;color:#1b2230;border:0;border-radius:10px;padding:9px 16px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit}
.v2-btn:hover{background:#f1c500}
.v2-btn.sec{background:#fff;border:1px solid #c7ccd6;font-weight:600;padding:6px 12px;font-size:12px}
.v2-btn.sec:hover{background:#f3f4f7}
.v2-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:22px}
.v2-card{background:#fff;border-radius:14px;padding:12px 16px;display:flex;align-items:center;gap:14px;border:2px solid transparent}
.v2-card .ci{flex:none;width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.v2-card .ci .v2-ic{width:21px;height:21px}
.v2-card .l{font-size:12px;color:#4a5262;font-weight:600}
.v2-card .n{font-size:28px;font-weight:800;line-height:1.1}
.v2-card.y{border-color:#e0b800}.v2-card.y .ci{background:#fff3b0;color:#6b5600}
.v2-card.o{border-color:#f08a24}.v2-card.o .ci{background:#ffd9b3;color:#8a3b00}
.v2-card.g{border-color:#3aa63a}.v2-card.g .ci{background:#c8efc8;color:#14551a}
.v2-sec{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 12px}
.v2-sec h2{margin:0;font-size:18px;font-weight:800}
.v2-sec .r{display:flex;align-items:center;gap:10px;font-size:12px;color:#4a5262}
.v2-list{display:flex;flex-direction:column;gap:10px}
.v2-lote{display:flex;align-items:center;gap:14px;background:#fff;border:2px solid transparent;border-radius:14px;padding:11px 16px;cursor:pointer}
.v2-lote:hover{border-color:#fdd32b}
.v2-lote.sel{background:#fff8dc;border-color:#fdd32b}
.v2-lote input{flex:none;width:18px;height:18px;cursor:pointer;accent-color:#e0a800}
.v2-lt{flex:1;min-width:0}
.v2-lt b{display:block;font-size:14px;font-weight:800}
.v2-lt small{display:block;font-size:11px;color:#4a5262;margin-top:3px;line-height:1.4}
.v2-cnt{flex:none;min-width:70px;text-align:center;font-size:20px;font-weight:800;border:2px solid #1a73e8;border-radius:10px;padding:2px 12px;background:#fff}
.v2-bar{position:fixed;left:250px;right:0;bottom:0;background:#fff;border-top:1px solid #c7ccd6;padding:10px 28px;display:flex;align-items:center;gap:20px;z-index:20}
body.v2-min .v2-bar{left:76px}
.v2-bar .t{flex:1}
.v2-bar .t span{font-size:12px;color:#4a5262;display:block}
.v2-bar .t b{font-size:22px;font-weight:800}
.v2-bar .v2-btn{padding:11px 28px;font-size:14px}
.v2-msg{font-size:12px;color:#8a3b00}
.v2-flash{background:#fff3cd;color:#664d03;border-radius:10px;padding:10px 14px;margin-bottom:14px}
.v2-panel{background:#fff;border-radius:14px;padding:16px 18px}
.v2-panel h3{margin:0 0 10px;font-size:15px;font-weight:800}
@media (max-width:900px){
  .v2-shell{display:block}
  .v2-side{position:fixed;left:-270px;top:0;z-index:50;width:250px;transition:left .15s}
  body.v2-open .v2-side{left:0}
  .v2-top{display:flex;align-items:center;gap:12px;background:#1c2230;color:#fff;padding:10px 14px;position:sticky;top:0;z-index:30}
  .v2-top button{background:none;border:0;color:#fff;cursor:pointer;display:flex}
  .v2-main{padding:16px 14px 110px}
  .v2-bar{left:0;padding:10px 14px;gap:12px}
  body.v2-min .v2-bar{left:0}
  .v2-cards{grid-template-columns:1fr}
  .v2-h h1{font-size:22px}
  .v2-col{display:none}
}
"""

BASE_TOP = (
    """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ v2_title }}</title>
<style>"""
    + CSS
    + """</style>
</head>
<body class="v2">
<div class="v2-top"><button type="button" id="v2-burger" aria-label="Menu">"""
    + ICONES["menu"]
    + """</button><b>Separação de Pedidos</b></div>
<div class="v2-shell">
<aside class="v2-side">
  <a class="v2-brand" href="{{ url_for('dashboard') }}"><span class="v2-logo">"""
    + ICONES["pendente"]
    + """</span><span class="v2-txt">Separação<br>de Pedidos</span></a>
  <nav class="v2-nav">
    <a href="{{ url_for('open_tab') }}">"""
    + ICONES["separar"]
    + """<span class="v2-txt">A separar</span></a>
    <a href="{{ url_for('scan') }}">"""
    + ICONES["escanear"]
    + """<span class="v2-txt">Escanear</span></a>
    <a href="{{ url_for('pending_tab') }}">"""
    + ICONES["pendente"]
    + """<span class="v2-txt">Produto Pendente</span></a>
    <a href="{{ url_for('completed_tab') }}">"""
    + ICONES["concluidos"]
    + """<span class="v2-txt">Concluídos</span></a>
    <a href="{{ url_for('desempenho.painel') }}"{% if v2_active == 'desempenho' %} class="on"{% endif %}>"""
    + ICONES["desempenho"]
    + """<span class="v2-txt">Desempenho</span></a>
    <a href="{{ url_for('lotes.painel') }}"{% if v2_active == 'etiquetas' %} class="on"{% endif %}>"""
    + ICONES["etiquetas"]
    + """<span class="v2-txt">Etiquetas</span></a>
  </nav>
  <div class="v2-foot">
    {% if employee_name %}<div class="v2-user"><span class="v2-av">{{ employee_name[:1] }}</span><span class="v2-txt">{{ employee_name }}</span></div>{% endif %}
    <button type="button" class="v2-col" id="v2-col">"""
    + ICONES["recolher"]
    + """<span class="v2-txt">Recolher menu</span></button>
    <a href="{{ url_for('logout') }}">"""
    + ICONES["sair"]
    + """<span class="v2-txt">Sair</span></a>
  </div>
</aside>
<main class="v2-main">
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="v2-flash">{{ m }}</div>{% endfor %}{% endwith %}
"""
)

BASE_BOTTOM = """
</main>
</div>
<script>
(function(){
  var b=document.body;
  try{ if(localStorage.getItem('v2min')==='1') b.classList.add('v2-min'); }catch(e){}
  document.getElementById('v2-col').addEventListener('click',function(){
    b.classList.toggle('v2-min');
    try{ localStorage.setItem('v2min', b.classList.contains('v2-min')?'1':'0'); }catch(e){}
  });
  document.getElementById('v2-burger').addEventListener('click',function(){ b.classList.toggle('v2-open'); });
})();
</script>
</body>
</html>
"""


def render_page(title, active, content, **ctx):
    """Renderiza o conteúdo (um trecho Jinja) dentro do layout novo."""
    return render_template_string(
        BASE_TOP + content + BASE_BOTTOM, v2_title=title, v2_active=active, **ctx
    )


def icone(nome):
    """Ícone SVG pronto para usar dentro do conteúdo das páginas."""
    return ICONES[nome]
