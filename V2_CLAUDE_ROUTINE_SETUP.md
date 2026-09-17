# Configuração manual do piloto V2

A V1 permanece na branch `main`. O piloto vive em `v2-claude-routines`, usa
estado separado e não possui comando de envio de e-mail.

## Antes do primeiro teste

1. Abra `https://claude.ai/code/routines` com a conta Claude Pro.
2. Confirme em Settings > Usage que uso adicional/extra está desativado.
   Assim, atingir o limite interrompe a Routine em vez de gerar cobrança extra.
3. Crie uma Routine e conecte o repositório `telegram-news-bot`.
4. As instruções abaixo mandam a Routine abrir `v2-claude-routines`; a interface
   web não possui seletor de branch.
5. Use execução manual/one-off no primeiro teste. Não crie agenda diária ainda.
6. No ambiente da Routine, habilite acesso de rede aos feeds e artigos usados
   pelo projeto. Para o piloto, não configure `ANTHROPIC_API_KEY`, `BREVO_API_KEY`,
   `EMAIL_REMETENTE` nem `EMAIL_DESTINO`: ele não precisa desses secrets.

## Prompt para colar na Routine

```text
Trabalhe a partir da versão v2-claude-routines deste repositório. No início,
execute `git fetch origin v2-claude-routines` e `git checkout
v2-claude-routines`. Leia e siga integralmente ROUTINE_V2_INSTRUCTIONS.md.
Este é um piloto sem envio: nunca execute digest_email.py, não envie e-mail,
não altere main nem arquivos digest_*.json e não use ANTHROPIC_API_KEY.

Faça primeiro `python routine_v2.py preflight`. Se falhar, pare imediatamente,
informe o diagnóstico e não execute mais chamadas de rede. Se passar, execute a
coleta completa com `python routine_v2.py prepare --max-new-per-topic 0`, faça
ranking e resumos, valide as duas etapas e finalize. Não mude o validador nem
relaxe as regras.

Ao final, crie uma branch nova com prefixo `claude/v2-pilot-`, grave somente os
artefatos autorizados em ROUTINE_V2_INSTRUCTIONS.md e faça um único push dessa
branch. Nunca faça push direto para v2-claude-routines ou main. Se o push falhar,
pare sem criar agentes auxiliares ou tentar contornar pela API; apresente o
relatório completo na sessão.
```

## O que anotar no primeiro teste

Antes de executar, registre uma captura ou os números de uso do Claude em
Settings > Usage. Depois do run, anote novamente:

- percentual da janela de cinco horas, se exibido;
- percentual/quantidade do limite semanal, se exibido;
- horário inicial e final;
- quantidade `needs_evaluation`, `cached` e `counts` do relatório;
- se a prévia preencheu as vagas e se as matérias são editorialmente corretas.

Este teste processa todos os candidatos. O preflight impede que uma falha de
rede seja confundida com um dia sem notícias ou consuma a franquia no ranking.

## Critério para avançar

Não agende nem habilite envio após um único teste. Faça um segundo run manual em
outro dia para medir o custo com cache. Só avance se ambos forem verdadeiros:

1. a seleção e os resumos mantêm a qualidade do digest atual;
2. o consumo diário deixa margem confortável para seu uso normal do Claude Pro.

Depois disso, a próxima etapa de desenvolvimento adicionará envio e confirmação
de entrega à V2. Só então o cron da V1 deverá ser desativado, evitando dois e-mails.
