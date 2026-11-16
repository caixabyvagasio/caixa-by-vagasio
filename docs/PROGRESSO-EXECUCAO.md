# Progresso de execução — auditoria e correções prioritárias

Data: 2026-11-16. Branch alvo `development-v1`; commits recentes nos links do repositório.

## Estado herdado e auditoria
- Base inicial `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`: FastAPI/Python, SQLite, HTML/CSS/JS, 39 testes; Render Free e banco `/tmp/caixa.sqlite3`, cadastro público ligado e demo mutável, sem DB durável/backup. Relatório detalhado em `PLANO-AUDITORIA-E-EXECUCAO.md`.
- Funcionalidades anteriores: organização/usuário, catálogo/estoque, compras/recebimento idempotente, clientes, caixa/venda só dinheiro, estorno manual, contas a pagar básicas, perfil sugerido por segmento e relatórios operacionais; não são V1 comercial completa.

## Hardening anterior
- `cc9266726412e5db42b14347d33c5a7c569b64c6`: signup bloqueado em demo e PII/histórico clientes sob RBAC. CI `43913857996` success. Deployado ao Render Free de desenvolvimento `dep-dbtjga88v5cc7396nd3g`; não produção. Health/home HTTP 200, signup HTTP 404.
- `292e336139e358b9b9e7c3652293a7a63a1bdcdd`: stock não cria/altera catálogo/preços; CI `43914431760` success.
- `fe4faba5f6305769c6d91773a94d1dbe2fb45a6b`: RBAC adicional, UI cliente oculta PII ao operador, valorização sem overflow; testes e CI `43916794723` success. Esse commit não foi deployado.

## Incremento financeiro — commitado e CI success
- Ledger `payable_payments` append-only, idempotente por empresa+chave, FKs e triggers para não permitir update/delete; detalhe de despesa guarda centro de custo e request hash.
- Backfill idempotente no startup para contas antigas já marcadas pagas, preserva nota/data; teste chama inicialização duas vezes.
- `POST /v1/finance/expenses`: cria conta a pagar e centro de custo livre; exige chave idempotente, replay igual reutiliza, payload diferente 409; vencidas continuam registradas. Opcional baixa manual declarada na criação, sem alegação de banco.
- `POST /v1/finance/payables/{id}/payments`: parcelas manuais idempotentes, data não futura, não pode exceder saldo, auditoria; muda a conta para paga quando quitada. O endpoint anterior de baixa total agora também grava pagamento no ledger.
- Listagem inclui pago/saldo/centro; summary calcula obrigações abertas pelo saldo; cashflow mostra vendas pagas em dinheiro e baixa manual por dia em séries separadas, com janela máxima de 366 dias e isolamento de empresa.
- Portal financeiro inclui formulário para despesa, lista com parcela/saldo e consulta de fluxo por data. Interface mantém chave idempotente do formulário enquanto a requisição falha e do pagamento; deixa claro que baixas são manuais.
- Testes API cobrem isolamento de tenant/papel, despesas, replays/conflitos, parcelas, excesso, ledger append-only, migração histórica, vendas e baixas no fluxo. Última suíte: **49 passed, 1 warning conhecido** de depreciação Starlette TestClient/httpx. AST Python OK; referências de IDs sem faltantes e 15 formulários balanceados. Não havia runtime Node/Deno/Bun, logo sem checagem de sintaxe JS por runtime ou E2E visual.
- Guia: `docs/FINANCEIRO-PARCIAL-V1.md`.

## Estado Git e próximos passos
- Incremento financeiro commit `ef082c4a41156a37f07d33188a6e387befda52d4` na branch `development-v1`. CI run `43950327110` success.
- Documento de progresso atualizado depois do CI, sem alterar código. O bloco financeiro foi commitado, CI passou e **não foi deployado**, respeitando ordem de não publicar sem autorização.
- Não publicar o bloco financeiro sem autorização expressa. Não é ambiente de produção.
- Próximas tarefas executáveis: migrations versionadas e suporte PostgreSQL sem contratar serviço, Portal Master e filiais; persistência/backup comercial depende de provider/custo aprovado.
- Ainda faltam recebíveis/parcelamento na venda, conciliação, DRE, centros de custo estruturados, Master, entitlements reais, pagamentos/fiscal, offline e módulos especializados.
- Demo Render permanece SQLite em `/tmp`, dados fictícios compartilhados e mutáveis; não usar para negócios reais.
