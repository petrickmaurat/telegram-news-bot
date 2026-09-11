# Bot de notícias — Data Centers & Mercado de Carbono

Dois canais automáticos, rodando no GitHub Actions:

| Canal | Frequência | Conteúdo |
|---|---|---|
| **Telegram** (`telegram_news_bot.py`) | a cada 15 min | Notícias novas de **data center**, por palavras-chave e domínios permitidos |
| **E-mail** (`digest_email.py`) | 1x/dia, às 9h BRT | Digest curado por IA: até **3 do Brasil + 1 do exterior** por tema, com resumo |

## Onde configurar o quê

### Fontes e palavras-chave — `common.py`

`TOPICOS` define, para cada tema (`data_center`, `carbono`):

- **`keywords`** — a notícia precisa conter uma dessas expressões (título ou resumo) para ser considerada.
- **`feeds`** — lista de `{"url": ..., "origem": "BR" | "INT"}`. `origem` diz se o feed traz notícia do Brasil ou de fora (usado para separar "Brasil" e "Exterior").

O Telegram mantém a lista permitida de `reliability.py`, em `DOMINIOS`, e não usa IA.
No e-mail, os veículos de [FONTES_EMAIL.md](FONTES_EMAIL.md) têm prioridade de ordenação,
mas outros veículos também podem preencher vagas. O domínio final valida a prioridade;
um nome de fonte no RSS não basta. A configuração do e-mail fica em `email_sources.py`.

### Critério de seleção do digest — `digest_email.py`

- **`email_ranking.py` / `PROMPT`** — classifica e justifica cada candidata, sem limite por nota.
- **`email_sources.py` / `FONTES`** — catálogo único de veículos prioritários para o e-mail.
- **`FOCO_SETORIAL`** — o recorte de "setor elétrico" de cada tema.
- **`PROMPT_RESUMO`** — como o parágrafo de resumo é escrito.
- **`MAX_BR` / `MAX_US`** — quantas notícias por bucket (hoje 3 e 1).

### Visual do e-mail — `digest_email.py`, função `montar_html`

O comentário acima da função descreve o objetivo visual. Cores por tema em `TEMA`.

### Horários — `.github/workflows/`

- `news.yml` linha `cron` — Telegram
- `digest.yml` linhas `cron` — digest (horário em UTC; BRT = UTC−3)

## Secrets necessários (GitHub → Settings → Secrets and variables → Actions)

| Secret | Para quê |
|---|---|
| `TELEGRAM_TOKEN` | bot do Telegram (@BotFather) |
| `CHAT_ID` | chat de destino no Telegram |
| `ANTHROPIC_API_KEY` | filtro + resumo do digest (console.anthropic.com) |
| `BREVO_API_KEY` | envio do e-mail (brevo.com) |
| `EMAIL_REMETENTE` | endereço verificado na Brevo |
| `EMAIL_DESTINO` | quem recebe o digest |

## Estado e prevenção de duplicatas

- `enviados.json` — links já mandados no Telegram
- `digest_enviados.json` — novos registros são apenas de envios confirmados; os registros legados continuam sendo respeitados
- `google_cache.json` — cache de links do Google Notícias já resolvidos
- `digest_fila.json` — candidatos pendentes, enviados, rejeitados por tema ou expirados
- `telegram_incerto.json` / `digest_incerto.json` — envio em andamento ou com resultado incerto; `null` indica canal liberado

A fila conserva candidatos mesmo depois que saem do RSS. Todos os candidatos são avaliados em
lotes de até 30 (rodada base, com cache por tema e versão da regra — um lote malformado não
derruba o tópico, só fica pendente pra próxima execução). Se os elegíveis dessa rodada passarem
de 30, é um torneio: os melhores de cada lote (por nota) avançam para uma nova rodada de
comparação direta entre vencedores, sem cache, repetindo até caber numa chamada só — essa rodada
final decide nota e duplicidade com o contexto completo dos rivais. Quem não avança não é
descartado: continua elegível com a nota da última rodada em que participou, disponível como
reserva. Detalhes em `email_ranking.py`.
O e-mail só admite matérias publicadas
nas últimas **72 horas**, com tolerância de 15 minutos para relógios adiantados. A data vem de
`published` do RSS/Atom; `updated` não renova a idade. Sem data válida, a notícia não chega à IA
nem ao envio; pode receber uma data numa coleta posterior. Pendências expiram após sete dias
desde a coleta ou antes, quando a publicação sai da janela. Essas regras de data são exclusivas
do e-mail. Os limites ficam em `email_policy.py`.

Prioridade ordena, não exclui: o código preenche três vagas BR e uma Exterior por tema quando
há candidatos suficientes, atuais, inéditos, com links válidos e de fatos distintos. Não existe
corte mínimo de nota. Valores financeiros, regulação e setor elétrico aumentam a nota, mas sua
ausência não rejeita uma notícia do tema. Notícias de veículos não prioritários completam vagas.
A geografia é a do fato: CNN Brasil sobre Emirados é Exterior; Reuters sobre Brasil é BR.
Cada candidata precisa de decisão e motivo. Só há rejeição editorial por fora do tema, ausência
de informação substantiva/fato novo ou evidência de fonte duvidosa. Array vazio ou índices
faltantes são falhas técnicas, não rejeições. Uma falha em data centers não impede tentar carbono.
Se houver seleção válida em outro tema, ela é enviada; a execução sinaliza falha parcial no
Actions, preservando o histórico do que já foi enviado. Não selecionados continuam pendentes.

No **digest**, a coleta usa apenas o cache para links do Google (`resolver=False`). Cada execução
também consulta Google Notícias por `site:` para CADA veículo prioritário e CADA tema, com quatro
consultas simultâneas e registro de resultados/falhas. Isso complementa os feeds existentes;
não garante cobertura de páginas que o Google não indexa ou disponibiliza no RSS.
Os links dos elegíveis são resolvidos na ordem do ranking até preencher as vagas com as melhores
fontes; reservas continuam sendo usadas após falha/duplicidade, sem o antigo limite de três rodadas.
Não são resolvidos links que já não podem superar vagas preenchidas por fontes prioritárias.
Falha de resolução preserva o candidato para outra execução. O **Telegram** mantém a resolução imediata.
URLs equivalentes
(Google/original, `www`, fragmentos e parâmetros de rastreamento conhecidos) compartilham
identidade. Notícias associadas aos dois temas participam de ambos até serem escolhidas.
A identificação do mesmo fato em textos/URLs diferentes ainda depende da curadoria da IA;
a normalização de URLs não garante deduplicação semântica.

Registros concluídos da fila perdem o título/resumo após sete dias e são removidos após 30 dias
do encerramento. A lista de aliases enviados em `digest_enviados.json` permanece para impedir
reenvios; registros descartados removidos não contornam o filtro de data se reaparecerem no RSS.

O HTML do e-mail escapa títulos, fontes, resumos, links e a data de edição. Links inválidos ou
Google Notícias não resolvidos impedem a montagem. Resumos não têm mínimo obrigatório de palavras:
com menos de 20 palavras de apoio, o trecho/título é usado diretamente, sem expansão pela IA;
com mais conteúdo, o modelo recebe instruções para resumir somente fatos fornecidos.

### Por que uma notícia não entrou?

Cada e-mail inclui `curadoria.txt`, com resultado e motivo por notícia e fontes consultadas.
O mesmo diagnóstico fica em `digest_auditoria.json`, versionado no GitHub e nos artefatos do
workflow, inclusive quando nada foi enviado. O resultado distingue: selecionada, sem vaga,
duplicada, já enviada, fora da janela, sem data, rejeição editorial e falha técnica.
As avaliações recentes rejeitadas pela regra anterior voltam a ser consideradas. A justificativa
antiga não pode ser recuperada: não foi registrada. Os novos motivos correspondem à nova avaliação.
O relatório é uma justificativa editorial estruturada da IA, não comprovação independente de seus juízos.

Os workflows compartilham um grupo de concorrência por branch, preservam a fila de execuções
e fazem checkout da versão atual da branch. No Actions, `BOT_PERSIST_STATE=1` grava um marcador
no remoto **antes** de cada POST e persiste o resultado depois. Se o push anterior ao POST
falhar, o envio não acontece. Há até três tentativas de push e uma etapa final de persistência
mesmo em falhas. Artefatos de recuperação ficam disponíveis por 14 dias quando a execução falha.
Essa proteção gera commits adicionais por envio. Evite executar cópias locais ou branches
diferentes simultaneamente contra os mesmos destinatários: o lock do Actions é por branch.

### Recuperar um envio incerto

Timeout, interrupção ou resposta ambígua podem ocorrer depois de o serviço aceitar um envio.
O canal fica bloqueado para não repetir automaticamente. Confira a mensagem no Telegram ou
o assunto/horário nos logs transacionais da Brevo; o arquivo `*_incerto.json` contém os itens.
Em um checkout atualizado, execute apenas a opção que corresponde ao resultado verificado:

```sh
python resolve_delivery.py telegram --entregue
python resolve_delivery.py telegram --nao-entregue
# Para e-mail, substitua telegram por digest.
```

Depois faça commit/push dos arquivos de estado alterados antes de reexecutar o workflow.
`--entregue` registra os aliases como enviados; `--nao-entregue` permite uma nova tentativa.
Se não for possível confirmar o resultado, mantenha o bloqueio. O script não envia mensagens.
Os históricos antigos não permitem distinguir descartes de envios reais; a migração não apaga
esses registros, evitando uma onda de reenvios. Notícias descartadas pelo código antigo não
são recuperadas automaticamente.

## Testes

```sh
python -m unittest discover -s tests
```

Os testes simulam APIs e usam estado temporário; não enviam mensagens nem consomem IA.
Também rodam antes do processamento nos workflows.

## Rodar localmente

```
pip install -r requirements.txt
TELEGRAM_TOKEN=... CHAT_ID=... python telegram_news_bot.py
ANTHROPIC_API_KEY=... BREVO_API_KEY=... EMAIL_REMETENTE=... EMAIL_DESTINO=... python digest_email.py
```
