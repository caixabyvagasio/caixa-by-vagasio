# Financeiro gerencial — incremento de desenvolvimento

Estado: código commitado em `development-v1`, CI validado; ainda não publicado. Não é contabilidade, extrato bancário, conciliação nem confirmação de pagamentos.

## Endpoints
- `POST /v1/finance/expenses`: owner/admin/manager, despesa vinculada como conta a pagar; exige `Idempotency-Key`. Body: `description`, `amount`, `due_date`, opcional `cost_center`, e opcional `payment_date`/`payment_reference` para declarar baixa manual na criação. Mesmo key + mesmo payload retorna o mesmo item (HTTP 200); mesmo key/diferente payload retorna 409. Conta vencida pode ser registrada e aparece vencida; data de baixa futura é rejeitada.
- `POST /v1/finance/payables/{id}/payments`: owner/admin/manager; recebe amount positivo, data de pagamento não futura e referência/justificativa, além de chave idempotente. Registra pagamento parcial append-only. Não permite superar o saldo; marca conta paga somente quando total alcança valor da conta. Reuso exato retorna HTTP 200 sem duplicar; chave reutilizada com payload diferente retorna 409.
- `GET /v1/finance/payables`: inclui valor original, total pago, saldo, vencimento, situação e centro de custo.
- `GET /v1/finance/summary`: total de vendas pagas em dinheiro e obrigações a pagar restantes (inclui baixas parciais).
- `GET /v1/finance/cashflow?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`: agrupa entradas de vendas em dinheiro pagas e saídas por baixa manual declarada. Máximo 366 dias; tenant-scoped; roles financeiros. Datas refletem data UTC dos registros e datas informadas pelo usuário.

## Persistência e trilha
`payable_payments` é append-only por triggers SQLite, indexada por empresa/conta e com unicidade de `company_id + idempotency_key`. Expense detail armazena centro de custo e hash/idempotency key. Startup executa backfill idempotente de contas antigas com status `paid` para o ledger, preservando nota e data registradas, com chave estável por obrigação. Baixa tradicional `/mark-paid` também registra um pagamento manual total no ledger. Cada operação inclui evento de auditoria na transação.

## Interface
A tela financeira inclui formulário de despesa, tabela de fluxo por período e mostra total pago/saldo em contas. Pagamento parcial ou quitação envia chave idempotente e pede referência; todos os textos informam que baixa manual não é confirmação bancária.

## Limites deste incremento
SQLite e tabelas criados pelo schema inline em startup; não substitui migrations versionadas nem banco durável. Centro de custo é rótulo livre e ainda não há cadastro/relatório consolidado de centros. Contas a receber, parcelamento de recebíveis, gateway, conciliação, DRE, importação de extrato e pagamentos integrados permanecem pendentes. Confirmar transação bancária exige provedor autorizado e homologação.

## Testes
Testes de API cobrem isolamento tenant, baixa parcial/full, replay idempotente, conflito de chave, não sobrepagamento, anotação manual, imutabilidade do ledger, idempotência da criação de despesa, cashflow com venda e saída, migração idempotente, roles e fluxo. Suíte local passou; CI é critério adicional. Sem runtime Node/browser E2E nesta sessão.