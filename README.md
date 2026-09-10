# Bot de notícias — Data Centers & Mercado de Carbono

Dois canais automáticos, rodando no GitHub Actions:

| Canal | Frequência | Conteúdo |
|---|---|---|
| **Telegram** (`telegram_news_bot.py`) | a cada 15 min | Toda notícia nova de **data center** (Brasil + exterior), em tempo real |
| **E-mail** (`digest_email.py`) | 2x/dia (7h e 18h BRT) | Digest curado por IA: **3 do Brasil + 1 do exterior** por tema (data center e mercado de carbono), com resumo em parágrafo |

## Onde configurar o quê

### Fontes e palavras-chave — `common.py`

`TOPICOS` define, para cada tema (`data_center`, `carbono`):

- **`keywords`** — a notícia precisa conter uma dessas expressões (título ou resumo) para ser considerada.
- **`feeds`** — lista de `{"url": ..., "origem": "BR" | "INT"}`. `origem` diz se o feed traz notícia do Brasil ou de fora (usado para separar "Brasil" e "Exterior").

Para adicionar um tema novo, é só acrescentar outra entrada em `TOPICOS` com o mesmo formato.

### Critério de seleção do digest — `digest_email.py`

- **`PROMPT_SELECAO`** — instrução para a IA escolher e ranquear. Critérios, na ordem de peso: montante financeiro → impacto regulatório/político → ângulo de setor elétrico → veículo que publicou. Prioridade é ordenação, não exclusão.
- **`VEICULOS_PRIORITARIOS`** — lista de veículos de referência que pesam mais no ranking.
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

## Arquivos de estado (não editar à mão)

- `enviados.json` — links já mandados no Telegram
- `digest_enviados.json` — links já considerados pelo digest
- `google_cache.json` — cache de links do Google Notícias já resolvidos

## Rodar localmente

```
pip install -r requirements.txt
TELEGRAM_TOKEN=... CHAT_ID=... python telegram_news_bot.py
ANTHROPIC_API_KEY=... BREVO_API_KEY=... EMAIL_REMETENTE=... EMAIL_DESTINO=... python digest_email.py
```
