# Progresso de execução — auditoria e correções prioritárias

Data: 2026-11-16. Branch alvo `development-v1`; commits mais recentes registrados no fim deste arquivo.

## Estado herdado e auditoria
- Auditoria do commit inicial `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`: API FastAPI/Python, SQLite, HTML/CSS/JS, 39 testes; deploy Render Free e banco `/tmp/caixa.sqlite3`, cadastro público ligado e demo mutável, sem DB durável/backup. Relatório detalhado em `PLANO-AUDITORIA-E-EXECUCAO.md`.
- Funcionalidades já implementadas antes: organização/usuário, catálogo e saldo de estoque, compras/recebimento idempotente, clientes, caixa/venda apenas dinheiro, estorno manual, contas a pagar de compras, perfil sugerido por segmento e relatórios operacionais. Não representam V1 comercial completa.

## Hardening aplicado antes do bloco financeiro
- Commit `cc9266726412e5db42b14347d33c5a7c569b64c6`: signup bloqueado no modo demo, PII/histórico de clientes sob RBAC e operador com nome apenas. CI `43913857996` success. Foi explicitamente deployado ao Render Free de desenvolvimento (`dep-dbtjga88v5cc7396nd3g` live); não produção. Health/home HTTP 200; POST de signup no ambiente de desenvolvimento retornou 404 conforme esperado.
- Commit `292e336139e358b9b9e7c3652293a7a63a1bdcdd`: papel stock não cria/altera catálogo e preços; CI `43914431760` success.
- Commit `fe4faba5f6305769c6d91773a94d1dbe2fb45a6b`: proteção extra de endpoints/campos por papel, UI cliente esconde campos privados a operador e cálculo Python sem overflow SQLite; teste de limite e estrutural UI. CI `43916794723` success. Não publicado.

## Incremento financeiro implementado no worktree (ainda não commitado)
- Criada tabela append-only `payable_payments` indexada por organização/conta, com idempotência, FKs e triggers que impedem update/delete; detalhe de despesas persiste centro de custo e hash de criação idempotente.
- `POST /v1/finance/expenses` cria uma despesa como conta a pagar, centro de custo livre e exige `Idempotency-Key`; replay idêntico retorna mesmo registro (200), payload diferente mesma chave 409. Pode declarar pagamento total manual opcional na criação; nunca descrito como banco confirmado.
- `POST /v1/finance/payables/{id}/payments` registra parcelas manuais: idempotência, reference, data não futura, limite ao saldo, parcial e quitação, auditoria transacional e tenant scope.
- Endpoint existente de quitação manual agora registra pagamento total restante no ledger append-only, preservando compatibilidade.
- GET contas retorna pago, saldo e centro de custo; resumo considera saldo após parcelas; `/v1/finance/cashflow` soma por dia vendas em dinheiro pagas separadamente das baixas manuais declaradas, limitado a 366 dias.
- Portal financeiro agora tem formulário de despesa, pagamentos parciais/quit. saldo e fluxo por período, sem alegar conciliação. A UI mantém a chave idempotente da despesa enquanto um envio falha e usa chave do pagamento associada à obrigação.
- Testes locais após a rodada: 48 passed, 1 aviso depreciação Starlette TestClient/httpx. Checagem Python sintaxe e refs HTML/JS: sem refs ausentes; 15 forms balanceadas. Runtimes Node/Deno/Bun não estão disponíveis, então JS não foi syntax-checked por runtime nem testado em browser; não considerar validação visual/E2E. Não há runtime Node/browser E2E instalado/verificado nesta sessão.
- Documento técnico em `docs/FINANCEIRO-PARCIAL-V1.md` descreve rotas, semântica e limites.

## Pendências
- Nova versão do frontend tem fluxo de caixa; a busca de data inicia com o mês atual. HTML IDs todos referenciados, forms balanceadas; validar renderização em browser permanece necessário.
- Bloco financeiro ainda no worktree sem commit/CI; conferir novamente diff, executar suíte, commitar no branch dev e aguardar CI. Não publicar sem autorização.
- Próxima tarefa executável: migrations versionadas/abstração banco em desenvolvimento sem contratar serviço, explorar Postgres local/CI se disponível; persistência/backup real continua bloqueada por provider/custo.
- Faltam contas a receber, parcelamento de venda/recebimento, conciliação, DRE, cadastro estruturado de centro de custo, Portal Master, filiais, entitlements funcionais, integração pagamento/fiscal, operação offline e módulos especializados.
- Sem produto real: deploy Render Free usa SQLite volátil em `/tmp`; dados da demonstração são fictícios, compartilhados e modificáveis. Não usar operação comercial.
- O pedido do usuário veda publicar produção sem autorização. Nenhum deploy do bloco financeiro foi feito.
