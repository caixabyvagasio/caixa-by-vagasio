# Progresso de execução — auditoria e correções prioritárias

Data: 2026-11-16. Branch alvo `development-v1`.

## Auditoria base
- Commit inicial auditado `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`: FastAPI/Python, SQLite, HTML/CSS/JS, suite inicial 39 testes; Render Free e banco `/tmp/caixa.sqlite3`, demo mutável e signup aberto, sem DB durável/backup. Detalhes em `PLANO-AUDITORIA-E-EXECUCAO.md`.
- Já existiam organizações/usuários, catálogo/estoque, compras/recebimentos, clientes, caixa/venda somente dinheiro, estorno, contas a pagar básicas, sugestões de segmentos e relatórios. Não equivalem à plataforma completa.

## Hardening e CI anteriores
- `cc9266726412e5db42b14347d33c5a7c569b64c6`: bloqueio de signup no modo demo e controle de acesso a PII/histórico clientes. CI `43913857996` success. Deploy ao Render Free de desenvolvimento confirmado `dep-dbtjga88v5cc7396nd3g`; signup HTTP 404, health/home 200. Não produção.
- `292e336139e358b9b9e7c3652293a7a63a1bdcdd`: papel estoque não pode criar/editar catálogo/preços; CI `43914431760` success.
- `fe4faba5f6305769c6d91773a94d1dbe2fb45a6b`: mais RBAC, UI clientes minimiza PII de operador, valuation sem overflow, testes; CI `43916794723` success. Não publicado.

## Bloco financeiro
- Commit inicial `ef082c4a41156a37f07d33188a6e387befda52d4`; CI `43950327110` success; não publicado.
- Implementado ledger `payable_payments` append-only, FK, índice e idempotência empresa/chave. Despesas com centros de custo livres; parcelas de contas a pagar; sem sobrepagamento; saldo restante, auditoria, quitação compatível e baixa manual histórica migrada.
- API: `POST /v1/finance/expenses`; `POST /v1/finance/payables/{id}/payments`; `GET /v1/finance/payables`; `GET /v1/finance/summary`; `GET /v1/finance/cashflow`. Portal: criar despesa, registrar pagamento parcial/total e ver fluxo diário separado em vendas dinheiro e baixas manuais.
- Bloco é gerencial/manual, não integrado ao banco. Sem contas a receber, conciliação, DRE, catálogo de centro de custo, gateway ou aprovação automática.

## Migrations e testes do refinamento atual
- Migrações agora ficam registradas na tabela `schema_migrations` com versões 1–4 e transações: coluna sales.customer_id, metadados product, índice único barcode e backfill idempotente de contas antigas pagas.
- Teste de versão/execução repetida e compatibilidade com schema legado adicionados. Suíte local final: **50 passed, 1 warning** (Starlette TestClient/httpx). AST Python OK; IDs referenciados na UI sem faltantes; 15 forms balanceadas.
- Não há node/deno/bun nem browser E2E disponível nesta sessão. Resultado não prova UX renderizada ou browser.
- Código/refinamento de migrations commitado em `9f2608002a72099e167096d512204d806bb89fce`, CI `43952022632` success. Suíte local final: 50 passed, 1 warning. Não publicar o bloco financeiro sem autorização.

## PostgreSQL Render e pendências
- PostgreSQL Free criado: `dpg-dbtmtfqfngtc739jvhk0-a`, Ohio, PG18, vence em 16/12/2026. Base vazia; dados anteriores do SQLite efêmero não são recuperáveis. Endpoint interno não é acessível diretamente deste sandbox; conexão deve ser validada pelo serviço Render.
- Adapter psycopg2 e workflow CI PostgreSQL real implementados no worktree. Primeira CI `43960213644` falhou com problemas reais de comparação date/text, upsert e teste signup; correções foram aplicadas. Suite SQLite final: 51 passed, 1 skipped, 1 warning.
- Novo commit/CI PostgreSQL precisa passar antes de conectar DATABASE_URL no Render. Depois ativar guard CAIXA_REQUIRE_POSTGRES e testar health/login/criação fictícia/reinício, sem publicar a connection string.
- Banco grátis expira em 30 dias e não tem backups/PITR; o usuário instruirá atualização posterior, mas necessita migrar antes de 16/12/2026.
- Prosseguir com Portal Master, filiais, entitlements, backup/restore, recebíveis/parcelamentos, conciliação, DRE, fiscal/pagamentos, offline e módulos especializados.
