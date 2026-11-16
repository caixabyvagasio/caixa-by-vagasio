# Financeiro gerencial — incremento em desenvolvimento

Estado: código em `development-v1`; não publicado. Não é contabilidade, extrato bancário, conciliação nem confirmação de pagamentos.

## Funcionalidades/rotas
- `POST /v1/finance/expenses`: owner/admin/manager; cria despesa como conta a pagar, com centro de custo livre. Exige `Idempotency-Key`; replay do mesmo payload retorna o item existente (200), chave reutilizada com outro payload retorna 409. Despesa vencida é permitida e fica em aberto/vencida. Pode declarar baixa manual total opcional na criação.
- `POST /v1/finance/payables/{id}/payments`: baixa parcial ou total declarada manualmente; valor, data não futura, referência e chave idempotente. Não permite pagamento acima do saldo. A obrigação muda para paga só quando quitada. Replays idênticos não duplicam; payload diferente na mesma chave retorna 409.
- `GET /v1/finance/payables`: valor total, valor pago, saldo, vencimento, status e centro de custo.
- `GET /v1/finance/summary`: vendas marcadas pagas (no modelo atual, somente dinheiro) e obrigações restantes após parcelas.
- `GET /v1/finance/cashflow?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`: totais diários de vendas em dinheiro e baixas manuais separados. Período até 366 dias, tenant-scoped, somente roles financeiras.
- UI financeira: formulário de despesa, listagem com pago/saldo, ações de parcela/quitação e fluxo por data. Mensagens deixam explícito que baixa manual não prova transação bancária.

## Persistência e migrações
`payable_payments` é append-only por triggers SQLite, FK e unicidade por empresa/chave; expense detail armazena centro de custo e payload idempotente. Baixas antigas registradas no campo `paid` são migradas ao ledger preservando referência/data. Migrações versionadas 1–4 agora registram customer_id, metadados de produto, índice barcode e backfill de contas pagas. Migrações executam uma só vez com registro `schema_migrations` e transação; testes cobrem reexecução, rollback idempotente e base legada.

## Limites
Backend continua SQLite, schema inicial inline; isto não cria persistência durável e não substitui suporte/migração PostgreSQL e backup/restore. Centro de custo ainda é rótulo, sem catálogo. Contas a receber, parcelamento na venda, conciliação, DRE, importação de extrato, gateway e pagamentos integrados permanecem pendentes. Dados demonstrativos não são dados reais.

## Validação
Suíte local completa deste commit: 50 testes aprovados, 1 aviso de depreciação Starlette TestClient/httpx. Sem runtime Node/browser E2E nesta sessão; UI passou checagem estática de IDs/formulários, não revisão visual. Validar CI do commit antes de considerar homologado.