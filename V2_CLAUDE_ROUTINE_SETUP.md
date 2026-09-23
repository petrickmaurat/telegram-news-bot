# Teste do piloto V2 com a franquia do Claude Pro

A V1 continua na branch `main`, com seu agendamento e envio atuais. O piloto
vive em `v2-claude-routines`, usa estado separado e não envia e-mail.

O teste tem duas etapas. O GitHub Actions coleta as notícias sem IA e publica
um arquivo de entrada. Depois, a Claude Routine usa a franquia da assinatura
somente para classificar, escolher e resumir as notícias.

## 1. Preparar a entrada no GitHub

1. Abra o repositório no GitHub e entre em **Actions**.
2. Escolha **Preparar entrada V2 (sem IA)**.
3. Clique em **Run workflow** e aguarde o ícone verde.
4. Confirme que a branch `v2-claude-routines` recebeu um commit chamado
   `Prepara entrada V2 sem IA` e contém `routine_v2_input.json`.

Essa etapa não usa Claude, não usa `ANTHROPIC_API_KEY` e não envia e-mail. Ela
pode demorar porque resolve links e lê artigos de várias fontes.

## 2. Executar a curadoria no Claude

Antes do run, abra **Settings > Usage** e anote ou fotografe:

- percentual da sessão de cinco horas;
- percentual do limite semanal;
- horário dos próximos reinícios.

Na Routine conectada ao repositório, use uma execução manual e cole:

```text
Trabalhe a partir da versão v2-claude-routines deste repositório. No início,
execute `git fetch origin v2-claude-routines` e `git checkout
v2-claude-routines`. Leia e siga integralmente ROUTINE_V2_INSTRUCTIONS.md.

Este é um piloto sem envio: nunca execute digest_email.py, não envie e-mail,
não altere main nem arquivos digest_*.json e não use ANTHROPIC_API_KEY. A
coleta já foi feita pelo GitHub Actions: não acesse feeds ou sites de notícias,
não execute preflight nem prepare. Execute primeiro
`python routine_v2.py load-input --max-age-hours 6`; depois siga os passos de
lotes com subagentes, seleção, resumos e finalize. Não leia o
ranking_request.json inteiro. Não mude nem afrouxe os validadores.

Ao final, grave na branch v2-claude-routines somente os três artefatos
autorizados em ROUTINE_V2_INSTRUCTIONS.md e faça uma única tentativa de push.
Nunca escreva em main. Se o push falhar, pare sem criar agentes para contornar
o push nem usar a API; apresente no chat o relatório, os títulos, as fontes
e os resumos.
```

Depois que a Routine terminar, atualize **Settings > Usage** e registre de novo
os dois percentuais. O consumo do teste é a diferença entre antes e depois.

## Como interpretar o consumo

O uso da janela de cinco horas pode ser alto se a rotina rodar por volta das
7h e o limite reiniciar antes do horário de trabalho. O limite semanal também
é consumido e não volta quando a janela de cinco horas reinicia.

Se uma execução aumentar o semanal em `W` pontos percentuais, sete execuções
semelhantes consumiriam aproximadamente `7 × W` pontos por semana. A interface
arredonda percentuais, então faça dois testes em dias diferentes: o primeiro
mede a carga inicial e o segundo mede o dia normal com cache.

Para este piloto, sucesso significa:

- a prévia mantém a qualidade editorial e preenche as vagas quando há matérias;
- o uso da janela de cinco horas cabe antes do trabalho;
- a projeção semanal deixa margem para seu uso normal do Claude.

Mesmo que o GitHub App ainda recuse o push, a primeira curadoria e a medição de
consumo continuam válidas: confira o relatório mostrado na própria sessão. O
push é necessário para que o segundo teste reaproveite as avaliações e represente
o custo diário com cache. O envio só será habilitado depois desses dois testes.
