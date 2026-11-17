# Filial e ledger de estoque — status em desenvolvimento

Commit-base executável de retomada: `7835a4310325fb593eaac4dd75f8eaae9eba7f32`.

## Implementado nesta fatia (ainda local até publicação)
- Migration 8 `branch_scoped_stock_ledger`: ledger/saldo independente por (empresa, filial, produto), com eventos de estoque correlacionados e transferências idempotentes.
- APIs: `GET /v1/branches/{branch_id}/inventory`; `POST /v1/branches/{branch_id}/inventory/{product_id}/adjustments`; `GET /v1/branches/{branch_id}/stock-movements`; `POST /v1/branches/stock-transfers`.
- Toda operação valida empresa ativa, filial ativa e associação `branch_users`. Transferência exige acesso a ambas as filiais, chave de idempotência, produto do mesmo tenant e saldo suficiente, alterando os dois saldos em transação e mantendo dois eventos correlacionados. Replays iguais retornam operação existente; chave repetida com payload diferente gera 409.
- Não importa `products.stock_milli` nem movimentações antigas no ledger: começam em zero e registros legados continuam com `branch_id NULL`; resposta informa o legado não atribuído.
- Seletor de filial persistido no localStorage e cabeçalho `X-Branch-Id` implementados na API web; card básico de saldo e ajustes no cadastro de produtos.
- Guarda de multi-filial permanece para rotas não particionadas. Rotas novas de estoque por filial expostas em multi-filial; associação do usuário validada pelo backend.
- Testes locais completos: 75 passed, 1 skipped (teste PostgreSQL condicionado). Deve reexecutar após adição das telas do ledger e também validar PostgreSQL antes de publicar.

## Ainda não entregue / bloqueios de liberação
- PDV, estoque legado, caixas, compras/recebimentos, financeiro e restaurante ainda não persistem/filtram `branch_id`; continuam bloqueados para organizações com mais de uma filial, não liberar esses fluxos.
- Não há conciliação de histórico (não atribuir automaticamente); nem reconciliação reversível estruturada ainda.
- Sem teste autenticado visual dos novos fluxos; sem E2E via UI ou testes reais de concorrência de transferências PostgreSQL. Não afirmar validação visual.
- Próximo: completar testes depois dos arquivos UI, CI/PostgreSQL, commit em development-v1 e CI/Render dev; autenticar browser só pelo formulário seguro sem parâmetros na URL; validar card/ajuste. Depois particionar compras/recebimento e caixa/vendas com os mesmos branch IDs antes de restaurante; manter guard até todos operacionais por branch.
- Final local full suite after latest UI/backend/policy edits: `75 passed, 1 skipped, 1 warning`; skip is the opt-in PostgreSQL fixture because local env does not provide `CAIXA_TEST_POSTGRES`. Need run official PostgreSQL workflow before claiming Postgres coverage for migration 8.

## Incremento extra: caixas e vendas por filial
- Migration 9 adiciona `branch_cash_registers` (um caixa aberto por filial) e ledgers de vendas/itens/movimentos separados. APIs para abrir/consultar/movimentar/fechar caixa por filial e POST venda em dinheiro: `/v1/branches/{id}/registers/...` e `/v1/branches/{id}/sales`.
- Venda exige caixa daquela filial, saldo do ledger da filial, Idempotency-Key, cliente same-company se indicado, valida total/troco; transação baixa o saldo e credita movimentos de caixa específicos, sem tocar produto.stock legado, caixa legado ou venda legado. Não suporta devolução/void ainda.
- Teste E2E de API simulou 2 filiais, caixas separados, estoque 5 só na origem, venda 2 unidades, replay idempotente, filial destino negada por falta de estoque e saldos/caixa independentes. Migração v9 listada no teste de schema.
- Reexecutar full local/CI/PostgreSQL após esse incremento. Os endpoints ainda não foram montados na tela; seletor de filial e painel de inventário são a interface atual.
- Devolução, compras, financeiro e restaurante ainda são rotas legacy company-scope e continuam bloqueadas em empresas multi-filiais.

## Continuação 22:48–23:14 BRT
- Implementados (ainda local nesta seção) migration v8 estoque por filial, v9 caixa e venda por filial, APIs independentes. Corrigida policy router para sales.void e cash manage em rotas filiais.
- Venda por filial: requer ledger saldo e caixa da mesma filial, baixa branch_product_stock, insere branch_sales/branch_sale_items e branch_cash_movements; não altera as tabelas legacy. Idempotência testada; void requer role de gerente e sales.void, saldo caixa suficiente, restaura apenas saldo local, registra movimento reverso, branch_sales_returns e auditoria.
- UI: seletor recebe lista `/v1/company/branches`, persiste escolha em localStorage, envia X-Branch-Id; card de estoque mostra saldo ledger por filial e permite ajuste. UI de abrir/fechar caixa, venda e void por filial ainda não existe; rotas company-wide permanecem 409 quando multi-branch.
- Testes finais locais no arquivo atual: 78 passed, 1 skipped (PostgreSQL condicional), 1 aviso Starlette/httpx. Os 4 testes focados de estoque/transferência, caixa, venda e estorno: 4/4. Corrigido esperado de caixa para incluir vendas no ledger. Nenhum CI PostgreSQL foi executado ainda para migrations 8/9 e branch sale/void.
- NADA da branch atual local além do commit anterior `e0a9e96a4ba2fd6032c1cb1102112bfb29afa563` foi commitado/publicado. Desenvolvimentos mais recentes estão em `scratch/caixa/work`. CI e produção permanecem do commit anterior; agora precisa preparar um commit novo em cima de e0a9e96 (branch de fato terá avançado), executar CI geral + Postgres e deploy somente desenvolvimento se passar.
- Escopo ainda grande e aberto: compras/recebimentos por filial, financeiro por filial, multi-cash/sales UI, reconciliação histórica explícita e reversível (histórico NULL segue intocado), restaurantes por filial; interface autenticada /static JS de login teve no passado navegação query string e erro 404; não afirmar validação visual. Recursos adicionais de restaurante e os quatro outros segmentos também não foram implementados nesta sequência.
- Não abrir o guard de multi-filial para fluxos não particionados. Dados `vagasio-2026` não usados/alterados; payments/fiscal seguem off.
- Próxima tarefa exata: atualizar os 6 arquivos (backend tests js html css e este checkpoint) num único commit sobre o branch live atual `e0a9e96`, CI + Postgres, deploy dev/health; depois implementar branch-scoped purchases/receipts and ledger expense before restaurant. Não encerrar ordem mestre.

## Publicação em desenvolvimento desta fatia
- Commit `d7b2bb072cffc02530b7e4e50adf38f91b258bd4` implantado no branch `development-v1`; CI geral run `44001860586` success; CI PostgreSQL run `44001860302` success; Render deployment `dep-dbtricuk1f9s739i1eng` live para mesmo SHA; `/health` status ok, storage PostgreSQL, payments/fiscal false.
- Não foi realizada validação autenticada visual (o formulário de login já redirecionou anteriormente para querystring quando enviado; não repetir tal método). Só testes API/CI e health. UI incluída mas não comprovada autenticada.
- Branch ainda tem bloqueio global em endpoints legacy multi-filial; fluxos recentes de `branches/{id}/inventory`, `stock-movements`, `stock-transfers`, `registers/*`, `sales` e `sales/{sale}/void` usam ledgers separados; `branch_id` pode não ser obrigatório no header nessas rotas, pois ID explícito do recurso valida membership server-side.
- Saldos legacy ficam fora do novo ledger e não são contados nem transferidos automaticamente. Não há fluxo administrativo de reconciliação, inventário/ajuste idempotente/ledger é separado do catálogo legado. Nenhum dado comercial real manipulado.
- Pendências para continuar: compra/recebimento por branch e stock entry; list/detail/report por branch para vendas/estoque/caixa; contas financeiras branch-owned; produtos/fornecedores de leitura com contexto; restaurante por filial, com features pedidos (add-ons/recipes/delivery/splits/transfers/cancellations, etc.); UI de caixa/vendas/troca usando endpoints novos; E2E auth UI. Só após mapear/filtrar cada route pode liberar a barreira de empresas multi-branch. Outros 5 segmentos especializados seguem sem implementação funcional completa; ordem mestre permanece incompleta.
- Continuação pedida é sem pausa voluntária. O contexto de execução desta sessão está encerrando; retomar no head `d7b2bb...` e próxima tarefa imediatamente branch-scoped purchases/receipts, sem refazer migrações anteriores; manter produção e `vagasio-2026` intocados.
