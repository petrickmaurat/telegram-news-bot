"""Distingue saldo insuficiente de rejeição editorial ou falha transitória."""
class SaldoInsuficiente(RuntimeError):
    pass


def verificar_saldo(erro):
    if isinstance(erro, SaldoInsuficiente) or "credit balance is too low" in str(erro).lower():
        raise SaldoInsuficiente("Saldo insuficiente na API Anthropic; edição interrompida sem envio. Candidatos preservados.") from erro
