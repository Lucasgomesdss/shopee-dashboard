"""Frase motivacional do dia, mostrada na tela de Escanear pro time (formado por
mulheres). Uma frase por dia -- a mesma o dia todo, muda sozinha no dia seguinte
(baseado na data, sem precisar guardar nada no banco)."""

from datetime import date

PHRASES = [
    "Você é capaz de coisas incríveis — hoje é mais um dia pra provar isso.",
    "Mulher forte não é a que nunca cansa, é a que continua mesmo cansada. Você consegue!",
    "Seu esforço de hoje é a base do seu sucesso de amanhã. Vai com tudo!",
    "Você não está aqui por acaso — está aqui porque é competente e merece.",
    "Cada pedido separado com cuidado é prova do seu profissionalismo. Orgulhe-se!",
    "Determinação é o seu superpoder. Use-o hoje também.",
    "Você inspira só de estar aqui, dando o seu melhor todos os dias.",
    "O mundo precisa de mais mulheres como você: fortes, dedicadas e imparáveis.",
    "Hoje é um novo dia pra mostrar do que você é capaz. Bora!",
    "Sua dedicação não passa despercebida. Continue brilhando.",
    "Você é a prova de que competência não tem gênero, tem atitude.",
    "Grandes conquistas começam com pequenos passos — e você está dando o seu agora.",
    "Confie em você: sua força já te trouxe até aqui.",
    "Cada dia de trabalho é uma oportunidade de crescer. Aproveite o seu!",
    "Você é capaz, é competente e está no lugar certo.",
    "Nenhum obstáculo é grande demais para quem tem a sua garra.",
    "Seja gentil consigo mesma hoje — você está se saindo muito bem.",
    "Sua energia e dedicação fazem toda a diferença no time.",
    "Você merece reconhecimento por tudo que faz, inclusive o que ninguém vê.",
    "Comece o dia lembrando: você já superou 100% dos seus dias difíceis até aqui.",
    "Ser mulher e trabalhadora é ter uma força que poucos entendem. Você tem essa força.",
    "Seu trabalho importa, e você faz ele com excelência todos os dias.",
    "Acredite: sua persistência hoje vai valer a pena amanhã.",
    "Você é referência de dedicação para quem está ao seu redor.",
    "Um dia de cada vez, um pedido de cada vez — você está construindo algo grande.",
    "Sua força silenciosa move muita coisa por aqui. Obrigada por ser você.",
    "Você não precisa ser perfeita, só precisa continuar tentando. E você continua.",
    "O sucesso de hoje começa com a atitude que você tem agora. Vai dar tudo certo!",
    "Mulheres como você mudam o jogo todos os dias, mesmo nas tarefas simples.",
    "Respire, sorria e lembre: você é mais capaz do que imagina.",
]


def get_daily_phrase():
    """Escolhe uma frase de forma determinística baseada na data -- a mesma frase
    aparece o dia inteiro pra quem acessar, e muda sozinha no dia seguinte."""
    index = date.today().toordinal() % len(PHRASES)
    return PHRASES[index]
