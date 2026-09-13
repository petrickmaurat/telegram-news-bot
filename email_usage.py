"""Medição de respostas da API; não armazena prompts, textos ou credenciais."""
import json
import os
from contextvars import ContextVar

_eventos = ContextVar("email_usage", default=None)


def iniciar():
    _eventos.set([])


def criar_mensagem(cliente, etapa, topico=None, **kwargs):
    eventos = _eventos.get()
    registro = {"etapa": etapa, "topico": topico, "modelo": kwargs.get("model")}
    try:
        resposta = cliente.messages.create(**kwargs)
    except Exception:
        if eventos is not None:
            registro.update(status="erro", custo_estimado_usd=None)
            eventos.append(registro)
            print("USO_IA " + json.dumps(registro, ensure_ascii=False))
        raise
    uso = getattr(resposta, "usage", None)
    campos = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    valores = {k: getattr(uso, k, None) for k in campos}
    medido = all(type(valores[k]) is int and valores[k] >= 0 for k in campos[:2])
    registro.update(status="resposta", medido=medido, custo_estimado_usd=None)
    if medido:
        registro.update({k: v if type(v) is int else 0 for k, v in valores.items()})
        # Tarifas padrão Haiku 4.5. Cache de 1h deve ter tarifa própria;
        # sem detalhe de duração, não inventar valor para cache de escrita.
        if kwargs.get("model") == "claude-haiku-4-5" and not registro["cache_creation_input_tokens"]:
            registro["custo_estimado_usd"] = (
                registro["input_tokens"] + 5 * registro["output_tokens"]
                + .1 * registro["cache_read_input_tokens"]) / 1_000_000
    if eventos is not None:
        eventos.append(registro)
        print("USO_IA " + json.dumps(registro, ensure_ascii=False))
    return resposta


def relatorio():
    eventos = _eventos.get() or []
    return {"run_id": os.getenv("GITHUB_RUN_ID"), "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "chamadas": list(eventos),
            "custo_estimado_usd": round(sum(e.get("custo_estimado_usd") or 0 for e in eventos), 8),
            "chamadas_sem_custo_medido": sum(e.get("custo_estimado_usd") is None for e in eventos),
            "nota": "Estimativa das respostas observadas; erros e tentativas internas do SDK podem não ter uso disponível."}
