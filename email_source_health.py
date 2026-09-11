"""Histórico de consultas por fonte e tema, sem remover fontes por baixo volume."""
from reliability import carregar_json, salvar_json

JANELA = 30 * 86400


def anotar_resultados(consultas, fila):
    for row in consultas:
        relacionados = [r for r in fila.values() if row["consulta"] in r["item"].get("feeds_origem", [])
            and row["topico"] in r["item"].get("topicos", [])]
        row["selecionadas"] = sum(r.get("resultados", {}).get(row["topico"], {}).get("resultado") == "selecionada"
                                  for r in relacionados)
        row["elegiveis"] = sum(r.get("resultados", {}).get(row["topico"], {}).get("resultado") in ("selecionada", "sem_vaga", "duplicada")
                              for r in relacionados)


def atualizar(path, consultas, agora):
    historico = carregar_json(path, {"execucoes": []})
    execucoes = [e for e in historico.get("execucoes", []) if agora - e["em"] < JANELA]
    if consultas:
        execucoes.append({"em": agora, "consultas": consultas})
    # Protege também contra muitas execuções manuais no mesmo dia.
    execucoes = execucoes[-120:]
    fontes = {}
    for execucao in execucoes:
        for row in execucao["consultas"]:
            chave = (row["topico"], row["fonte"], row["consulta"])
            stats = fontes.setdefault(chave, {"topico": row["topico"], "fonte": row["fonte"],
                "consulta": row["consulta"], "consultas": 0, "falhas": 0, "itens_rss": 0,
                "capturados": 0, "mesclados": 0, "fora_keywords": 0, "vazias_seguidas": 0,
                "selecionadas": 0, "elegiveis": 0})
            stats["consultas"] += 1
            falha = row["resultado"] != "ok"
            stats["falhas"] += int(falha)
            stats["itens_rss"] += row["itens_rss"]
            stats["selecionadas"] += row.get("selecionadas", 0)
            stats["elegiveis"] += row.get("elegiveis", 0)
            stats["vazias_seguidas"] = stats["vazias_seguidas"] + 1 if not falha and row["itens_rss"] == 0 else 0
            for campo in ("capturados", "mesclados", "fora_keywords"):
                stats[campo] += row.get("contagem", {}).get(campo, 0)
            if row["itens_rss"]:
                stats["ultimo_resultado_em"] = execucao["em"]
    for stats in fontes.values():
        if stats["falhas"] / stats["consultas"] >= 0.5:
            stats["acao"] = "Verificar acesso e validade do feed; pelo menos metade das consultas falhou."
        elif stats["vazias_seguidas"] >= 3:
            stats["acao"] = "Conferir cobertura no site e disponibilidade de RSS direto; três ou mais consultas vazias não provam ausência de publicações."
        elif stats["itens_rss"] and stats["fora_keywords"] / stats["itens_rss"] >= 0.8:
            stats["acao"] = "Revisar amostra dos termos descartados; feed geral pode conter muitos assuntos diferentes."
        else:
            stats["acao"] = "Acompanhar; sem indício suficiente para alterar a busca."
    resultado = {"gerado_em": agora, "execucoes": execucoes, "fontes": list(fontes.values())}
    salvar_json(path, resultado)
    return resultado["fontes"]
