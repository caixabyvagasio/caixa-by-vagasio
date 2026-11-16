# Caixa by VagasIO — base funcional V1

Esta versão concentra a primeira etapa de varejo geral em ambiente de desenvolvimento e demonstração.

## Implementado

- API FastAPI e interface web responsiva.
- Provisionamento inicial protegido por chave bootstrap; credenciais com PBKDF2 e sessão Bearer assinada.
- Isolamento por empresa, gerenciamento de usuários e autorização server-side.
- Produtos, saldo e movimentos de estoque justificados e auditáveis.
- Abertura, suprimento, sangria, venda em dinheiro, fechamento e conciliação do caixa.
- Idempotência na venda para evitar duplicidade.
- Demonstração opt-in com tenant e catálogo inteiramente fictícios. O login demo é compartilhado e não deve conter informação real.
- Testes automatizados para fluxos principais e fronteiras multiempresa.

## Limitações

O serviço hospedado é uma demonstração pública no plano gratuito, com SQLite em filesystem efêmero. Pode adormecer, reiniciar ou perder dados a qualquer momento. Não há persistência, backup, recuperação, suporte de produção ou garantia de disponibilidade. Não usar para atividade comercial nem inserir dados pessoais/reais.

Ainda não existem pagamentos eletrônicos/Pix, emissão fiscal, periféricos, multifilial, cadastro empresarial/CNPJ, assistente de implantação, planos/módulos comerciais, devolução, compras/fornecedores ou operação offline. A demonstração é compartilhada; qualquer visitante pode alterar os dados fictícios e esses dados podem sumir.

## Rodar localmente

Python 3.11+, `pip install -r requirements.txt`, variáveis locais fortes `CAIXA_TOKEN_SECRET`, `CAIXA_BOOTSTRAP_SECRET`; execute `uvicorn app.main:app --host 127.0.0.1 --port 8000`. Para os testes: `PYTHONPATH=. pytest -q`.

`CAIXA_DEMO_MODE=true` habilita a conta fictícia; mantenha-o desligado em qualquer ambiente que use dados de clientes.
