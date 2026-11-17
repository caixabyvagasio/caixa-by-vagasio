# Plano mestre de entrega — Caixa by VAGASIO

**Início:** 2026-11-17 12:22 BRT  
**Base inicialmente pedida:** auditoria do commit `c6ea16fd7496976f4d2d777ff018616ee4521507`.  
**Branch alvo:** `development-v1` exclusivamente.  
**Produção e `vagasio-2026`:** fora de escopo de escrita; nunca usar nos testes.  
**Status global:** EM ANDAMENTO — não homologado, não pronto para produção.

## Estado inicial confirmado nesta retomada

- HEAD remoto lido antes de editar: `c6ea16fd7496976f4d2d777ff018616ee4521507`, mensagem “Document waiter portal progress and safety gates”. Sem commits posteriores observados.
- Deploy dev associado ao mesmo commit; execução confirmada anteriormente na auditoria como live. Nenhum dado de empresa real será alterado.
- O relatório de auditoria completo está em `relatorio_auditoria_caixa.md` e `/` não deve ser confundido com tela Master. O estado inicial continua: FastAPI monolítico, SPA, migrations 1–16, negócio cash-only, sem integrações pagamento/fiscal homologadas.
- O primeiro passo é corrigir o ciclo de desativação/reativação de contas garçom, verificável com tenant descartável de teste. Nenhuma implementação é declarada enquanto CI/Postgres e runtime não forem verificados.

## Regras de segurança e publicação

1. Antes de cada lote, reler branch HEAD e baixar/verificar as versões atuais dos arquivos a alterar; nunca sobrescrever um commit concorrente.
2. Alterações somente em `development-v1`. Rodar suíte geral e PostgreSQL antes de deploy; não aplicar migrations diretamente no banco persistente sem backup/restore comprovado.
3. Tratar todo dado de QA como fictício, isolado e não ligado a `vagasio-2026`. Não executar pagamentos, emissão fiscal nem operações financeiras reais.
4. Nunca atribuir registros históricos com `branch_id NULL` automaticamente. Desenhar conciliação explícita, preview/dry-run, dupla verificação e trilha auditável.
5. Não ativar feature status `AVAILABLE/ENABLED` nem anunciar conclusão sem backend+frontend+schema+permissões+testes+E2E e integração pertinente.
6. Se um provedor externo não estiver homologado, manter estado `unavailable`; não simular pagamento, fiscal, impressora, balança ou TEF.
7. A aplicação inicia migrations automaticamente. Toda migration deve ser aditiva/idempotente, CI PostgreSQL, revisão de lock/recovery e rollback strategy; se não houver recovery comprovada, não aplicar em base persistente.

## Ordem de implementação e critérios

### Fase 0 — Continuidade, checkpoint e baseline
- Confirmar HEAD, workflows, branch e serviço dev antes de cada lote.
- Fixar problema, impacto, arquivos/rotas e teste de regressão neste plano.
- Critério: evidência do commit remoto e do estado do deployment documentada; sem alteração em produção.

### Fase 1 — Segurança e integridade crítica
- Consolidar matriz de políticas endpoint→permission→role→tenant→branch; rejeitar acesso fail-closed.
- Corrigir desativação/reativação de waiter profile e validar tokens/sessões existentes.
- Auditar atomicidade, lock/concurrency, idempotência e constraint em operações de caixa, venda, estoque, pagamento manual, transferência e reserva.
- Definir backup, export, restore drill, RPO/RTO antes de qualquer migration persistente relevante. Estado atual backup/PITR não comprovado.
- Critério: teste negativo parametrizado, PostgreSQL concorrente para invariantes; estratégia e restore verificados em DB isolado; usuário desativado sem acesso.

### Fase 2 — Escopo multiempresa/multifilial
- Fazer inventário de 134 operações, contexts e queries; estruturar helper/request context de branch e escopo de repos.
- Definir um único source of truth para saldo: migrar transacionalmente para ledger por filial com snapshots/reconciliação; manter legado `NULL` intocado até conciliação manual assinada/auditada.
- Escopar estoque, catálogo policies, compras, caixa/venda, financeiro, clientes/fornecedores, restaurante e relatórios; composite FK/unique constraints onde seguro.
- UI de seleção e transferências devem operar só filiais associadas; produto/cliente master catalog precisa política compartilhada explícita.
- Critério: duas filiais operam em paralelo; cross-tenant/cross-branch retorna 403/404; ledger, relatórios e liquidação batem; dados NULL não acessíveis como atribuídos.

### Fase 3 — Arquitetura e núcleo comercial
- Extrair módulos de domínio incrementalmente de `app/main.py`: auth/tenant, products/inventory, sales/cash, purchasing, finance, branches, restaurant, permissions/master; routers/services/repositories/schemas.
- Preservar APIs, migração/teste por módulo.
- Completar categorias, produto ativo/histórico, variantes, barcode input contract, inventory count, loss, reservation, supplier orders/divergence, customer history/privacy, accounts receivable, cash reconciliation e relatórios.
- UI sem `prompt()/alert()`, forms com validation, loading/empty/error states e accessibles.
- Critério: contratos de API e testes behavior/UI completos e ledger reconciliado.

### Fase 4 — Restaurante
- Completar modelo de praça/garçom, adicionais/combos/variantes, recipes/ingredient stock decrement, KDS, counter/pickup/delivery, reservations lifecycle, table/service transfer, bill split by item/person, reversals/voids, cash settlement and reports.
- Corrigir offboarding, permissões e escopo branch.
- Critério: fluxograma solicitado executado via browser autenticado e Postgres QA; concorrência de mesa/estoque/recebimento; impressão externa apenas após aparelho e protocolo definidos.

### Fase 5 — Cinco segmentos restantes
- Grocery: barcode/scales contract, weighed item pricing, promotions, lots/expiry, inventory/loss/replenishment, multi registers.
- Apparel: sizes/colors/grades/SKUs, stock variants, sellers/goals/commissions, reservations, exchanges/returns/loyalty.
- Construction: estimates/quotes, conversion, UOM conversion, price lists, warehouse/reservations, pick/delivery/partial delivery.
- Services: professionals, availability/calendar, appointment reschedule/conflict exclusion, work order/quote/packages/materials/commission/client history.
- Retail general: complete common flow, promotion and returns.
- Critério de cada vertical: data model, migrations, permissions, API/UI, report, tenant/branch constraints, E2E workflow, feature catalog available only after proof.

### Fase 6 — Portal Master e ativação inteligente
- Implementar admin identity/MFA/least privilege, UI, company search/status/suspension, plans, subscription pending→active only through trusted manual administration or verified provider, entitlements, support/audit/service health.
- Subscription pending remains no access. No payment simulation.
- Activity recommendations must not activate unavailable modules; validate entitlement+plan+subscription period+permission+branch dependencies server-side.
- Critério: browser auth UI and negative cross-tenant/master RBAC tests; audit all privileged actions.

### Fase 7 — Integrações
- Prepare adapters for payments (Pix/cards/TEF), fiscal, printers/barcode scanners/scales, notifications and delivery providers.
- Implement only if credentials/provider/sandbox and formal requirements exist; webhook verification, replay/idempotency and reconciliation required.
- Until homologated, explicit unavailable state.

### Fase 8 — Testes e homologação
- Unit/domain, Postgres migrations, branch/cross-tenant negative, concurrency/integrity, failures/rollback, API and browser E2E all roles, desktop/mobile/a11y, load/performance, backup restore.
- Stage only synthetic QA. Provide exact run IDs, commits, deployment and screenshots.
- Critério de release: every module in scope has traceable acceptance; no critical issues; backups restore; integrations real/homologated or clearly disabled; do not claim more than verified.

## Work log / checkpoints

| 2026-11-17 14:49 | `a42548a9fd56771e79396752312f7d400a1a746d` | Legacy sales/cash/void and payable-row concurrency fixes local; 114 passed/1 skipped. Prior state on this commit is deployed/live. | CI general/Postgres; dev deploy on green. |

## Work log / checkpoints

| 2026-11-17 14:31 | `f2c649321749350cb57b598896b3edee7956007c` | Legacy restaurant table/tab/order/production/payment locks deployed/live, CI green. Legacy retail sale/cash movement/close/void concurrency locks local; 113/1 tests. | CI general/PostgreSQL; deploy after green. |

## Work log / checkpoints

| 2026-11-17 14:31 | `f254388c0615539c4da21a6f05f3f70060bd0180` | Branch/legacy reservations and tab-vs-service close concurrency fix deployed/live, CI general/Postgres green. Legacy restaurant table/tab/order/production/payment locks local; suite 111/1. | Run CI general/Postgres and deploy dev. |

## Work log / checkpoints

| 2026-11-17 14:22 | `aa6add7c72bde83bab775c8e89c3584a9863d91a` | Branch/legacy reservation overlap locks and service close-empty vs tab-open lock; local 110 passed/1 skipped. | CI general/PostgreSQL, then dev deploy. |

## Work log / checkpoints

| 2026-11-17 14:01 | `a9aeec33a0f5cf79bb72afce2204d48918da1a97` | Branch cash lock fix deployed/live, CI green. Restaurant locks for order/production/settlement + concurrent final-payment test local. | Extend table opening lock and CI. |
| 2026-11-17 14:01 | `a9aeec33a0f5cf79bb72afce2204d48918da1a97` | Restaurant table/service opening now row-lock; waiter test assertion corrected to existing API response contract. Full local 107/1, CI pending. | Run CI general/PostgreSQL; publish dev after both green. |

## Work log / checkpoints

| 2026-11-17 13:53 | `9a12da23b45dbaec11d9bd6ae45fe7607c756da6` | Caixa branch movements/sales/void/close local row-lock and cash balance validation; tests 105/1. | CI general/Postgres for register concurrency and verify before dev deployment. |

## Work log / checkpoints

| 2026-11-17 12:49 | `c2389d1088c7dca5a6ec858f74db25274418768f` | Revisão AST comparou permission_for_endpoint e require_permission em todos os handlers (finding inicial 5 divergências); alinhei sales report, branch product reads, close-empty, waiter management, restaurant operations report e branch restaurant payment. CI geral/Postgres posterior verde; Render dev live no commit da ordem. Full local 96/1. | Continuar teste PostgreSQL de concorrência de saldo. |
| 2026-11-17 12:57 | `d1167d484a1462e2347f0c4d3b06526a7cdbadd9` | Deploy verificado live. Teste concorrente em Postgres revelou SerializationFailure. | Aplicar lock e testar estoque/venda concorrentes no Postgres. |
| 2026-11-17 13:10 | `14b861d675802341f4f161a7caacfa98f9325d1f` | CI PostgreSQL `44121112420` falhou por serialização concorrente em branch stock upsert. | Corrigir locking e testar contra Postgres. |
| 2026-11-17 13:16 | `9301a720aa5e811ee3abd50667b6aa3729a183d3` | Stock adjustment e branch sales concorrentes com row-lock/READ COMMITTED; CI+deploy dev verdes. | Auditar transferências, compras, estornos e restaurante. |
| 2026-11-17 13:21 | `fb28c5551a87d2139a0763069525127f59a4f867` | Transfer lock fix + parallel transfer/overdraw tests locais. | Rodar CI e publicar dev apenas após passar. |
| 2026-11-17 13:30 | `e1f2a137278c75fa54bd22a59134155326367de1` | Transfer fix CI general+Postgres success; dev `dep-dbu80jl9fdbs73dv4r0g` live. | Seguir para receipts/void locks. |
| 2026-11-17 13:30 | `55378c69368872a01d77ebef13708070cd7a0d23` | Receipts lock fix CI general+Postgres success; dev `dep-dbu84959fdbs73dvhbvg` live. Local concurrent receipt test verified. | Implement and test same-sale concurrent void lock. |
| 2026-11-17 13:30 | `55378c69368872a01d77ebef13708070cd7a0d23` | Voids same-sale lock + concurrent test locally green, prior to CI. | CI and publish dev. |
| 2026-11-17 13:30 | `f17b1541dd55a19a2ffb0db6a7ab127c6ee79153` | Receipt and void locks passed general/Postgres CI (`44128071984`, `44128095176`, `44128072464`); dev deploy `dep-dbu879mi0phs739215gg` live; health Postgres. Full local 103/1. | Continue concurrency audit for register operations, order status/branch restaurant lifecycle, legacy stock path. |

## Work log / checkpoints

| BRT/data | HEAD | Trabalho/resultado | Próxima ação |
|---|---|---|---|
| 2026-11-17 12:22 | `c6ea16fd7496976f4d2d777ff018616ee4521507` | HEAD confirmado antes de alterações; auditoria completa relida. Sem dados modificados. | Corrigir user/waiter active lifecycle; testes e CI; prosseguir em segurança/multibranch. |
| 2026-11-17 12:30 | `0bc9104f8af612e168ceff04d488ae8306010ccf` | Correção waiter user/profile active implementada, testada local 101 passed/1 skipped, CI geral+PostgreSQL verdes; deploy dev `dep-dbu74up42hec73bp4jbg` live, health PostgreSQL OK. | Ampliar guard de multi-filial e continuar fase 1/2. |
| 2026-11-17 12:45 | `041f5617990583d1dae75aeb36e13f48d0055f1a` | Guard fail-closed nas rotas legacy operacionais implementado; teste 11 endpoints; local full 94/1; CI general+Postgres verdes; dev live `dep-dbu7a6jrjlhs73afg45g`, health PostgreSQL. Sem UI autenticada e sem operação em tenant real. | Revisar exceções shared/master/branch route; continuar refactor de source-of-truth e demais fases. |

## Pendências de maior prioridade visíveis no baseline

- Backup/PITR/restore do Postgres persistente não comprovados; sem alteração de schema persistente até estratégia verificada.
- Multi-filial é parcial, legacy branch NULL e dois ledgers de stock; endpoint policy scope revisto route by route.
- Produtos e compras ainda coexistem em caminhos company-wide e branch-scoped.
- Portal Master web e roles/administration inexistentes.
- Cinco verticais especializadas ainda não implementadas.
- Restaurante não tem adicionais/recipes/delivery/KDS real/bill-by-item/cancel-close UX completa.
- `prompt()/alert()` operacional em frontend.
- Pagamento, fiscal e hardware indisponíveis; não simular.
- UI autenticada E2E ausente; exigência para próxima validação QA.
