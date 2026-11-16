# Caixa by VagasIO — Auditoria e plano contínuo

Atualização inicial: 2026-11-16. Base auditada: `development-v1`, commit `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`. Deploy atual é uma demonstração Render Free, não produção; esta ordem proíbe publicação em produção sem nova autorização.

## Inventário auditado

Stack: FastAPI/Python, SQLite, HTML/CSS/JavaScript sem framework, pytest/TestClient, deploy Render Free. Repositório contém `app/main.py` (API/esquema/migrações inline), `frontend/index.html`, `app.js`, `app.css`, workflows CI e documentação. 39 testes do commit publicado passaram localmente; CI GitHub success. Navegador confirmou página, catálogo/relatório de estoque e health HTTP 200. Auditoria deste turno é leitura de código + suíte automatizada + smoke checks, não teste E2E completo de cada botão/fluxo.

### Funcionalidades existentes e evidência
- Autenticação bearer HMAC, PBKDF2 310k, papéis owner/admin/manager/operator/stock, rate-limit local: `app/main.py`.
- Organizações por tenant e filtros de companhia em CRUD e transações principais; cobertura parcial de isolamento no pytest.
- Perfil de segmento, questionário, sugestões de módulos e versões append-only. Não há entitlements, habilitação real, assinatura/plano nem menus derivados dos direitos.
- Produtos/categorias/código de barras/estoque auditado; compras/recebimentos idempotentes; clientes; caixa/vendas somente dinheiro; cancelamento/estorno; contas a pagar com baixa manual; relatório de vendas e avaliação estimada de estoque.
- No seed de demonstração: dados fictícios de varejo, visíveis e mutáveis a todos que acessam a demo.

## Achados classificados

| ID | Severidade | Causa e impacto | Correção e aceitação |
|---|---|---|---|
| AUD-01 | Crítica | DB configurado em `/tmp/caixa.sqlite3` no Render Free; perde dados, sem backup/restore. Inadequado a qualquer operação real. | Manter demo explicitamente descartável; implementar PostgreSQL/migrations/backup/restore em infraestrutura autorizada. Testar migração, restore e consistência. A dependência de provedor/destino durável bloqueia ambiente comercial. |
| AUD-02 | Alta | `CAIXA_PUBLIC_SIGNUP=true` no serviço demo permite criar tenants públicos no SQLite temporário. | Bloquear signup quando `DEMO_MODE` estiver ligado; cobrir com teste. Verificar posteriormente configuração de deploy sem substituir secrets/variáveis inadvertidamente. |
| AUD-03 | Alta | GET clientes não aplicava RBAC; listagem incluía tax_id, notas, telefone e e-mail para qualquer papel autenticado; detalhe/histórico igualmente sem RBAC. | Corrigido no commit `cc92667` em branch (sem deploy): detalhe e campos pessoais ficam em owner/admin/manager; operador limitado ao nome, estoque bloqueado. Testes de PII e tenant. |
| AUD-04 | Alta | Limiter usa memória de processo; perde estado ao reiniciar e não coordena instâncias. | Manter proteção local, mas exigir rate limit no edge/distribuído antes de uso público. Testar limites; infra externa pendente. |
| AUD-05 | Alta | Sem Portal Master, multi-filial, RBAC por filial, planos/entitlements, suporte ou administração global. | Projetar identidade Master separada e isolamento cross-tenant; implementar em fase, testes negativos antes das telas. |
| AUD-06 | Alta | PDV tem apenas dinheiro; sem Pix/cartão, pagamento misto, devolução efetiva ou comprovante fiscal. | Criar ledger e contratos de provedores, estados pending/authorized/failed/refunded; só sandbox e credenciais fornecidas, sem simular aprovação. |
| AUD-07 | Alta | Persistência/financeiro são parciais: contas a pagar simples, recebíveis/parcelamento, conciliação, despesas, DRE e centros de custo ausentes. | Modelo de ledger idempotente e trilha, receber/pagar parcial e reconciliação, sem misturar marcação manual com confirmação externa. |
| AUD-08 | Média/Alta | Segmentos sugerem recursos, mas o backend chama todas as features `available=1`; módulos especializados não existem nem têm entitlement. | Estado separado recomendado/implementado/contratado/ativo, ativação server-side e configuração sem apagar histórico. |
| AUD-09 | Média | Clientes retornavam dados sensíveis; motivo era autorização ausente e resposta excessiva. | Endpoints corrigidos em `cc92667`, não publicados. Varredura adicional de papéis identificou edição de preços/custo por estoque; corrigida em `292e336`, CI aprovado; continuar revisão sistemática. |
| AUD-10 | Média | Frontend usa prompts/alerts do navegador para movimentos, recebimentos, baixas e histórico de cliente; UX móvel/teclado/acessibilidade limitada. | Substituir em etapas por diálogos acessíveis, estados ocupados/erro/vazio, validação inline, testes funcionais. |
| AUD-11 | Média | Migrações são idempotentes inline no startup SQLite, sem versão formal, backend de banco alternativo ou ensaios de rollback/restore. | Criar sistema versionado e testado antes de PostgreSQL; só migrar dados após backup verificável e autorização operacional. |
| AUD-12 | Baixa/Média | 39 testes API focam happy/negative paths; não há suíte browser E2E automatizada, segurança independente, accessibility audit nem carga. | Ampliar unit/integration/E2E, RBAC matriz, concorrência, scripts de restore, axe/lighthouse quando ferramentas disponíveis. |

## Plano por fases, prioridade e critérios de aceitação

1. **Auditoria e registro (em andamento):** mapa de módulos/rotas, riscos, plano versionado e execução rastreável. Aceite: inventário contra código/CI/runtime e gaps explicitados.
2. **Críticos: privacidade, demo e estabilidade:** corrigir RBAC/Pii, bloquear signup na demo, teste de isolamento; checar concorrência/idempotência e segurança de respostas. Aceite: testes por papel/tenant e CI.
3. **Persistência/migrações:** abstraction para PostgreSQL sem perder SQLite demo, migration runner versionado, backup/restore testado, health de DB e documented env. Aceite: suite em SQLite + PostgreSQL; não usar dado comercial antes de backup restore e ambiente autorizado.
4. **Modelo SaaS seguro:** organização/filial, claims/entitlements, papéis/ações, sessões e audit; API Master isolada e logs. Aceite: nenhuma rota privilegiada acessível por tenant normal; teste cross-tenant.
5. **Portal Master:** empresas, filiais, planos/entitlements, módulos, terminais, suporte, comunicados e indicadores reais. Aceite: cada ação persiste, audita, pagina, filtra e tem autorização server-side.
6. **Portal Empresarial:** categoria/variante/inventário, cotação/pedido, troca/devolução, contas a pagar/receber e relatórios. Aceite: operações transacionais/ledger, estado/auditoria e validação ponta a ponta.
7. **PDV:** operadores/terminal, scanner wedge, descontos, vendedor, múltiplos pagamentos por interface provider, suspender/recuperar, comprovante, devolução e fechamento. Aceite: idempotência/concorrência e nenhuma aprovação fictícia.
8. **Planos e segmentos:** wizard da empresa, escolha de plano e revisão, entitlements reais e menu/dash por módulo. Aceite: só ativa recurso implementado+contratado; desligar não elimina histórico.
9. **Módulos especializados:** restaurante, supermercados, vestuário, serviços/beleza e construção integrados ao núcleo. Aceite por módulo com fluxos reais, testes e privacidade; não usar mero catálogo como conclusão.
10. **Fiscal/dispositivos/offline:** contratos/adaptadores, sandbox, contingência, auditoria de autorização e fila idempotente. Aceite: certificações e acesso externo necessários; jamais anunciar produção sem homologar.
11. **Preparação de homologação:** pentest, accessibility/mobile/E2E/carga, backup/restore, runbooks, monitoramento, documentação, release e rollback. Aceite: CI e validação operacional completos; deploy produtivo exige autorização expressa.

## Progresso deste ciclo
- [x] Próximo incremento financeiro desenvolvido no worktree: despesa com centro de custo, pagamentos parciais idempotentes append-only, total pago/saldo por obrigação, baixa completa compatível com ledger, summary e fluxo operacional por período distinguindo vendas em dinheiro de baixas declaradas. Tela financeira acrescenta cadastro de despesa, pagamentos e tabela por período.
- [x] Testes de tenant, roles, baixa parcial/final, replay/conflict, excesso, anotação manual, imutabilidade do ledger, expense create idempotência, cashflow com venda+saída e segurança de interface; 48 passed, 1 warning no suite local.
- [x] Commit/CI inicial do bloco financeiro: `ef082c4a41156a37f07d33188a6e387befda52d4`, run `43950327110` success; não foi deployado.
- [x] Migrations SQLite versionadas 1–4 para schema changes antigos e backfill, testadas localmente inclusive reruns e compatibilidade com schema legado (50 tests passed); refinamento será commitado e CI rodado, sem deploy.
- [ ] O módulo continua parcial: sem contas a receber, conciliação, DRE e integração bancária. Centro de custo atualmente é rótulo, não cadastro analítico.

## Progresso deste ciclo
- [x] Auditar commit atual, ambiente, endpoints, esquemas, papeis, dependências, testes e implantação.
- [x] Reproduzir localmente os 39 testes do commit (`39 passed`, 1 warning conhecido de depreciação TestClient/httpx).
- [x] Corrigir signup público na demonstração e exposição de dados de clientes; adicionar testes (`cc92667`, CI success; sem deploy por ordem de não publicar).
- [x] Commitar primeiras correções e plano apenas na branch `development-v1`; CI success `43913857996`.
- [x] Corrigir RBAC de produtos: `stock` não cria nem altera catálogo/preço/custo; teste em `292e336`, CI run `43914431760` success. Nenhuma dessas duas correções foi deployada.
- [ ] Prosseguir para migrações/persistência, SaaS/Master, PDV e módulos conforme fases; registrar bloqueios de infraestrutura e homologação.

## Restrições / dependências

Não provisionar banco pago, disco, backups, provedor de pagamento, gateway fiscal, certificados, domínio ou device sem autorização específica/custo aprovado. Não transferir nem apagar dados de produção. O Render atual é demo descartável e não é produção nem destino de dados reais. Código local/branch de desenvolvimento não equivale a feature pronta/publicada/homologada.

### Continuação após testes por papel (sem deploy)
- [x] AUD-02/03 corrigidos e CI/deploy de desenvolvimento confirmados em `cc92667` (`dep-dbtjga88v5cc7396nd3g` live); esse deploy foi ao Render Free de desenvolvimento, nunca produção. Signup público retorna 404 em demo; leitura de PII minimizada por role.
- [x] `stock` sem create/update catálogo ou preços (`292e336`, CI `43914431760`).
- [x] Na etapa local de hardening, `stock` não vê custo de produtos, fornecedores PII, vendas, caixa, dashboard, relatórios de estoque/vendas, auditoria nem clientes; operadores têm nome de cliente apenas e sem endpoints de histórico/PII. Testes API atualizados.
- [x] Overflow do resumo de valuation alterado para aritmética inteira Python; teste de valor grande.
- [x] Commit/CI dos últimos ajustes de RBAC/overflow/UI no commit `fe4faba`, CI `43916794723` success. Não deployar sem autorização.
- [ ] Continuar fases 3–11; conclusão global não atingida.
- [x] Agregados testes estruturais de frontend: refs de elementos, form balanceado, proteção UI PII, breakpoints móveis e presença de security headers; total de suíte local 47 passed no checkpoint pré-final. Ver CI `fe4faba` para versão commitada.
