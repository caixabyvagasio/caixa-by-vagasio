# Financeiro gerencial — incremento de desenvolvimento

Estado: código e testes em branch `development-v1`; ainda não publicado. Não é contabilidade, extrato bancário, conciliação nem confirmação de pagamentos.

## Endpoints
- `POST /v1/finance/expenses`: owner/admin/manager, despesa vinculada como conta a pagar; exige `Idempotency-Key`. Body: `description`, `amount`, `due_date`, opcional `cost_center`, e opcional `payment_date`/`payment_reference` para declarar baixa manual na criação. Mesmo key + mesmo payload retorna o mesmo item (HTTP 200); mesmo key/diferente payload retorna 409. Conta vencida pode ser registrada e aparece vencida; data de baixa futura é rejeitada.
- `POST /v1/finance/payables/{id}/payments`: owner/admin/manager; recebe amount positivo, data de pagamento não futura e referência/justificativa, além de chave idempotente. Registra pagamento parcial append-only. Não permite superar o saldo; marca conta paga somente quando total alcança valor da conta. Reuso exato retorna HTTP 200 sem duplicar; chave reutilizada com payload diferente retorna 409.
- `GET /v1/finance/payables`: inclui valor original, total pago, saldo, vencimento, situação e centro de custo.
- `GET /v1/finance/summary`: total de vendas pagas em dinheiro atual e obrigações a pagar restantes (inclui baixas parciais).
- `GET /v1/finance/cashflow?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`: agrupa entradas de vendas em dinheiro pagas e saídas por baixa manual declarada. Máximo 366 dias; tenant-scoped; roles financeiros. Datas refletem data UTC dos registros e datas informadas pelo usuário.

## Persistência e trilha
`payable_payments` é append-only por triggers SQLite, indexada por empresa/conta e com unicidade de `company_id + idempotency_key`. Expense detail armazena centro de custo e hash/idempotency key. Baixa tradicional `/mark-paid` também registra um pagamento manual total no ledger. Cada operação inclui evento de auditoria na transação.

## Interface
A tela financeira inclui formulário de despesa, tabela de fluxo por período e mostra total pago/saldo em contas. Pagamento parcial ou quitação envia chave aleatória e pede referência; todos os textos informam que baixa manual não é confirmação bancária.

## Limites deste incremento
SQLite e tabela criados pelo schema inline em startup; não substitui migrations versionadas nem banco durável. Centro de custo é rótulo livre e ainda não há cadastro/relatório consolidado de centros. Contas a receber, parcelamento de recebíveis, gateway, conciliação, DRE, importação de extrato e pagamentos integrados permanecem pendentes. Confirmar transação bancária exige provedor autorizado e homologação.

## Testes
Testes de API cobrem isolamento tenant, baixa parcial/full, replay idempotente, conflito de chave, não sobrepagamento, paid/balance, trilha append-only, data de baixa futura, papéis e fluxo. Reexecutar `pytest -q tests` no repo. CI é critério adicional; este documento não afirma runtime browser E2E nem produção.