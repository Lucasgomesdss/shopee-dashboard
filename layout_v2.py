"""Layout novo (menu lateral com ícones) usado só nas páginas Etiquetas e Desempenho.

As demais páginas continuam com o base.html de sempre. Nenhuma função muda: os links
do menu são os mesmos de antes.
"""

from flask import render_template_string

_I = {
    "painel": '<path d="M3 11l9-8 9 8M5 10v10h14V10"/>',
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

CSS_LINK = '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'v2.css\') }}?v=1">'

BASE_TOP = (
    """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ v2_title }}</title>
"""
    + CSS_LINK
    + """
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
    <a href="{{ url_for('dashboard') }}">"""
    + ICONES["painel"]
    + """<span class="v2-txt">Dashboard</span></a>
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
