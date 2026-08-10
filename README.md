# Dashboard de Separação de Pedidos (Shopee)

## Teste rápido agora (Windows), enquanto aguarda a aprovação da Shopee

Não precisa saber programar, é só seguir os passos:

1. Instale o Python: baixe em **python.org/downloads** (botão amarelo "Download Python").
   Na primeira tela do instalador, marque a caixinha **"Add python.exe to PATH"** antes
   de clicar em Install.
2. Extraia o `shopee-dashboard.zip` numa pasta (ex: Área de Trabalho).
3. Abra o **Prompt de Comando** (pesquise "cmd" no menu Iniciar).
4. Digite os comandos abaixo, um de cada vez (troque o caminho pela pasta onde extraiu):
   ```
   cd Desktop\shopee-dashboard
   pip install -r requirements.txt
   python app.py
   ```
5. Vai aparecer uma lista de códigos de rastreio de teste no terminal (algo como
   `BR123456789ABC -> BR250800001`). Deixe essa janela aberta.
6. Abra o navegador em **http://localhost:5000**, digite seu nome, e vá em "Escanear".
   Cole um dos códigos de rastreio de teste para ver o fluxo completo: produto,
   variação, quantidade, dar OK ou marcar pendência. Teste também o PDF de
   pré-separação no dashboard.
7. Me conta o que achou — dá pra ajustar textos, cores, campos, o que for antes de
   ligar com dados reais.

Para parar o app, volte no Prompt de Comando e aperte `Ctrl+C`.


App web para o time de expedição separar pedidos sem precisar acessar a conta Shopee
(faturamento, dados do comprador etc). Eles só veem: quantidade de pedidos a separar,
o produto/variação/quantidade de cada pedido (via código de rastreio) e as abas de
pendências e concluídos.

## Como funciona o fluxo

1. Você sincroniza os pedidos "prontos para envio" da Shopee (botão no dashboard).
2. Você imprime as etiquetas normalmente, como já faz hoje.
3. O colaborador abre o app, digita o nome uma vez, e vai em **Escanear**.
4. Ele bipa/digita o código de rastreio que está na etiqueta física.
5. O sistema mostra o produto, a variação e a quantidade daquele pedido.
6. Ele separa o produto e clica em **"Separado, dar OK"** → o pedido some da fila e
   vai para **Concluídos** (com nome de quem confirmou e hora).
7. Se faltar item ou tiver algum problema, ele clica em **"Marcar pendência"** e descreve
   o problema → o pedido vai para a aba **Pendências**, e você decide o que fazer.
8. A qualquer momento você pode baixar o **PDF de pré-separação**: soma a quantidade
   de cada produto/variação de todos os pedidos ainda em aberto (a separar + pendentes),
   pronto para o time pegar os produtos do estoque antes de abrir pedido por pedido.

## Passo 1 — Criar as credenciais na Shopee (você precisa fazer isso)

Isso é obrigatório para o app buscar pedidos reais. Eu não consigo fazer por você porque
exige login na sua conta de vendedor e aprovação da Shopee. Status: Lucas já fez o
cadastro (Registered Business Seller) e está aguardando aprovação (até 3 dias úteis).

Quando aprovar:

1. No Console (open.shopee.com/console/app), crie um App na categoria
   **"App Seller In-House System"** (é a opção certa quando o próprio vendedor
   desenvolve para a própria loja).
2. Ao criar, cadastre a **Redirect URL** apontando para o link do seu app já publicado
   + `/shopee/callback` (ex: `https://seu-app.onrender.com/shopee/callback`). Por isso
   é melhor publicar o app (Passo 4) antes desse passo.
3. O Console mostra o **Partner ID** e a **Partner Key** — cole os dois no `.env`
   (existem versões Sandbox e Live, não misture).
4. Solicite o **Go-Live** quando quiser sair do modo teste.
5. Clique no botão **Authorize** dentro do Console para conectar sua própria loja —
   ele te redireciona para `/shopee/callback`, e o app já salva o token sozinho no
   banco de dados (ver seção abaixo). Não precisa copiar token manualmente.

## Passo 2 — Configurar o projeto

```bash
cd shopee-dashboard
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edite o `.env`:
- Enquanto não tiver as credenciais, deixe `USE_MOCK_DATA=true` — o app já roda com
  6 pedidos de exemplo (os códigos de rastreio de teste aparecem no terminal ao iniciar).
- Quando tiver Partner ID e Partner Key, preencha os dois e mude para
  `USE_MOCK_DATA=false`. Não precisa preencher Shop ID nem Access Token no `.env` —
  isso é salvo automaticamente no banco quando você clica em "Authorize" no Console
  da Shopee (ver Passo 1).

## Passo 3 — Rodar localmente para testar

```bash
python3 app.py
```

Abra `http://localhost:5000` no navegador. Em modo demo, use um destes códigos de
rastreio na tela de escaneamento: `BR123456789ABC`, `BR123456790ABC`, `BR123456791ABC`, etc.
(a lista completa aparece no terminal quando o app inicia).

## Passo 4 — Publicar num link para o time acessar (hospedagem)

Recomendo **Render.com** (tem plano gratuito, simples de configurar):

1. Crie uma conta gratuita em **github.com** (se ainda não tiver) e suba esta pasta
   como um repositório novo (dá para arrastar os arquivos direto pela interface web
   do GitHub, sem usar linha de comando, em "Add file → Upload files").
2. Crie uma conta gratuita em **render.com**.
3. No Render: **New → Web Service**, conecte o repositório do GitHub.
4. Build command: `pip install -r requirements.txt`
5. Start command: já está definido no arquivo `Procfile` (`gunicorn app:app`) — o Render
   detecta sozinho, não precisa preencher nada.
6. Em **Environment**, cadastre as mesmas variáveis do `.env` (Partner ID, Partner Key,
   Shop ID, Access Token, USE_MOCK_DATA=false, SECRET_KEY). Pode deixar
   `USE_MOCK_DATA=true` no começo e trocar só quando a Shopee aprovar seu cadastro.
7. Deploy. Você recebe um link tipo `https://seu-app.onrender.com` — é esse link que
   o time vai acessar pelo celular ou computador no galpão. Guarde esse link: é ele
   que você vai usar como Redirect URL ao criar o App na Shopee Open Platform
   (ex: `https://seu-app.onrender.com/shopee/callback`).

Alternativas equivalentes: Railway.app ou PythonAnywhere.

**Atenção ao banco de dados:** por padrão o app usa um arquivo SQLite local
(`dashboard.db`). No plano gratuito do Render, esse arquivo pode ser apagado a cada
novo deploy. Para uso contínuo, quando formos para produção de verdade, o ideal é trocar
por um banco Postgres gerenciado (o Render oferece um gratuito) — posso migrar isso
quando você chegar nessa etapa.

## O que o time NUNCA vê neste app

- Faturamento, valor da venda, forma de pagamento
- Dados do comprador (nome, endereço, telefone)
- Configurações da loja / conta Shopee

Só aparece: número interno do pedido, código de rastreio, produto, variação e
quantidade — o mínimo necessário para separar.

## Renovação automática do token (já implementado)

O access_token da Shopee expira em poucas horas. O app já cuida disso sozinho: toda
vez que você sincroniza, ele confere se o token está perto de expirar e renova
automaticamente usando o refresh_token guardado no banco. Você não precisa fazer nada
manualmente — só a autorização inicial pelo botão "Authorize" no Console.

A autorização da loja em si (o vínculo entre o App e a sua loja) dura até 365 dias;
depois disso, é só clicar em "Authorize" de novo no Console.
